"""项目配置加载、路径解析与脱敏日志管理模块。"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_ENV_KEYS: tuple[str, ...] = (
    "DATA_DIR",
    "MANIFEST_DIR",
    "LOG_DIR",
    "DORIS_HOST",
    "DORIS_QUERY_PORT",
    "DORIS_DATABASE",
    "DORIS_LOAD_URL",
    "DORIS_LOAD_TIMEOUT",
    "DORIS_USER",
    "DORIS_PASSWORD",
    "STREAMLIT_PORT",
    "METABASE_PORT",
    "MB_DB_TYPE",
    "MB_DB_HOST",
    "MB_DB_PORT",
    "MB_DB_DBNAME",
    "MB_DB_USER",
    "MB_DB_PASS",
    "MB_ADMIN_EMAIL",
    "MB_ADMIN_PASS",
)

# 允许为空字符串但键本身必须声明的配置项（如本地默认无密码的 Doris root）
ALLOW_EMPTY_ENV_KEYS: frozenset[str] = frozenset({"DORIS_PASSWORD"})

DEFAULT_LADE_BASE_URL = "https://huggingface.co/datasets/Cainiao-AI/LaDe/resolve/main"
DEFAULT_LADE_DATASET_FILES: tuple[str, ...] = (
    "delivery/delivery_jl.csv",
    "delivery/delivery_yt.csv",
    "delivery/delivery_cq.csv",
    "delivery/delivery_sh.csv",
    "delivery/delivery_hz.csv",
    "pickup/pickup_jl.csv",
    "pickup/pickup_yt.csv",
    "pickup/pickup_cq.csv",
    "pickup/pickup_sh.csv",
    "pickup/pickup_hz.csv",
    "road-network/roads.csv",
    "delivery_five_cities.csv",
    "pickup_five_cities.csv",
    "courier_detailed_trajectory.zip",
    "data_with_trajectory_20s/delivery_five_cities.pkl.xz",
    "data_with_trajectory_20s/pickup_five_cities.pkl.xz",
    "data_with_trajectory_20s/courier_detailed_trajectory_20s.pkl.xz",
)


class SecretRedactingFilter(logging.Filter):
    """在日志输出中自动屏蔽密码与敏感凭据。"""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 3]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        msg = record.getMessage()
        redacted = msg
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, "***REDACTED***")
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def resolve_project_path(raw_path: str, project_root: Path = PROJECT_ROOT) -> Path:
    """将相对路径解析为相对于项目根目录的绝对路径，绝对路径保持不变。"""
    p = Path(raw_path).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (project_root / p).resolve()


@dataclass(frozen=True)
class Settings:
    """项目运行配置快照。"""

    project_root: Path
    env_file: Path
    data_dir: Path
    manifest_dir: Path
    staging_dir: Path
    log_dir: Path
    plugins_dir: Path
    compose_file: Path
    bootstrap_sql_dir: Path

    lade_base_url: str
    lade_source_url: str
    lade_default_file: str
    lade_dataset_files: tuple[str, ...]
    lade_download_timeout: int

    doris_host: str
    doris_query_port: int
    doris_fe_http_port: int
    doris_be_http_port: int
    doris_database: str
    doris_table: str
    doris_user: str
    doris_password: str
    doris_load_url: str
    doris_load_timeout: int

    streamlit_host: str
    streamlit_port: int
    metabase_host: str
    metabase_port: int

    mb_db_type: str
    mb_db_host: str
    mb_db_port: int
    mb_db_dbname: str
    mb_db_user: str
    mb_db_pass: str
    mb_admin_email: str
    mb_admin_pass: str

    missing_env_keys: tuple[str, ...]


def load_settings(
    env_file: Path | None = None,
    data_dir_override: str | None = None,
) -> Settings:
    """显式加载项目根目录 .env（已有进程环境变量优先）并构建配置对象。"""
    target_env = env_file or (PROJECT_ROOT / ".env")
    file_kv: dict[str, str | None] = {}
    if target_env.exists():
        file_kv = dotenv_values(target_env)
        load_dotenv(dotenv_path=target_env, override=False)

    missing: list[str] = []
    for key in REQUIRED_ENV_KEYS:
        in_os = key in os.environ
        in_file = key in file_kv and file_kv[key] is not None
        if not in_os and not in_file:
            missing.append(key)
            continue
        val = os.environ.get(key, "")
        if key not in ALLOW_EMPTY_ENV_KEYS and val.strip() == "":
            missing.append(key)

    raw_data_dir = data_dir_override or os.environ.get("DATA_DIR", "data/raw")
    raw_manifest_dir = os.environ.get("MANIFEST_DIR", "data/manifests")
    raw_staging_dir = os.environ.get("STAGING_DIR", "data/staging")
    raw_log_dir = os.environ.get("LOG_DIR", "logs")

    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            missing.append(name)
            return default

    raw_dataset_files = os.environ.get("LADE_DATASET_FILES", "").strip()
    if raw_dataset_files:
        parsed_dataset_files = tuple(
            item.strip().lstrip("/")
            for item in raw_dataset_files.split(",")
            if item.strip()
        )
    else:
        parsed_dataset_files = DEFAULT_LADE_DATASET_FILES

    return Settings(
        project_root=PROJECT_ROOT,
        env_file=target_env,
        data_dir=resolve_project_path(raw_data_dir),
        manifest_dir=resolve_project_path(raw_manifest_dir),
        staging_dir=resolve_project_path(raw_staging_dir),
        log_dir=resolve_project_path(raw_log_dir),
        plugins_dir=PROJECT_ROOT / "deploy" / "plugins",
        compose_file=PROJECT_ROOT / "deploy" / "compose.yaml",
        bootstrap_sql_dir=PROJECT_ROOT / "sql" / "bootstrap",
        lade_base_url=os.environ.get(
            "LADE_BASE_URL", DEFAULT_LADE_BASE_URL
        ).strip().rstrip("/"),
        lade_source_url=os.environ.get(
            "LADE_SOURCE_URL",
            f"{DEFAULT_LADE_BASE_URL}/delivery/delivery_jl.csv",
        ).strip(),
        lade_default_file=os.environ.get(
            "LADE_DEFAULT_FILE", "delivery/delivery_jl.csv"
        ).strip(),
        lade_dataset_files=parsed_dataset_files,
        lade_download_timeout=_int_env("LADE_DOWNLOAD_TIMEOUT", 120),
        doris_host=os.environ.get("DORIS_HOST", "127.0.0.1").strip(),
        doris_query_port=_int_env("DORIS_QUERY_PORT", 9030),
        doris_fe_http_port=_int_env("DORIS_FE_HTTP_PORT", 8030),
        doris_be_http_port=_int_env("DORIS_BE_HTTP_PORT", 8040),
        doris_database=os.environ.get("DORIS_DATABASE", "raw_lade").strip(),
        doris_table=os.environ.get("DORIS_TABLE", "delivery_jl").strip(),
        doris_user=os.environ.get("DORIS_USER", "root").strip(),
        doris_password=os.environ.get("DORIS_PASSWORD", ""),
        doris_load_url=os.environ.get(
            "DORIS_LOAD_URL", "http://127.0.0.1:8040"
        ).strip(),
        doris_load_timeout=_int_env("DORIS_LOAD_TIMEOUT", 120),
        streamlit_host=os.environ.get("STREAMLIT_HOST", "127.0.0.1").strip(),
        streamlit_port=_int_env("STREAMLIT_PORT", 8501),
        metabase_host=os.environ.get("METABASE_HOST", "127.0.0.1").strip(),
        metabase_port=_int_env("METABASE_PORT", 3000),
        mb_db_type=os.environ.get("MB_DB_TYPE", "postgres").strip(),
        mb_db_host=os.environ.get("MB_DB_HOST", "postgres").strip(),
        mb_db_port=_int_env("MB_DB_PORT", 5432),
        mb_db_dbname=os.environ.get("MB_DB_DBNAME", "metabase_app").strip(),
        mb_db_user=os.environ.get("MB_DB_USER", "metabase").strip(),
        mb_db_pass=os.environ.get("MB_DB_PASS", ""),
        mb_admin_email=os.environ.get("MB_ADMIN_EMAIL", "").strip(),
        mb_admin_pass=os.environ.get("MB_ADMIN_PASS", "").strip(),
        missing_env_keys=tuple(dict.fromkeys(missing)),
    )


def ensure_runtime_dirs(settings: Settings) -> None:
    """确保运行所需的本地目录存在。"""
    for d in (
        settings.data_dir,
        settings.manifest_dir,
        settings.manifest_dir / "files",
        settings.manifest_dir / "inspections",
        settings.manifest_dir / "batches",
        settings.staging_dir,
        settings.log_dir,
        settings.plugins_dir,
        settings.bootstrap_sql_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)


def setup_task_logger(task_name: str, settings: Settings) -> tuple[logging.Logger, Path]:
    """初始化按任务批次隔离的日志记录器，并挂载密码脱敏过滤器。"""
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_task = task_name.replace("-", "_")
    log_file = settings.log_dir / f"{safe_task}_{ts}.log"

    logger = logging.getLogger(f"warehouse.{safe_task}.{ts}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    redact_filter = SecretRedactingFilter(
        [settings.doris_password, settings.mb_db_pass, settings.mb_admin_pass]
    )
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redact_filter)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redact_filter)

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    return logger, log_file
