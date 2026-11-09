"""Docker Compose 服务编排、Metabase Doris 驱动下载校验与生命周期管理。"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from warehouse.config import Settings, ensure_runtime_dirs
from warehouse.db import ensure_be_registered, get_cluster_status

DRIVER_VERSION = "v1.0.0"
DRIVER_FILENAME = "doris.metabase-driver-v1.0.0.jar"
DRIVER_SHA256_FILENAME = "doris.metabase-driver-v1.0.0.jar.sha256"
DRIVER_RELEASE_BASE = (
    f"https://github.com/velodb/metabase-doris-driver/releases/download/{DRIVER_VERSION}"
)
DRIVER_URL = f"{DRIVER_RELEASE_BASE}/{DRIVER_FILENAME}"
DRIVER_SHA256_URL = f"{DRIVER_RELEASE_BASE}/{DRIVER_SHA256_FILENAME}"


def compute_sha256(file_path: Path) -> str:
    """计算本地文件的 SHA-256 十六进制摘要。"""
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest().lower()


def parse_sha256_text(text: str) -> str:
    """从 .sha256 校验文件内容中提取 64 位十六进制哈希值。"""
    for token in text.strip().split():
        clean = token.strip().lower()
        if len(clean) == 64 and all(c in "0123456789abcdef" for c in clean):
            return clean
    raise ValueError(f"无法从校验内容中解析有效的 SHA-256 值: {text!r}")


def ensure_metabase_driver(
    settings: Settings,
    logger: logging.Logger,
) -> dict[str, Any]:
    """确保 Metabase Doris 社区驱动已下载并通过官方 SHA-256 校验。"""
    ensure_runtime_dirs(settings)
    jar_path = settings.plugins_dir / DRIVER_FILENAME
    sha_path = settings.plugins_dir / DRIVER_SHA256_FILENAME
    manifest_path = settings.manifest_dir / "files" / "metabase_doris_driver.json"

    if jar_path.is_dir():
        raise RuntimeError(
            f"检测到驱动目标路径为目录而非文件: {jar_path}，请先清理异常目录。"
        )

    expected_sha256: str | None = None
    if sha_path.exists() and sha_path.is_file():
        expected_sha256 = parse_sha256_text(sha_path.read_text(encoding="utf-8"))

    if jar_path.exists() and jar_path.is_file() and expected_sha256:
        actual_sha = compute_sha256(jar_path)
        if actual_sha == expected_sha256:
            logger.info(
                "复用已校验的 Metabase Doris 驱动: %s (sha256=%s)",
                jar_path.name,
                actual_sha,
            )
            info = {
                "driver_name": DRIVER_FILENAME,
                "version": DRIVER_VERSION,
                "source_url": DRIVER_URL,
                "sha256_url": DRIVER_SHA256_URL,
                "sha256": actual_sha,
                "size_bytes": jar_path.stat().st_size,
                "reused": True,
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }
            manifest_path.write_text(
                json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return info
        logger.warning(
            "本地驱动文件 SHA-256 不匹配（期望=%s，实际=%s），将重新下载。",
            expected_sha256,
            actual_sha,
        )

    logger.info("正在获取驱动校验文件: %s", DRIVER_SHA256_URL)
    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        sha_resp = client.get(DRIVER_SHA256_URL)
        sha_resp.raise_for_status()
        sha_text = sha_resp.text
        expected_sha256 = parse_sha256_text(sha_text)
        sha_path.write_text(sha_text, encoding="utf-8")

        tmp_jar = settings.plugins_dir / f".{DRIVER_FILENAME}.part"
        logger.info("正在下载 Metabase Doris 驱动: %s", DRIVER_URL)
        with client.stream("GET", DRIVER_URL) as resp:
            resp.raise_for_status()
            with tmp_jar.open("wb") as out:
                for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                    out.write(chunk)

    actual_sha = compute_sha256(tmp_jar)
    if actual_sha != expected_sha256:
        tmp_jar.unlink(missing_ok=True)
        raise RuntimeError(
            f"Metabase Doris 驱动校验失败: 期望 SHA-256={expected_sha256}，实际={actual_sha}"
        )

    tmp_jar.replace(jar_path)
    info = {
        "driver_name": DRIVER_FILENAME,
        "version": DRIVER_VERSION,
        "source_url": DRIVER_URL,
        "sha256_url": DRIVER_SHA256_URL,
        "sha256": actual_sha,
        "size_bytes": jar_path.stat().st_size,
        "reused": False,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path.write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "Metabase Doris 驱动下载并校验通过: %s (size=%d, sha256=%s)",
        jar_path.name,
        info["size_bytes"],
        actual_sha,
    )
    return info


def _compose_base_cmd(settings: Settings) -> list[str]:
    cmd = ["docker", "compose"]
    if settings.env_file.exists():
        cmd.extend(["--env-file", str(settings.env_file)])
    cmd.extend(["-f", str(settings.compose_file)])
    return cmd


def services_up(settings: Settings, logger: logging.Logger) -> int:
    """启动容器服务（保留既有数据卷）并等待 Doris 集群就绪。"""
    ensure_runtime_dirs(settings)
    if not settings.compose_file.exists():
        logger.error("未找到容器编排文件: %s", settings.compose_file)
        return 1

    ensure_metabase_driver(settings, logger)

    cmd = _compose_base_cmd(settings) + ["up", "-d"]
    logger.info("执行容器启动命令: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        cwd=settings.project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.stdout.strip():
        logger.info("compose 输出:\n%s", proc.stdout.strip())
    if proc.stderr.strip():
        logger.info("compose 状态信息:\n%s", proc.stderr.strip())
    if proc.returncode != 0:
        logger.error("容器服务启动失败，退出码: %d", proc.returncode)
        return proc.returncode

    logger.info("等待 Doris FE 与 BE 节点就绪并完成注册核验...")
    cluster = ensure_be_registered(settings, logger)
    logger.info(
        "Doris 集群已就绪: FE Alive=%s, BE Alive=%s (FE=%d, BE=%d)",
        cluster["fe_alive"],
        cluster["be_alive"],
        len(cluster["frontends"]),
        len(cluster["backends"]),
    )
    return 0


def services_down(settings: Settings, logger: logging.Logger) -> int:
    """停止容器服务，严格保留数据卷与本地文件（不附加 -v）。"""
    if not settings.compose_file.exists():
        logger.error("未找到容器编排文件: %s", settings.compose_file)
        return 1

    cmd = _compose_base_cmd(settings) + ["down"]
    logger.info("执行容器停止命令（保留持久化数据卷）: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        cwd=settings.project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.stdout.strip():
        logger.info("compose 输出:\n%s", proc.stdout.strip())
    if proc.stderr.strip():
        logger.info("compose 状态信息:\n%s", proc.stderr.strip())
    if proc.returncode != 0:
        logger.error("容器服务停止失败，退出码: %d", proc.returncode)
        return proc.returncode

    logger.info("容器服务已停止，持久化数据卷与本地文件均已保留。")
    return 0


def services_status(settings: Settings, logger: logging.Logger) -> int:
    """查看容器服务与 Doris 节点状态，不修改任何业务数据。"""
    if not settings.compose_file.exists():
        logger.error("未找到容器编排文件: %s", settings.compose_file)
        return 1

    cmd = _compose_base_cmd(settings) + ["ps"]
    proc = subprocess.run(
        cmd,
        cwd=settings.project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.stdout.strip():
        logger.info("当前容器列表:\n%s", proc.stdout.strip())
    if proc.stderr.strip():
        logger.warning("compose 提示:\n%s", proc.stderr.strip())
    if proc.returncode != 0:
        return proc.returncode

    try:
        cluster = get_cluster_status(settings)
        logger.info(
            "Doris 状态: FE Alive=%s, BE Alive=%s",
            cluster["fe_alive"],
            cluster["be_alive"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Doris 查询连接暂未就绪: %s", exc)
    return 0
