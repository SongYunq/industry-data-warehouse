"""原始文件获取、CSV 格式与表头核验、Doris 建表初始化、Stream Load 原子导入与结果核对模块。"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import shutil
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


def data_fetch(
    settings: Settings,
    logger: logging.Logger,
    file_arg: str | None = None,
    data_dir_arg: str | None = None,
) -> int:
    """获取默认完整吉林派件原始文件，或校验并复用已存在的本地文件 (I04, I12)。"""
    if data_dir_arg:
        from dataclasses import replace

        settings = replace(
            settings,
            data_dir=resolve_project_path(data_dir_arg, settings.project_root),
        )

    ensure_runtime_dirs(settings)
    default_target = (settings.data_dir / settings.lade_default_file).resolve()
    default_target.parent.mkdir(parents=True, exist_ok=True)

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

    # 模式 2：默认目标文件已存在时，先校验完整性并复用
    if default_target.exists() and default_target.is_file():
        size_bytes = default_target.stat().st_size
        if size_bytes > 0:
            sha256 = compute_sha256(default_target)
            logger.info(
                "检测到本地已存在完整原始文件，校验后直接复用: %s (size=%d, sha256=%s)",
                default_target,
                size_bytes,
                sha256,
            )
            manifest = {
                "source_mode": "reused_existing",
                "source_url": settings.lade_source_url,
                "source_file_label": _sanitize_source_file_label(
                    default_target, settings
                ),
                "local_file_name": default_target.name,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "reused": True,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_fetch_manifest(settings, sha256, manifest)
            return 0

    # 模式 3：从上游官方仓库下载到临时文件，校验通过后原子替换正式文件
    tmp_file = default_target.with_name(f".{default_target.name}.part")
    tmp_file.unlink(missing_ok=True)
    logger.info(
        "开始从官方数据源下载原始文件: %s -> 临时文件 %s",
        settings.lade_source_url,
        tmp_file.name,
    )
    try:
        with httpx.Client(follow_redirects=True, timeout=180.0) as client:
            with client.stream("GET", settings.lade_source_url) as resp:
                resp.raise_for_status()
                expected_len_raw = resp.headers.get("content-length")
                expected_len = (
                    int(expected_len_raw)
                    if expected_len_raw and expected_len_raw.isdigit()
                    else None
                )
                written = 0
                with tmp_file.open("wb") as out:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                        out.write(chunk)
                        written += len(chunk)

        if written == 0:
            tmp_file.unlink(missing_ok=True)
            logger.error("下载文件大小为 0 字节，已清理临时文件。")
            return 1

        if expected_len is not None and written != expected_len:
            tmp_file.unlink(missing_ok=True)
            logger.error(
                "下载文件字节数不完整（期望=%d，实际=%d），已清理临时文件，不覆盖正式文件。",
                expected_len,
                written,
            )
            return 1

        sha256 = compute_sha256(tmp_file)
        tmp_file.replace(default_target)
    except Exception as exc:  # noqa: BLE001
        tmp_file.unlink(missing_ok=True)
        logger.error(
            "下载原始文件异常中断（已清理临时文件，未破坏既有正式文件）: %s", exc
        )
        return 1

    size_bytes = default_target.stat().st_size
    manifest = {
        "source_mode": "downloaded",
        "source_url": settings.lade_source_url,
        "source_file_label": _sanitize_source_file_label(default_target, settings),
        "local_file_name": default_target.name,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "reused": False,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_fetch_manifest(settings, sha256, manifest)
    logger.info(
        "原始文件下载并完整性核验完成: %s (size=%d bytes, sha256=%s)",
        default_target,
        size_bytes,
        sha256,
    )
    return 0


def _save_fetch_manifest(
    settings: Settings, sha256: str, manifest: dict[str, Any]
) -> None:
    files_dir = settings.manifest_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    (files_dir / f"{sha256}.fetch.json").write_text(text, encoding="utf-8")
    (files_dir / "latest_fetch.json").write_text(text, encoding="utf-8")


def data_inspect(
    settings: Settings,
    logger: logging.Logger,
    file_arg: str | None = None,
    encoding: str = "utf-8",
) -> tuple[int, dict[str, Any] | None]:
    """使用标准库 csv 严格检查真实表头、编码、列数一致性与逻辑记录数 (I05, I08)。"""
    ensure_runtime_dirs(settings)
    target_file = resolve_target_raw_file(settings, file_arg)
    if not target_file.exists() or not target_file.is_file():
        logger.error("待核验的原始文件不存在: %s", target_file)
        return 1, None

    size_bytes = target_file.stat().st_size
    if size_bytes == 0:
        logger.error("待核验的原始文件为空 (0 bytes): %s", target_file)
        return 1, None

    sha256 = compute_sha256(target_file)
    source_label = _sanitize_source_file_label(target_file, settings)
    logger.info(
        "开始核验 CSV 文件: %s (encoding=%s, size=%d, sha256=%s)",
        source_label,
        encoding,
        size_bytes,
        sha256,
    )

    try:
        with target_file.open("r", encoding=encoding, errors="strict") as f_lines:
            physical_line_count = sum(1 for _ in f_lines)
    except UnicodeDecodeError as exc:
        logger.error(
            "文件编码检查失败（指定编码=%s）: %s", encoding, exc
        )
        return 1, None

    header: list[str] = []
    logical_records = 0
    empty_counts: dict[str, int] = {}
    row_hashes: set[bytes] = set()
    duplicate_raw_rows = 0
    sample_first_row: dict[str, str] | None = None

    try:
        with target_file.open(
            "r", encoding=encoding, errors="strict", newline=""
        ) as f_csv:
            reader = csv.reader(f_csv)
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
        "size_bytes": size_bytes,
        "sha256": sha256,
        "header_columns": header,
        "column_count": len(header),
        "physical_line_count": physical_line_count,
        "logical_record_count": logical_records,
        "duplicate_raw_row_count": duplicate_raw_rows,
        "empty_string_counts": empty_counts,
        "sample_first_record": sample_first_row,
        "inspected_at": datetime.now(timezone.utc).isoformat(),
    }

    inspections_dir = settings.manifest_dir / "inspections"
    inspections_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    (inspections_dir / f"{sha256}.inspect.json").write_text(text, encoding="utf-8")
    (inspections_dir / "latest_inspect.json").write_text(text, encoding="utf-8")

    logger.info(
        "CSV 核验通过: 表头列数=%d, 列名=%s, 物理行数=%d, 逻辑记录数=%d, 完全重复源记录数=%d",
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


def data_import(
    settings: Settings,
    logger: logging.Logger,
    file_arg: str | None = None,
    batch_id_arg: str | None = None,
    load_url_override: str | None = None,
) -> int:
    """通过 Doris Stream Load 原子导入完整原始文件，支持同文件幂等跳过与状态不明核对 (I06, I07, I08)。"""
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

    # 1. 先检查本地是否存在 IN_PROGRESS 或 UNKNOWN 状态的同目标同 SHA-256 批次 (I08)
    batches_dir = settings.manifest_dir / "batches"
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
            logger.info(
                "经核对原状态不明批次 %s 在 Doris 中无残留记录 (0 行)，标记该旧批次为 ABORTED_EMPTY。",
                m_batch,
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
            reader = csv.reader(f_in)
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

    # 5. 查询 Doris 核实本批次实际可见记录数
    verified_rows = 0
    for _ in range(10):
        db_after = _query_target_sha_batches(settings, sha256)
        after_map = {str(r["batch_id"]): int(r["row_count"]) for r in db_after}
        verified_rows = after_map.get(batch_id, 0)
        if verified_rows == expected_rows:
            break
        time.sleep(1)

    if verified_rows != expected_rows:
        batch_manifest["status"] = "UNKNOWN"
        batch_manifest["verified_rows_in_doris"] = verified_rows
        batch_manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        _save_batch_manifest(settings, batch_id, batch_manifest)
        logger.error(
            "导入后目标表可见行数 (%d) 与期望记录数 (%d) 不符，批次 %s 保持 UNKNOWN 状态。",
            verified_rows,
            expected_rows,
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
                actual_rows = int(summary["row_count"])

                # 检查本地批次清单状态，禁止将状态不明或失败任务判为成功
                manifest_file = (
                    settings.manifest_dir / "batches" / f"{actual_batch_id}.batch.json"
                )
                if not manifest_file.exists():
                    logger.error(
                        "缺少对应批次的本地导入清单文件: %s", manifest_file
                    )
                    return 1
                batch_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
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
