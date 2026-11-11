"""原始文件获取、CSV 格式与表头核验、Doris 建表初始化、Stream Load 原子导入与结果核对模块。"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from warehouse.config import Settings, ensure_runtime_dirs, resolve_project_path
from warehouse.db import connect_doris
from warehouse.services import compute_sha256

RESERVED_METADATA_COLUMNS: tuple[str, ...] = (
    "_ingest_batch_id",
    "_source_file",
    "_source_sha256",
    "_ingested_at",
)

VALID_COLUMN_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# 官方仓库 (Cainiao-AI/LaDe) 17 份数据文件的实测/LFS 字节数与 SHA-256 基线
LADE_OFFICIAL_FILE_SPECS: dict[str, tuple[int, str]] = {
    "delivery/delivery_jl.csv": (
        4_736_342,
        "12e2cf4664dd5b4475d39dddee8872f5a03b3082f08f0eece7f103baee6c6e73",
    ),
    "delivery/delivery_yt.csv": (
        31_532_960,
        "aa5bca7fdaa8c89d4ab50a2ed2b94fb09f608acc49fc1108a1a612e9c0f5a7ad",
    ),
    "delivery/delivery_cq.csv": (
        145_068_417,
        "114a35e9a31f817a3c9efb8513924688706bcedfa85ccd69df696eb5e8eddea2",
    ),
    "delivery/delivery_sh.csv": (
        228_069_571,
        "da875abddbb01ac324106b9b9ce33181e5e8530e77359a12ee68567fd1cc3bbf",
    ),
    "delivery/delivery_hz.csv": (
        286_893_611,
        "9969df2a5c79dadc6149c384e3632eccc0417d01a2575a74db2310e786541c22",
    ),
    "pickup/pickup_jl.csv": (
        43_801_745,
        "ac5b9f76605b544bd19d79703d137c8656c5395dfaeeb297e3b10ea6b30c3eb1",
    ),
    "pickup/pickup_yt.csv": (
        190_190_713,
        "00c049925a41d92112795718021f348da07f1adb84002de3f5707aef6ac01e91",
    ),
    "pickup/pickup_cq.csv": (
        190_522_468,
        "d58248d1aca9cf155bd202f311dff73f8562174d4e4041160a3996878143b056",
    ),
    "pickup/pickup_sh.csv": (
        228_339_849,
        "ebc564f1b6235e09ccca7cf4ccfe8a2014c91e99d973516e69350da9f7424db9",
    ),
    "pickup/pickup_hz.csv": (
        335_817_674,
        "390b80be91d9b1ff80594c29d5206633db2759d54ccd4dff3d9f6f80513ab3e2",
    ),
    "road-network/roads.csv": (
        231_746_519,
        "52d3f4ccf33a32cec42e2aa9f3083d8b2e79670b30bbd88f00f3fb9ad7979a0a",
    ),
    "delivery_five_cities.csv": (
        135_539_702,
        "a22b49b57a0201a2b569f80a8282e9147d37db1c2cc374fdf6111aad3d9a3d97",
    ),
    "pickup_five_cities.csv": (
        154_069_529,
        "966b5365d0a02422929088277c2838569c5a30458275e4b2587cb9b64fb8011a",
    ),
    "courier_detailed_trajectory.zip": (
        160_005_925,
        "68ca61378d5521844abab0249d72bc340e6aa24e9d6b11a64ced4a6f06b9d098",
    ),
    "data_with_trajectory_20s/delivery_five_cities.pkl.xz": (
        34_028_796,
        "1502c01e794504a68ba846e5bc3bf3404cd84c0c22c7c37ae585cd614079d905",
    ),
    "data_with_trajectory_20s/pickup_five_cities.pkl.xz": (
        41_345_044,
        "06f9c1db6cf136bbfa8bb4882113abbdee4c42998ddba63603133c8e4df5bffc",
    ),
    "data_with_trajectory_20s/courier_detailed_trajectory_20s.pkl.xz": (
        438_656_312,
        "61704f2735cafda3b4d455350dcde97e430d5926f3773a5e4ca7e7e8cc100cc4",
    ),
}


def _sanitize_source_file_label(file_path: Path, settings: Settings) -> str:
    """生成不包含敏感宿主机绝对路径的源文件标识。"""
    try:
        return file_path.resolve().relative_to(settings.data_dir.resolve()).as_posix()
    except ValueError:
        pass
    try:
        return file_path.resolve().relative_to(settings.project_root.resolve()).as_posix()
    except ValueError:
        return file_path.name


def resolve_target_raw_file(
    settings: Settings, file_arg: str | None = None
) -> Path:
    """解析目标原始文件路径（优先使用命令行 --file 参数，否则使用默认吉林派件文件路径）。"""
    if file_arg:
        return resolve_project_path(file_arg, settings.project_root)
    return (settings.data_dir / settings.lade_default_file).resolve()


def _download_or_verify_remote_file(
    settings: Settings,
    logger: logging.Logger,
    rel_file: str,
    source_url: str,
    update_latest: bool = True,
) -> tuple[int, dict[str, Any] | None]:
    """下载或复用单个远端数据文件，支持断点续传、有界重试、SHA-256 核验与临时文件原子替换。"""
    clean_rel = rel_file.strip().lstrip("/")
    target_path = (settings.data_dir / clean_rel).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    spec = LADE_OFFICIAL_FILE_SPECS.get(clean_rel)
    expected_spec_size = spec[0] if spec else None
    expected_spec_sha = spec[1] if spec else None

    # 1. 目标文件已存在时，校验字节数与 SHA-256 后直接复用
    if target_path.exists() and target_path.is_file():
        size_bytes = target_path.stat().st_size
        if size_bytes > 0:
            sha256 = compute_sha256(target_path)
            if (
                (expected_spec_size is None or size_bytes == expected_spec_size)
                and (expected_spec_sha is None or sha256 == expected_spec_sha)
            ):
                logger.info(
                    "检测到本地已存在完整原始文件，校验后直接复用: %s (size=%d, sha256=%s)",
                    target_path,
                    size_bytes,
                    sha256,
                )
                manifest = {
                    "source_mode": "reused_existing",
                    "source_url": source_url,
                    "source_file_label": _sanitize_source_file_label(
                        target_path, settings
                    ),
                    "local_file_name": target_path.name,
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                    "reused": True,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
                _save_fetch_manifest(
                    settings, sha256, manifest, update_latest=update_latest
                )
                return 0, manifest
            logger.warning(
                "本地文件 %s 与官方基线不一致（实际 size=%d/sha=%s，期望 size=%s/sha=%s），将重新下载至临时文件。",
                clean_rel,
                size_bytes,
                sha256,
                expected_spec_size,
                expected_spec_sha,
            )

    # 2. 流式下载至 .part 临时文件（支持断点续传与最多 5 次重试），校验通过后原子替换
    tmp_file = target_path.with_name(f".{target_path.name}.part")
    max_attempts = 5
    read_timeout = float(max(settings.lade_download_timeout, 60))
    timeout_cfg = httpx.Timeout(
        connect=30.0, read=read_timeout, write=30.0, pool=30.0
    )

    for attempt in range(1, max_attempts + 1):
        resume_offset = tmp_file.stat().st_size if tmp_file.exists() else 0
        if expected_spec_size and resume_offset > expected_spec_size:
            tmp_file.unlink(missing_ok=True)
            resume_offset = 0

        req_headers: dict[str, str] = {}
        if resume_offset > 0:
            req_headers["Range"] = f"bytes={resume_offset}-"
            logger.info(
                "断点续传下载 [%s] (尝试 %d/%d, 已下载=%d bytes): %s",
                clean_rel,
                attempt,
                max_attempts,
                resume_offset,
                source_url,
            )
        else:
            logger.info(
                "开始从官方数据源下载原始文件 [%s] (尝试 %d/%d): %s -> 临时文件 %s",
                clean_rel,
                attempt,
                max_attempts,
                source_url,
                tmp_file.name,
            )

        try:
            with httpx.Client(
                follow_redirects=True, timeout=timeout_cfg
            ) as client:
                with client.stream(
                    "GET", source_url, headers=req_headers
                ) as resp:
                    if resp.status_code == 416 and expected_spec_size:
                        # 已下载完整个临时文件
                        pass
                    else:
                        resp.raise_for_status()
                        is_partial = resp.status_code == 206 and resume_offset > 0
                        content_len_raw = resp.headers.get("content-length")
                        part_len = (
                            int(content_len_raw)
                            if content_len_raw and content_len_raw.isdigit()
                            else None
                        )
                        if is_partial:
                            total_expected = (
                                resume_offset + part_len
                                if part_len is not None
                                else expected_spec_size
                            )
                            file_mode = "ab"
                            written = resume_offset
                        else:
                            total_expected = (
                                part_len
                                if part_len is not None
                                else expected_spec_size
                            )
                            file_mode = "wb"
                            written = 0

                        next_log_pct = 25
                        with tmp_file.open(file_mode) as out:
                            for chunk in resp.iter_bytes(chunk_size=1024 * 512):
                                out.write(chunk)
                                written += len(chunk)
                                if (
                                    total_expected
                                    and total_expected >= 20 * 1024 * 1024
                                ):
                                    pct = int(written * 100 / total_expected)
                                    if pct >= next_log_pct and pct < 100:
                                        logger.info(
                                            "下载进度 [%s]: %d%% (%d / %d bytes)",
                                            clean_rel,
                                            pct,
                                            written,
                                            total_expected,
                                        )
                                        next_log_pct = ((pct // 25) + 1) * 25

            actual_written = tmp_file.stat().st_size if tmp_file.exists() else 0
            if actual_written == 0:
                tmp_file.unlink(missing_ok=True)
                logger.error("下载文件大小为 0 字节，已清理临时文件: %s", clean_rel)
                return 1, None

            if (
                expected_spec_size is not None
                and actual_written != expected_spec_size
            ):
                if actual_written > expected_spec_size:
                    tmp_file.unlink(missing_ok=True)
                    logger.error(
                        "下载文件 [%s] 实际字节数 (%d) 超出官方基线 (%d)，已清理异常临时文件。",
                        clean_rel,
                        actual_written,
                        expected_spec_size,
                    )
                    return 1, None
                logger.warning(
                    "下载文件 [%s] 字节数尚未完整（期望=%d，实际=%d），准备断点续传重试...",
                    clean_rel,
                    expected_spec_size,
                    actual_written,
                )
                if attempt == max_attempts:
                    logger.error(
                        "下载文件 [%s] 字节数尚未完整（期望=%d，已写入=%d）；已保留临时文件 %s 供下次断点续传，未覆盖正式文件。",
                        clean_rel,
                        expected_spec_size,
                        actual_written,
                        tmp_file.name,
                    )
                    return 1, None
                time.sleep(2.0)
                continue

            sha256 = compute_sha256(tmp_file)
            if expected_spec_sha is not None and sha256 != expected_spec_sha:
                tmp_file.unlink(missing_ok=True)
                logger.error(
                    "下载文件 [%s] SHA-256 校验不匹配（期望=%s，实际=%s），已清理损坏临时文件。",
                    clean_rel,
                    expected_spec_sha,
                    sha256,
                )
                return 1, None

            tmp_file.replace(target_path)
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "下载文件 [%s] 第 %d/%d 次尝试中断: %s",
                clean_rel,
                attempt,
                max_attempts,
                exc,
            )
            if attempt == max_attempts:
                part_size = tmp_file.stat().st_size if tmp_file.exists() else 0
                if part_size == 0 or (
                    expected_spec_size is not None
                    and part_size >= expected_spec_size
                ):
                    tmp_file.unlink(missing_ok=True)
                    logger.error(
                        "下载原始文件异常中断（已清理无效临时文件，未破坏既有正式文件）: %s",
                        exc,
                    )
                else:
                    logger.error(
                        "下载原始文件异常中断（已保留临时文件 %s [%d bytes] 供下次断点续传，未破坏既有正式文件）: %s",
                        tmp_file.name,
                        part_size,
                        exc,
                    )
                return 1, None
            time.sleep(min(2.0 * attempt, 8.0))

    size_bytes = target_path.stat().st_size
    sha256 = compute_sha256(target_path)
    manifest = {
        "source_mode": "downloaded",
        "source_url": source_url,
        "source_file_label": _sanitize_source_file_label(target_path, settings),
        "local_file_name": target_path.name,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "reused": False,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_fetch_manifest(
        settings, sha256, manifest, update_latest=update_latest
    )
    logger.info(
        "原始文件下载并完整性核验完成: %s (size=%d bytes, sha256=%s)",
        target_path,
        size_bytes,
        sha256,
    )
    return 0, manifest


def data_fetch(
    settings: Settings,
    logger: logging.Logger,
    file_arg: str | None = None,
    data_dir_arg: str | None = None,
    fetch_all: bool = False,
) -> int:
    """获取默认单文件、指定本地文件或全量数据源文件至本地磁盘 (I04, I12)。"""
    if data_dir_arg:
        from dataclasses import replace

        settings = replace(
            settings,
            data_dir=resolve_project_path(data_dir_arg, settings.project_root),
        )

    ensure_runtime_dirs(settings)
    default_target = (settings.data_dir / settings.lade_default_file).resolve()
    default_target.parent.mkdir(parents=True, exist_ok=True)

    # 模式 0：批量获取配置中的全部数据源文件 (--all)，仅落盘校验、不入 Doris
    if fetch_all:
        total_files = len(settings.lade_dataset_files)
        logger.info(
            "启动全量数据源文件获取模式 (--all): 共 %d 份文件，目标根目录=%s，基地址=%s",
            total_files,
            settings.data_dir,
            settings.lade_base_url,
        )
        all_manifests: list[dict[str, Any]] = []
        total_bytes = 0
        for idx, rel_item in enumerate(settings.lade_dataset_files, start=1):
            clean_rel = rel_item.strip().lstrip("/")
            file_url = f"{settings.lade_base_url}/{clean_rel}"
            logger.info(
                "==> [%d/%d] 处理数据源文件: %s",
                idx,
                total_files,
                clean_rel,
            )
            code, m = _download_or_verify_remote_file(
                settings,
                logger,
                rel_file=clean_rel,
                source_url=file_url,
                update_latest=(clean_rel == settings.lade_default_file),
            )
            if code != 0 or not m:
                logger.error(
                    "全量数据源获取在文件 [%d/%d] %s 处中止。",
                    idx,
                    total_files,
                    clean_rel,
                )
                return 1
            all_manifests.append(m)
            total_bytes += int(m["size_bytes"])

        summary_payload = {
            "mode": "fetch_all",
            "base_url": settings.lade_base_url,
            "file_count": len(all_manifests),
            "total_size_bytes": total_bytes,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "files": all_manifests,
        }
        files_dir = settings.manifest_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        (files_dir / "all_files_fetch.json").write_text(
            json.dumps(summary_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(
            "全量数据源文件获取与 SHA-256 校验全部完成: 共 %d 份文件，累计 %d 字节 (%.2f GiB)，清单已保存至 %s",
            len(all_manifests),
            total_bytes,
            total_bytes / (1024**3),
            files_dir / "all_files_fetch.json",
        )
        return 0

    # 模式 1：显式指定已有本地文件 (--file)
    if file_arg:
        src_path = resolve_project_path(file_arg, settings.project_root)
        if not src_path.exists() or not src_path.is_file():
            logger.error("指定的本地原始文件不存在: %s", src_path)
            return 1
        size_bytes = src_path.stat().st_size
        if size_bytes == 0:
            logger.error("指定的本地原始文件为空文件 (0 bytes): %s", src_path)
            return 1

        sha256 = compute_sha256(src_path)
        reused_in_data_dir = True
        if src_path.resolve() != default_target:
            if default_target.exists() and default_target.is_file():
                existing_sha = compute_sha256(default_target)
                if existing_sha == sha256:
                    logger.info(
                        "数据目录中已存在相同 SHA-256 的完整文件，直接复用: %s (sha256=%s)",
                        default_target,
                        sha256,
                    )
                else:
                    logger.info(
                        "保留指定的外部本地文件直接用于核验: %s (sha256=%s)",
                        src_path,
                        sha256,
                    )
            else:
                tmp_copy = default_target.with_name(f".{default_target.name}.copy.part")
                shutil.copy2(src_path, tmp_copy)
                if compute_sha256(tmp_copy) != sha256:
                    tmp_copy.unlink(missing_ok=True)
                    logger.error("复制本地文件至数据目录后校验失败: %s", default_target)
                    return 1
                tmp_copy.replace(default_target)
                reused_in_data_dir = True
                logger.info(
                    "已将指定本地文件校验并归档至数据目录: %s (sha256=%s)",
                    default_target,
                    sha256,
                )
            active_path = src_path.resolve()
        else:
            active_path = default_target
            logger.info(
                "校验并复用数据目录中的已有文件: %s (size=%d, sha256=%s)",
                active_path,
                size_bytes,
                sha256,
            )

        manifest = {
            "source_mode": "local_file",
            "source_url": settings.lade_source_url,
            "source_file_label": _sanitize_source_file_label(active_path, settings),
            "local_file_name": active_path.name,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "reused": reused_in_data_dir,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_fetch_manifest(settings, sha256, manifest)
        return 0

    # 模式 2 & 3：默认单文件校验复用或从官方数据源下载
    code, _ = _download_or_verify_remote_file(
        settings,
        logger,
        rel_file=settings.lade_default_file,
        source_url=settings.lade_source_url,
        update_latest=True,
    )
    return code


def _save_fetch_manifest(
    settings: Settings,
    sha256: str,
    manifest: dict[str, Any],
    update_latest: bool = True,
) -> None:
    files_dir = settings.manifest_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    (files_dir / f"{sha256}.fetch.json").write_text(text, encoding="utf-8")
    if update_latest:
        (files_dir / "latest_fetch.json").write_text(text, encoding="utf-8")


BINARY_ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".zip",
    ".xz",
    ".gz",
    ".bz2",
    ".pkl",
    ".parquet",
)


def _normalize_delimiter(raw_delim: str | None) -> str | None:
    if raw_delim is None:
        return None
    if raw_delim in ("\\t", "tab", "TAB", "\t"):
        return "\t"
    return raw_delim


def data_inspect(
    settings: Settings,
    logger: logging.Logger,
    file_arg: str | None = None,
    encoding: str = "utf-8",
    delimiter: str | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """使用标准库 csv 严格检查分隔文本表（CSV/TSV）的真实表头、编码、列数一致性、空值与 nan 计数 (I05, I08)。"""
    ensure_runtime_dirs(settings)
    target_file = resolve_target_raw_file(settings, file_arg)
    if not target_file.exists() or not target_file.is_file():
        logger.error("待核验的原始文件不存在: %s", target_file)
        return 1, None

    size_bytes = target_file.stat().st_size
    if size_bytes == 0:
        logger.error("待核验的原始文件为空 (0 bytes): %s", target_file)
        return 1, None

    lower_name = target_file.name.lower()
    if any(lower_name.endswith(sfx) for sfx in BINARY_ARCHIVE_SUFFIXES):
        logger.error(
            "文件 %s 属于压缩包或二进制序列化归档（%s），不适用 CSV/TSV 文本表核验入口；请使用专用归档解包或读取工具。",
            target_file.name,
            "".join(target_file.suffixes),
        )
        return 1, None

    sha256 = compute_sha256(target_file)
    source_label = _sanitize_source_file_label(target_file, settings)
    explicit_delim = _normalize_delimiter(delimiter)
    logger.info(
        "开始核验分隔文本表文件: %s (encoding=%s, delimiter=%s, size=%d, sha256=%s)",
        source_label,
        encoding,
        repr(explicit_delim or "auto"),
        size_bytes,
        sha256,
    )

    try:
        with target_file.open("r", encoding=encoding, errors="strict") as f_lines:
            first_line = f_lines.readline()
            physical_line_count = (1 if first_line else 0) + sum(
                1 for _ in f_lines
            )
    except UnicodeDecodeError as exc:
        logger.error(
            "文件编码检查失败（指定编码=%s）: %s", encoding, exc
        )
        return 1, None

    if explicit_delim:
        actual_delim = explicit_delim
    elif "\t" in first_line and "," not in first_line:
        actual_delim = "\t"
    else:
        actual_delim = ","

    header: list[str] = []
    logical_records = 0
    empty_counts: dict[str, int] = {}
    nan_counts: dict[str, int] = {}
    row_hashes: set[bytes] = set()
    duplicate_raw_rows = 0
    sample_first_row: dict[str, str] | None = None

    try:
        with target_file.open(
            "r", encoding=encoding, errors="strict", newline=""
        ) as f_csv:
            reader = csv.reader(f_csv, delimiter=actual_delim)
            try:
                raw_header = next(reader)
            except StopIteration:
                logger.error("CSV 文件不包含表头行: %s", source_label)
                return 1, None

            if not raw_header:
                logger.error("CSV 表头为空列表: %s", source_label)
                return 1, None

            seen_cols: set[str] = set()
            for idx, col in enumerate(raw_header):
                clean_col = col.strip()
                if not clean_col:
                    logger.error("CSV 表头第 %d 列为空列名，停止处理。", idx + 1)
                    return 1, None
                if clean_col in seen_cols:
                    logger.error("CSV 表头存在重复列名 %r，停止处理。", clean_col)
                    return 1, None
                if clean_col in RESERVED_METADATA_COLUMNS:
                    logger.error(
                        "CSV 源列名 %r 与系统接入元数据列冲突，停止处理。",
                        clean_col,
                    )
                    return 1, None
                if not VALID_COLUMN_PATTERN.match(clean_col):
                    logger.error(
                        "CSV 源列名 %r 含有非法标识符字符，停止处理。", clean_col
                    )
                    return 1, None
                seen_cols.add(clean_col)
                header.append(clean_col)
                empty_counts[clean_col] = 0
                nan_counts[clean_col] = 0

            expected_col_count = len(header)
            for row_idx, row in enumerate(reader, start=2):
                if len(row) != expected_col_count:
                    logger.error(
                        "CSV 格式错误: 第 %d 条逻辑记录列数 (%d) 与表头列数 (%d) 不一致，停止处理。",
                        row_idx - 1,
                        len(row),
                        expected_col_count,
                    )
                    return 1, None

                logical_records += 1
                if sample_first_row is None:
                    sample_first_row = dict(zip(header, row))

                row_digest = hashlib.blake2b(
                    "\x1f".join(row).encode("utf-8"), digest_size=16
                ).digest()
                if row_digest in row_hashes:
                    duplicate_raw_rows += 1
                else:
                    row_hashes.add(row_digest)

                for col_name, cell_val in zip(header, row):
                    if cell_val == "":
                        empty_counts[col_name] += 1
                    elif cell_val.strip().lower() == "nan":
                        nan_counts[col_name] += 1

    except (UnicodeDecodeError, csv.Error) as exc:
        logger.error("CSV 解析失败: %s", exc)
        return 1, None

    if logical_records == 0:
        logger.error("CSV 文件仅包含表头，无数据记录 (0 logical rows): %s", source_label)
        return 1, None

    report: dict[str, Any] = {
        "source_file_label": source_label,
        "local_file_name": target_file.name,
        "encoding": encoding,
        "delimiter": actual_delim,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "header_columns": header,
        "column_count": len(header),
        "physical_line_count": physical_line_count,
        "logical_record_count": logical_records,
        "duplicate_raw_row_count": duplicate_raw_rows,
        "empty_string_counts": empty_counts,
        "nan_string_counts": nan_counts,
        "sample_first_record": sample_first_row,
        "inspected_at": datetime.now(timezone.utc).isoformat(),
    }

    inspections_dir = settings.manifest_dir / "inspections"
    inspections_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    (inspections_dir / f"{sha256}.inspect.json").write_text(text, encoding="utf-8")
    (inspections_dir / "latest_inspect.json").write_text(text, encoding="utf-8")

    logger.info(
        "CSV 核验通过: 分隔符=%r, 表头列数=%d, 列名=%s, 物理行数=%d, 逻辑记录数=%d, 完全重复源记录数=%d",
        actual_delim,
        len(header),
        header,
        physical_line_count,
        logical_records,
        duplicate_raw_rows,
    )
    return 0, report


def _load_or_run_inspection(
    settings: Settings, logger: logging.Logger, file_arg: str | None = None
) -> dict[str, Any] | None:
    target_file = resolve_target_raw_file(settings, file_arg)
    if not target_file.exists() or not target_file.is_file():
        logger.error("原始文件不存在，请先执行 data-fetch: %s", target_file)
        return None
    sha256 = compute_sha256(target_file)
    inspect_file = settings.manifest_dir / "inspections" / f"{sha256}.inspect.json"
    if inspect_file.exists():
        data = json.loads(inspect_file.read_text(encoding="utf-8"))
        if data.get("sha256") == sha256 and data.get("header_columns"):
            return data

    code, report = data_inspect(settings, logger, file_arg=str(target_file))
    if code != 0 or not report:
        return None
    return report


def build_bootstrap_ddl(
    database: str, table: str, source_columns: list[str]
) -> str:
    """根据已核验的 CSV 表头生成 Doris 原始接入表 DDL。"""
    col_defs = [
        "    `_ingest_batch_id` VARCHAR(64) NOT NULL COMMENT '导入批次标识'",
        "    `_source_file` VARCHAR(1024) NOT NULL COMMENT '源文件标识（不含敏感本机路径）'",
        "    `_source_sha256` VARCHAR(64) NOT NULL COMMENT '原始文件 SHA-256 校验值'",
        "    `_ingested_at` DATETIMEV2(3) NOT NULL COMMENT '批次接入时间'",
    ]
    for col in source_columns:
        col_defs.append(
            f"    `{col}` STRING NULL COMMENT '源文件原始文本字段: {col}'"
        )
    cols_sql = ",\n".join(col_defs)
    return (
        f"-- 自动生成自已核验的真实文件表头（初次原始接入方案）\n"
        f"CREATE DATABASE IF NOT EXISTS `{database}`;\n\n"
        f"CREATE TABLE IF NOT EXISTS `{database}`.`{table}` (\n"
        f"{cols_sql}\n"
        f")\n"
        f"DUPLICATE KEY(`_ingest_batch_id`)\n"
        f"DISTRIBUTED BY HASH(`_ingest_batch_id`) BUCKETS 1\n"
        f"PROPERTIES (\n"
        f'    "replication_allocation" = "tag.location.default: 1"\n'
        f");\n"
    )


def db_init(
    settings: Settings,
    logger: logging.Logger,
    file_arg: str | None = None,
) -> int:
    """根据已核验的真实表头初始化 Doris 原始接入库表，并核对现有表结构一致性 (I05)。"""
    ensure_runtime_dirs(settings)
    inspection = _load_or_run_inspection(settings, logger, file_arg=file_arg)
    if not inspection:
        return 1

    source_cols: list[str] = list(inspection["header_columns"])
    ddl_text = build_bootstrap_ddl(
        settings.doris_database, settings.doris_table, source_cols
    )
    sql_path = (
        settings.bootstrap_sql_dir
        / f"001_{settings.doris_database}_{settings.doris_table}.sql"
    )
    sql_path.write_text(ddl_text, encoding="utf-8")
    logger.info("已更新建表初始化脚本: %s", sql_path)

    expected_cols = list(RESERVED_METADATA_COLUMNS) + source_cols
    try:
        with connect_doris(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{settings.doris_database}`"
                )
                # 提取 CREATE TABLE 语句执行
                create_table_stmt = ddl_text.split(";", 1)[1].strip().rstrip(";")
                cur.execute(create_table_stmt)

                cur.execute(
                    f"DESC `{settings.doris_database}`.`{settings.doris_table}`"
                )
                desc_rows = list(cur.fetchall())
    except Exception as exc:  # noqa: BLE001
        logger.error("Doris 初始化库表失败: %s", exc)
        return 1

    actual_cols = [str(r.get("Field", "")) for r in desc_rows]
    if actual_cols != expected_cols:
        logger.error(
            "目标表 %s.%s 现有列结构与文件表头不一致（严格保护已有数据，不自动删表重建）:\n期望列=%s\n实际列=%s",
            settings.doris_database,
            settings.doris_table,
            expected_cols,
            actual_cols,
        )
        return 1

    logger.info(
        "Doris 原始接入表初始化与结构核验通过: %s.%s（共 %d 列：4 项接入元数据 + %d 项源字段）",
        settings.doris_database,
        settings.doris_table,
        len(actual_cols),
        len(source_cols),
    )
    return 0


def _rewrite_stream_load_redirect(
    redirect_url: str, settings: Settings
) -> str:
    """若 FE 重定向返回容器内部地址，而当前配置通过本机 127.0.0.1 访问，则改写至本机映射的 BE 端口。"""
    parsed = urlparse(redirect_url)
    if settings.doris_host in ("127.0.0.1", "localhost") and parsed.hostname not in (
        "127.0.0.1",
        "localhost",
    ):
        netloc = f"{settings.doris_host}:{settings.doris_be_http_port}"
        return urlunparse(parsed._replace(netloc=netloc))
    return redirect_url


def _query_target_sha_batches(
    settings: Settings, sha256: str
) -> list[dict[str, Any]]:
    """查询目标表中指定 _source_sha256 已落库的批次及各批次行数。"""
    sql = (
        f"SELECT `_ingest_batch_id` AS batch_id, COUNT(*) AS row_count "
        f"FROM `{settings.doris_database}`.`{settings.doris_table}` "
        f"WHERE `_source_sha256` = %s "
        f"GROUP BY `_ingest_batch_id`"
    )
    with connect_doris(settings, database=settings.doris_database) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (sha256,))
            return list(cur.fetchall())


def _query_doris_label_transaction_state(
    settings: Settings, label: str
) -> str | None:
    """查询 Doris 中指定导入 label 的事务状态（如 PREPARE/COMMITTED/VISIBLE/ABORTED）。"""
    try:
        with connect_doris(settings, database=settings.doris_database) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW TRANSACTION WHERE LABEL = %s", (label,))
                rows = list(cur.fetchall())
                if not rows:
                    return None
                last_row = rows[-1]
                for key in ("TransactionStatus", "Status", "status"):
                    if key in last_row and last_row[key]:
                        return str(last_row[key]).upper()
    except Exception:  # noqa: BLE001
        return None
    return None


def _save_batch_manifest(
    settings: Settings, batch_id: str, manifest: dict[str, Any]
) -> Path:
    batches_dir = settings.manifest_dir / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    path = batches_dir / f"{batch_id}.batch.json"
    path.write_text(text, encoding="utf-8")
    if manifest.get("status") == "SUCCESS":
        (batches_dir / "latest_batch.json").write_text(text, encoding="utf-8")
    return path


_IMPORT_GUARD_LOCK = threading.Lock()
_ACTIVE_IMPORT_KEYS: set[tuple[str, str, str]] = set()


def data_import(
    settings: Settings,
    logger: logging.Logger,
    file_arg: str | None = None,
    batch_id_arg: str | None = None,
    load_url_override: str | None = None,
    abort_unknown_batch: bool = False,
) -> int:
    """通过 Doris Stream Load 原子导入完整原始文件，支持同文件幂等跳过、并发互斥与状态不明核对 (I06, I07, I08)。"""
    ensure_runtime_dirs(settings)
    target_file = resolve_target_raw_file(settings, file_arg)
    inspection = _load_or_run_inspection(settings, logger, file_arg=str(target_file))
    if not inspection:
        return 1

    sha256: str = inspection["sha256"]
    source_label: str = inspection["source_file_label"]
    header_cols: list[str] = list(inspection["header_columns"])
    expected_rows: int = int(inspection["logical_record_count"])
    target_table_id = f"{settings.doris_database}.{settings.doris_table}"
    batches_dir = settings.manifest_dir / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)

    # 进程内 + 文件系统级并发导入互斥锁（防止不同 batch_id 并发导入同一目标与同一文件）
    guard_key = (str(batches_dir.resolve()), target_table_id, sha256)
    safe_target_name = re.sub(r"[^A-Za-z0-9_.-]", "_", target_table_id)
    lock_file = batches_dir / f".import_{safe_target_name}_{sha256[:16]}.lock"
    lock_fd: int | None = None

    with _IMPORT_GUARD_LOCK:
        if guard_key in _ACTIVE_IMPORT_KEYS:
            logger.error(
                "检测到目标表 %s 与文件 SHA-256 (%s) 已有并发导入任务正在执行，拒绝并发重复导入！",
                target_table_id,
                sha256,
            )
            return 1
        try:
            lock_fd = os.open(
                str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            os.write(
                lock_fd,
                f"pid={os.getpid()} batch={batch_id_arg or 'auto'}\n".encode("utf-8"),
            )
        except FileExistsError:
            logger.error(
                "检测到并发导入锁文件已存在 (%s)，同一目标表 %s 与文件 SHA-256 (%s) 正在导入中，拒绝并发重复写入！",
                lock_file.name,
                target_table_id,
                sha256,
            )
            return 1
        _ACTIVE_IMPORT_KEYS.add(guard_key)

    try:
        # 1. 先检查本地是否存在 IN_PROGRESS 或 UNKNOWN 状态的同目标同 SHA-256 批次 (I08)
        existing_manifests: list[dict[str, Any]] = []
        for p in sorted(batches_dir.glob("*.batch.json")):
            if p.name == "latest_batch.json":
                continue
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
                if (
                    m.get("target_table") == target_table_id
                    and m.get("source_sha256") == sha256
                ):
                    existing_manifests.append(m)
            except Exception:  # noqa: BLE001
                continue

        try:
            db_batches = _query_target_sha_batches(settings, sha256)
        except Exception as exc:  # noqa: BLE001
            logger.error("导入前核对 Doris 目标表状态失败: %s", exc)
            return 1

        db_batch_map = {
            str(r["batch_id"]): int(r["row_count"]) for r in db_batches
        }
        total_existing_rows = sum(db_batch_map.values())

        # 处理先前状态不明或未完成的批次记录 (I08)
        for m in existing_manifests:
            m_status = m.get("status")
            m_batch = str(m.get("batch_id", ""))
            if m_status in ("IN_PROGRESS", "UNKNOWN"):
                rows_in_db = db_batch_map.get(m_batch, 0)
                if rows_in_db == expected_rows:
                    logger.info(
                        "原状态不明批次 %s 经核对已在 Doris 完整落库 (%d 行)，修复批次状态为 SUCCESS。",
                        m_batch,
                        rows_in_db,
                    )
                    m["status"] = "SUCCESS"
                    m["verified_rows_in_doris"] = rows_in_db
                    m["reconciled_at"] = datetime.now(timezone.utc).isoformat()
                    _save_batch_manifest(settings, m_batch, m)
                    return 0
                if rows_in_db > 0:
                    logger.error(
                        "检测到原批次 %s 状态为 %s 且目标表存在不完整记录 (%d != 期望 %d)，拒绝自动换标签重传！请先核查清理该异常批次。",
                        m_batch,
                        m_status,
                        rows_in_db,
                        expected_rows,
                    )
                    return 1

                txn_state = _query_doris_label_transaction_state(settings, m_batch)
                if txn_state not in (None, "ABORTED", "UNKNOWN"):
                    logger.error(
                        "检测到原批次 %s 状态为 %s，且 Doris 明确报告该事务状态为 %s（仍在执行或等待发布）；即便指定 --abort-unknown-batch 也严禁放行新批次！",
                        m_batch,
                        m_status,
                        txn_state,
                    )
                    return 1
                if txn_state != "ABORTED" and not abort_unknown_batch:
                    logger.error(
                        "检测到原批次 %s 状态为 %s 且当前可见行数为 0（Doris 事务状态=%s）。原任务可能仍在执行或等待提交，为防止并发重复入库，拒绝直接判定为已终止并生成新批次重传！请等待原任务完成，或在确认原事务已终止后使用 --abort-unknown-batch 显式清理。",
                        m_batch,
                        m_status,
                        txn_state or "UNCONFIRMED",
                    )
                    return 1

                logger.info(
                    "原状态不明批次 %s 经确认已终止且在 Doris 中无残留记录 (0 行, 事务状态=%s)，标记该旧批次为 ABORTED_EMPTY。",
                    m_batch,
                    txn_state or "MANUAL_ABORT_CONFIRMED",
                )
                m["status"] = "ABORTED_EMPTY"
                m["reconciled_at"] = datetime.now(timezone.utc).isoformat()
                _save_batch_manifest(settings, m_batch, m)

        # 2. 同目标＋同文件 SHA-256 幂等跳过检查 (I07)
        if total_existing_rows == expected_rows and len(db_batch_map) == 1:
            existing_batch_id = next(iter(db_batch_map.keys()))
            logger.info(
                "跳过导入 (I07 同文件幂等保护): 目标 %s 已成功导入相同 SHA-256 (%s) 的完整数据（批次=%s, 记录数=%d），不重复追加。",
                target_table_id,
                sha256,
                existing_batch_id,
                total_existing_rows,
            )
            return 0

        if total_existing_rows > 0:
            logger.error(
                "目标表 %s 中已存在该文件 SHA-256 (%s) 的异常记录数（实际=%d, 期望=%d, 批次分布=%s），拒绝静默追加。",
                target_table_id,
                sha256,
                total_existing_rows,
                expected_rows,
                db_batch_map,
            )
            return 1

        # 3. 生成新批次并序列化 JSON Lines 暂存文件
        ts_label = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_id = (
            batch_id_arg
            or f"lade_{settings.doris_table}_{sha256[:12]}_{ts_label}"
        )
        existing_batch_file = batches_dir / f"{batch_id}.batch.json"
        if existing_batch_file.exists():
            try:
                old_bm = json.loads(existing_batch_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                old_bm = {}
            if (
                old_bm.get("target_table") != target_table_id
                or old_bm.get("source_sha256") != sha256
                or old_bm.get("status") in ("IN_PROGRESS", "SUCCESS", "UNKNOWN")
            ):
                logger.error(
                    "指定的批次号 %s 已存在本地清单 (%s, target=%s, status=%s)，拒绝覆盖既有批次记录！",
                    batch_id,
                    existing_batch_file.name,
                    old_bm.get("target_table"),
                    old_bm.get("status"),
                )
                return 1

        ingested_at_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        staging_file = settings.staging_dir / f"{batch_id}.jsonl"

        batch_manifest: dict[str, Any] = {
            "batch_id": batch_id,
            "target_table": target_table_id,
            "source_file_label": source_label,
            "source_sha256": sha256,
            "expected_logical_rows": expected_rows,
            "ingested_at": ingested_at_str,
            "status": "IN_PROGRESS",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_batch_manifest(settings, batch_id, batch_manifest)

        logger.info(
            "正在生成 Stream Load JSON Lines 暂存材料: %s (批次=%s, 期望记录数=%d)",
            staging_file.name,
            batch_id,
            expected_rows,
        )
        written_rows = 0
        try:
            with (
                target_file.open(
                    "r",
                    encoding=inspection.get("encoding", "utf-8"),
                    errors="strict",
                    newline="",
                ) as f_in,
                staging_file.open("w", encoding="utf-8") as f_out,
            ):
                reader = csv.reader(
                    f_in, delimiter=str(inspection.get("delimiter", ","))
                )
                next(reader)  # 跳过已核验表头
                for row in reader:
                    record: dict[str, str] = {
                        "_ingest_batch_id": batch_id,
                        "_source_file": source_label,
                        "_source_sha256": sha256,
                        "_ingested_at": ingested_at_str,
                    }
                    for col_name, val in zip(header_cols, row):
                        record[col_name] = val
                    f_out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    written_rows += 1
        except Exception as exc:  # noqa: BLE001
            staging_file.unlink(missing_ok=True)
            batch_manifest["status"] = "FAILED"
            batch_manifest["stage"] = "serialize_staging"
            batch_manifest["error"] = str(exc)
            _save_batch_manifest(settings, batch_id, batch_manifest)
            logger.error("序列化暂存文件失败: %s", exc)
            return 1

        if written_rows != expected_rows:
            staging_file.unlink(missing_ok=True)
            batch_manifest["status"] = "FAILED"
            batch_manifest["stage"] = "serialize_row_mismatch"
            batch_manifest["error"] = f"written={written_rows} != expected={expected_rows}"
            _save_batch_manifest(settings, batch_id, batch_manifest)
            logger.error(
                "暂存文件记录数 (%d) 与核验记录数 (%d) 不一致！",
                written_rows,
                expected_rows,
            )
            return 1

        # 4. 执行 Doris Stream Load
        base_load_url = (load_url_override or settings.doris_load_url).rstrip("/")
        stream_load_url = (
            f"{base_load_url}/api/{settings.doris_database}/{settings.doris_table}/_stream_load"
        )
        all_cols = list(RESERVED_METADATA_COLUMNS) + header_cols
        columns_header = ", ".join(f"`{c}`" for c in all_cols)
        headers = {
            "Expect": "100-continue",
            "label": batch_id,
            "format": "json",
            "read_json_by_line": "true",
            "strict_mode": "true",
            "max_filter_ratio": "0",
            "columns": columns_header,
        }
        auth = (settings.doris_user, settings.doris_password)

        logger.info(
            "发送 Stream Load 请求: url=%s, label=%s, rows=%d",
            stream_load_url,
            batch_id,
            written_rows,
        )

        resp_json: dict[str, Any] = {}
        try:
            payload_bytes = staging_file.read_bytes()
            with httpx.Client(
                trust_env=False,
                follow_redirects=False,
                timeout=float(settings.doris_load_timeout),
            ) as client:
                resp = client.put(
                    stream_load_url, content=payload_bytes, headers=headers, auth=auth
                )
                if resp.status_code in (301, 302, 307, 308):
                    loc = resp.headers.get("location", "")
                    target_loc = _rewrite_stream_load_redirect(loc, settings)
                    logger.info("Stream Load 重定向至 BE 端点: %s", target_loc)
                    resp = client.put(
                        target_loc, content=payload_bytes, headers=headers, auth=auth
                    )
                resp.raise_for_status()
                resp_json = resp.json()
        except Exception as exc:  # noqa: BLE001
            staging_file.unlink(missing_ok=True)
            batch_manifest["status"] = "UNKNOWN"
            batch_manifest["stage"] = "stream_load_http"
            batch_manifest["error"] = str(exc)
            _save_batch_manifest(settings, batch_id, batch_manifest)
            logger.error(
                "Stream Load 请求异常（批次=%s 已标记为 UNKNOWN，重跑前将先核对原批次状态）: %s",
                batch_id,
                exc,
            )
            return 1
        finally:
            staging_file.unlink(missing_ok=True)

        load_status = str(resp_json.get("Status", ""))
        num_total = int(resp_json.get("NumberTotalRows", -1))
        num_loaded = int(resp_json.get("NumberLoadedRows", -1))
        num_filtered = int(resp_json.get("NumberFilteredRows", -1))
        num_unselected = int(resp_json.get("NumberUnselectedRows", -1))
        batch_manifest["stream_load_response"] = resp_json

        if load_status == "Publish Timeout":
            logger.warning(
                "Stream Load 返回 Publish Timeout，正在轮询核对目标表实际落库状态..."
            )
            time.sleep(5)

        if (
            load_status not in ("Success", "Publish Timeout")
            or num_filtered != 0
            or num_loaded != expected_rows
        ):
            batch_manifest["status"] = "FAILED"
            batch_manifest["stage"] = "stream_load_business_status"
            batch_manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
            _save_batch_manifest(settings, batch_id, batch_manifest)
            logger.error(
                "Stream Load 业务检查未通过: Status=%s, Loaded=%d, Filtered=%d, Expected=%d, Message=%s, ErrorURL=%s",
                load_status,
                num_loaded,
                num_filtered,
                expected_rows,
                resp_json.get("Message"),
                resp_json.get("ErrorURL"),
            )
            return 1

        # 5. 查询 Doris 核实本批次实际可见记录数与唯一性
        verified_rows = 0
        after_map: dict[str, int] = {}
        for _ in range(10):
            db_after = _query_target_sha_batches(settings, sha256)
            after_map = {str(r["batch_id"]): int(r["row_count"]) for r in db_after}
            verified_rows = after_map.get(batch_id, 0)
            if verified_rows == expected_rows:
                break
            time.sleep(1)

        total_sha_rows = sum(after_map.values())
        if verified_rows != expected_rows or len(after_map) != 1 or total_sha_rows != expected_rows:
            batch_manifest["status"] = "UNKNOWN"
            batch_manifest["verified_rows_in_doris"] = verified_rows
            batch_manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
            _save_batch_manifest(settings, batch_id, batch_manifest)
            logger.error(
                "导入后目标表可见行数或批次唯一性异常 (本批次=%d, 同文件总行数=%d, 期望=%d, 批次分布=%s)，批次 %s 保持 UNKNOWN 状态。",
                verified_rows,
                total_sha_rows,
                expected_rows,
                after_map,
                batch_id,
            )
            return 1

        batch_manifest["status"] = "SUCCESS"
        batch_manifest["number_total_rows"] = num_total
        batch_manifest["number_loaded_rows"] = num_loaded
        batch_manifest["number_filtered_rows"] = num_filtered
        batch_manifest["number_unselected_rows"] = num_unselected
        batch_manifest["verified_rows_in_doris"] = verified_rows
        batch_manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        _save_batch_manifest(settings, batch_id, batch_manifest)

        logger.info(
            "Stream Load 整文件导入成功并完成可见性核验: 批次=%s, Status=%s, Loaded=%d, Filtered=%d, Doris 实查行数=%d",
            batch_id,
            load_status,
            num_loaded,
            num_filtered,
            verified_rows,
        )
        return 0
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
        lock_file.unlink(missing_ok=True)
        with _IMPORT_GUARD_LOCK:
            _ACTIVE_IMPORT_KEYS.discard(guard_key)


def data_check(
    settings: Settings,
    logger: logging.Logger,
    file_arg: str | None = None,
    batch_id_arg: str | None = None,
) -> int:
    """核对已导入批次的状态、行数、接入元数据与源字段可查询情况 (I06, I07, I08)。"""
    ensure_runtime_dirs(settings)
    target_file = resolve_target_raw_file(settings, file_arg)
    inspection = _load_or_run_inspection(settings, logger, file_arg=str(target_file))
    if not inspection:
        return 1

    sha256: str = inspection["sha256"]
    expected_rows: int = int(inspection["logical_record_count"])
    header_cols: list[str] = list(inspection["header_columns"])

    try:
        with connect_doris(settings, database=settings.doris_database) as conn:
            with conn.cursor() as cur:
                if batch_id_arg:
                    cur.execute(
                        f"SELECT `_ingest_batch_id`, `_source_file`, `_source_sha256`, "
                        f"MIN(`_ingested_at`) AS min_ingested_at, MAX(`_ingested_at`) AS max_ingested_at, "
                        f"COUNT(*) AS row_count "
                        f"FROM `{settings.doris_database}`.`{settings.doris_table}` "
                        f"WHERE `_ingest_batch_id` = %s "
                        f"GROUP BY `_ingest_batch_id`, `_source_file`, `_source_sha256`",
                        (batch_id_arg,),
                    )
                else:
                    cur.execute(
                        f"SELECT `_ingest_batch_id`, `_source_file`, `_source_sha256`, "
                        f"MIN(`_ingested_at`) AS min_ingested_at, MAX(`_ingested_at`) AS max_ingested_at, "
                        f"COUNT(*) AS row_count "
                        f"FROM `{settings.doris_database}`.`{settings.doris_table}` "
                        f"WHERE `_source_sha256` = %s "
                        f"GROUP BY `_ingest_batch_id`, `_source_file`, `_source_sha256`",
                        (sha256,),
                    )
                summary_rows = list(cur.fetchall())

                if len(summary_rows) != 1:
                    logger.error(
                        "Doris 批次汇总核验异常: 期望唯一有效批次，实际查询到 %d 个批次分组: %s",
                        len(summary_rows),
                        summary_rows,
                    )
                    return 1

                summary = summary_rows[0]
                actual_batch_id = str(summary["_ingest_batch_id"])
                actual_db_sha256 = str(summary.get("_source_sha256", ""))
                actual_rows = int(summary["row_count"])
                target_table_id = f"{settings.doris_database}.{settings.doris_table}"

                if batch_id_arg and actual_batch_id != batch_id_arg:
                    logger.error(
                        "Doris 返回的批次编号 (%s) 与指定 --batch-id (%s) 不一致，核验不通过！",
                        actual_batch_id,
                        batch_id_arg,
                    )
                    return 1

                if actual_db_sha256 != sha256:
                    logger.error(
                        "批次 %s 在 Doris 中记录的源文件 SHA-256 (%s) 与当前待核验文件 SHA-256 (%s) 不一致，核验不通过！",
                        actual_batch_id,
                        actual_db_sha256,
                        sha256,
                    )
                    return 1

                # 检查本地批次清单的目标表、批次编号、状态与文件 SHA-256，防止跨库/跨表误用同名清单
                manifest_file = (
                    settings.manifest_dir / "batches" / f"{actual_batch_id}.batch.json"
                )
                if not manifest_file.exists():
                    logger.error(
                        "缺少对应批次的本地导入清单文件: %s", manifest_file
                    )
                    return 1
                batch_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                manifest_batch_id = str(batch_manifest.get("batch_id", ""))
                manifest_target_table = str(batch_manifest.get("target_table", ""))
                manifest_sha256 = str(batch_manifest.get("source_sha256", ""))
                if manifest_batch_id != actual_batch_id or (
                    batch_id_arg and manifest_batch_id != batch_id_arg
                ):
                    logger.error(
                        "批次清单内的 batch_id (%s) 与当前核验批次 (%s) 不一致，核验不通过！",
                        manifest_batch_id,
                        actual_batch_id,
                    )
                    return 1
                if manifest_target_table != target_table_id:
                    logger.error(
                        "批次 %s 的本地清单目标表 (%s) 与当前核验目标表 (%s) 不一致，拒绝跨库/跨表误用同名清单！",
                        actual_batch_id,
                        manifest_target_table,
                        target_table_id,
                    )
                    return 1
                if manifest_sha256 != sha256:
                    logger.error(
                        "批次 %s 的本地清单 SHA-256 (%s) 与当前待核验文件 SHA-256 (%s) 不一致，核验不通过！",
                        actual_batch_id,
                        manifest_sha256,
                        sha256,
                    )
                    return 1
                if batch_manifest.get("status") != "SUCCESS":
                    logger.error(
                        "批次 %s 的清单状态为 %s（非 SUCCESS），核验不通过！",
                        actual_batch_id,
                        batch_manifest.get("status"),
                    )
                    return 1

                if actual_rows != expected_rows:
                    logger.error(
                        "记录数核对不一致: Doris 实际行数=%d != CSV 逻辑记录数=%d",
                        actual_rows,
                        expected_rows,
                    )
                    return 1

                # 统计各源字段在 Doris 中的空字符串与非空值数量
                non_empty_exprs = [
                    f"SUM(CASE WHEN `{c}` IS NOT NULL AND `{c}` <> '' THEN 1 ELSE 0 END) AS `{c}_non_empty`"
                    for c in header_cols
                ]
                stats_sql = (
                    f"SELECT {', '.join(non_empty_exprs)} "
                    f"FROM `{settings.doris_database}`.`{settings.doris_table}` "
                    f"WHERE `_ingest_batch_id` = %s"
                )
                cur.execute(stats_sql, (actual_batch_id,))
                col_stats = cur.fetchone() or {}

                cur.execute(
                    f"SELECT * FROM `{settings.doris_database}`.`{settings.doris_table}` "
                    f"WHERE `_ingest_batch_id` = %s LIMIT 3",
                    (actual_batch_id,),
                )
                sample_rows = list(cur.fetchall())

    except Exception as exc:  # noqa: BLE001
        logger.error("Doris 数据核验查询失败: %s", exc)
        return 1

    # 对照 CSV 核验时的非空数与 Doris 落库后的非空数
    expected_empty_counts: dict[str, int] = inspection.get(
        "empty_string_counts", {}
    )
    for c in header_cols:
        actual_non_empty = int(col_stats.get(f"{c}_non_empty", 0))
        expected_non_empty = expected_rows - int(expected_empty_counts.get(c, 0))
        if actual_non_empty != expected_non_empty:
            logger.error(
                "字段 %r 非空记录数不一致: Doris=%d != 文件核验=%d",
                c,
                actual_non_empty,
                expected_non_empty,
            )
            return 1

    check_report = {
        "target_table": f"{settings.doris_database}.{settings.doris_table}",
        "batch_id": actual_batch_id,
        "source_file": summary["_source_file"],
        "source_sha256": summary["_source_sha256"],
        "expected_logical_rows": expected_rows,
        "doris_row_count": actual_rows,
        "min_ingested_at": str(summary["min_ingested_at"]),
        "max_ingested_at": str(summary["max_ingested_at"]),
        "column_non_empty_counts": {
            c: int(col_stats.get(f"{c}_non_empty", 0)) for c in header_cols
        },
        "sample_rows": [
            {k: str(v) for k, v in r.items()} for r in sample_rows
        ],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASSED",
    }
    check_path = (
        settings.manifest_dir / "batches" / f"{actual_batch_id}.check.json"
    )
    text = json.dumps(check_report, ensure_ascii=False, indent=2)
    check_path.write_text(text, encoding="utf-8")
    (settings.manifest_dir / "batches" / "latest_check.json").write_text(
        text, encoding="utf-8"
    )

    logger.info(
        "数据核验 (data-check) 全部通过: 表=%s.%s, 批次=%s, SHA-256=%s, 记录数=%d == %d, 全字段非空计数 100%% 吻合。",
        settings.doris_database,
        settings.doris_table,
        actual_batch_id,
        sha256,
        actual_rows,
        expected_rows,
    )
    return 0
