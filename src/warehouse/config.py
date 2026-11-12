"""Shared runtime settings for Docker Compose, Doris, and Metabase services."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SecretRedactingFilter(logging.Filter):
    """Redact configured credentials from service logs."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret and len(secret) >= 3]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "***REDACTED***")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


@dataclass(frozen=True)
class Settings:
    project_root: Path
    env_file: Path
    manifest_dir: Path
    log_dir: Path
    plugins_dir: Path
    compose_file: Path
    doris_host: str
    doris_query_port: int
    doris_fe_http_port: int
    doris_be_http_port: int
    doris_user: str
    doris_password: str
    mb_db_pass: str


def load_settings(env_file: Path | None = None) -> Settings:
    """Load project-local settings while allowing process variables to override them."""
    target_env = env_file or PROJECT_ROOT / ".env"
    if target_env.exists():
        load_dotenv(dotenv_path=target_env, override=False)

    def int_env(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        return int(raw) if raw else default

    return Settings(
        project_root=PROJECT_ROOT,
        env_file=target_env,
        manifest_dir=PROJECT_ROOT / os.environ.get("MANIFEST_DIR", "data/manifests"),
        log_dir=PROJECT_ROOT / os.environ.get("LOG_DIR", "logs"),
        plugins_dir=PROJECT_ROOT / "deploy" / "plugins",
        compose_file=PROJECT_ROOT / "deploy" / "compose.yaml",
        doris_host=os.environ.get("DORIS_HOST", "127.0.0.1").strip(),
        doris_query_port=int_env("DORIS_QUERY_PORT", 9030),
        doris_fe_http_port=int_env("DORIS_FE_HTTP_PORT", 8030),
        doris_be_http_port=int_env("DORIS_BE_HTTP_PORT", 8040),
        doris_user=os.environ.get("DORIS_USER", "root").strip(),
        doris_password=os.environ.get("DORIS_PASSWORD", ""),
        mb_db_pass=os.environ.get("MB_DB_PASS", ""),
    )


def ensure_runtime_dirs(settings: Settings) -> None:
    """Create runtime directories used by the shared service tooling."""
    for directory in (settings.manifest_dir, settings.plugins_dir, settings.log_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (settings.manifest_dir / "files").mkdir(parents=True, exist_ok=True)


def setup_task_logger(task_name: str, settings: Settings) -> tuple[logging.Logger, Path]:
    """Create a timestamped console/file logger with credential redaction."""
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_task = task_name.replace("-", "_")
    log_file = settings.log_dir / f"{safe_task}_{timestamp}.log"
    logger = logging.getLogger(f"warehouse.{safe_task}.{timestamp}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    redactor = SecretRedactingFilter((settings.doris_password, settings.mb_db_pass))
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")):
        handler.setFormatter(formatter)
        handler.addFilter(redactor)
        logger.addHandler(handler)
    return logger, log_file
