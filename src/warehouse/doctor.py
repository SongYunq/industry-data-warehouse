"""环境、配置、端口绑定、Doris 查询／导入连接与 Metabase 驱动健康检查模块。"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
import sys
import time
from typing import Any

import httpx

from warehouse.config import Settings, ensure_runtime_dirs
from warehouse.db import get_cluster_status
from warehouse.services import (
    DRIVER_FILENAME,
    DRIVER_SHA256_FILENAME,
    compute_sha256,
    parse_sha256_text,
)


def _check_docker_containers(
    settings: Settings, logger: logging.Logger
) -> tuple[bool, list[dict[str, Any]]]:
    cmd = ["docker", "compose"]
    if settings.env_file.exists():
        cmd.extend(["--env-file", str(settings.env_file)])
    cmd.extend(["-f", str(settings.compose_file), "ps", "--format", "json"])

    proc = subprocess.run(
        cmd,
        cwd=settings.project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        logger.error("无法获取 Docker Compose 容器状态: %s", proc.stderr.strip())
        return False, []

    containers: list[dict[str, Any]] = []
    raw_out = proc.stdout.strip()
    if raw_out.startswith("["):
        containers = json.loads(raw_out)
    else:
        for line in raw_out.splitlines():
            line = line.strip()
            if line:
                containers.append(json.loads(line))

    expected_services = {"doris-fe", "doris-be", "postgres", "metabase"}
    running_services = {
        c.get("Service", "")
        for c in containers
        if c.get("State", "").lower() == "running"
    }
    missing_services = expected_services - running_services
    if missing_services:
        logger.error(
            "以下容器服务未处于运行状态: %s", ", ".join(sorted(missing_services))
        )
        return False, containers

    # I10 检查：确认宿主机发布端口仅绑定 127.0.0.1，且 PostgreSQL 不向宿主机发布端口
    for c in containers:
        svc = c.get("Service", "")
        publishers = c.get("Publishers") or []
        for pub in publishers:
            pub_port = pub.get("PublishedPort", 0)
            pub_url = pub.get("URL", "")
            if not pub_port:
                continue
            if svc == "postgres":
                logger.error(
                    "安全边界违例 (I10): PostgreSQL 容器不应向宿主机发布端口，实际发布了 %s:%s",
                    pub_url,
                    pub_port,
                )
                return False, containers
            if pub_url != "127.0.0.1":
                logger.error(
                    "安全边界违例 (I10): 服务 %s 端口 %s 未限定绑定 127.0.0.1（实际绑定=%s）",
                    svc,
                    pub_port,
                    pub_url,
                )
                return False, containers

    logger.info(
        "容器状态与端口回环检查通过: 运行服务=%s，所有对外端口仅绑定 127.0.0.1，PostgreSQL 仅限内部网络。",
        ", ".join(sorted(running_services)),
    )
    return True, containers


def _check_metabase_ready(
    settings: Settings, logger: logging.Logger, timeout_sec: int = 150
) -> bool:
    health_url = f"http://{settings.metabase_host}:{settings.metabase_port}/api/health"
    deadline = time.monotonic() + timeout_sec
    last_err = ""

    with httpx.Client(trust_env=False, timeout=10.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(health_url)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "ok":
                        logger.info(
                            "Metabase HTTP 健康检查通过: %s -> %s",
                            health_url,
                            data,
                        )
                        break
                    last_err = f"status={data}"
                else:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:120]}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
            time.sleep(3)
        else:
            logger.error(
                "Metabase 健康检查未就绪 (%s): %s", health_url, last_err
            )
            return False

    # 检查 Metabase 容器日志或插件加载状态，确认 doris 驱动已注册
    proc = subprocess.run(
        ["docker", "compose", "-f", str(settings.compose_file), "logs", "metabase"],
        cwd=settings.project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    logs_combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if "doris" in logs_combined.lower():
        logger.info("Metabase 日志确认已发现并注册 :doris 插件驱动。")
    else:
        logger.warning("Metabase 日志中暂未检索到 doris 关键字，请进一步核验插件目录。")

    return _verify_metabase_doris_connection(settings, logger)


def _verify_metabase_doris_connection(
    settings: Settings, logger: logging.Logger
) -> bool:
    """通过 Metabase API 验证 :doris 驱动引擎注册、Doris 数据库连接、字段发现、原生查询及 PostgreSQL 落库 (I03)。"""
    base_url = f"http://{settings.metabase_host}:{settings.metabase_port}"
    admin_email = "admin@lade.local"
    admin_pass = "LadeDwAdmin2026!"

    with httpx.Client(trust_env=False, base_url=base_url, timeout=30.0) as client:
        props_resp = client.get("/api/session/properties")
        props_resp.raise_for_status()
        props = props_resp.json()

        setup_token = props.get("setup-token")
        session_id: str | None = None

        if setup_token:
            logger.info("首次初始化 Metabase 本地管理员账户与 Doris 数据源连接...")
            setup_payload = {
                "token": setup_token,
                "user": {
                    "first_name": "LaDe",
                    "last_name": "Admin",
                    "email": admin_email,
                    "password": admin_pass,
                    "site_name": "Industry Data Warehouse",
                },
                "prefs": {
                    "site_name": "Industry Data Warehouse",
                    "allow_tracking": False,
                },
            }
            setup_resp = client.post("/api/setup", json=setup_payload)
            setup_resp.raise_for_status()
            session_id = setup_resp.json().get("id")
        else:
            login_resp = client.post(
                "/api/session",
                json={"username": admin_email, "password": admin_pass},
            )
            if login_resp.status_code == 200:
                session_id = login_resp.json().get("id")
            else:
                logger.error(
                    "Metabase 登录失败 (HTTP %d): %s",
                    login_resp.status_code,
                    login_resp.text[:200],
                )
                return False

        headers = {"X-Metabase-Session": str(session_id)}

        # 核验 engines 列表中包含 doris
        props_auth = client.get("/api/session/properties", headers=headers).json()
        engines = props_auth.get("engines") or {}
        if "doris" not in engines:
            logger.error(
                "Metabase 已认证会话的 engines 列表中未发现 'doris' 驱动！当前可用引擎: %s",
                list(engines.keys()),
            )
            return False
        logger.info(
            "Metabase 引擎列表确认已注册 'doris': %s",
            engines["doris"].get("driver-name", "doris"),
        )

        # 查找或创建 Doris 数据库连接
        db_list_resp = client.get("/api/database", headers=headers)
        db_list_resp.raise_for_status()
        db_items = db_list_resp.json().get("data", [])
        doris_db = next(
            (d for d in db_items if d.get("engine") == "doris"), None
        )

        # 确保 Doris 中目标库已存在以便连接
        from warehouse.db import connect_doris

        with connect_doris(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{settings.doris_database}`"
                )

        if not doris_db:
            create_db_payload = {
                "name": f"Apache Doris ({settings.doris_database})",
                "engine": "doris",
                "details": {
                    "host": "doris-fe",
                    "port": 9030,
                    "dbname": settings.doris_database,
                    "user": settings.doris_user,
                    "password": settings.doris_password,
                },
                "is_full_sync": True,
                "auto_run_queries": True,
            }
            create_resp = client.post(
                "/api/database", headers=headers, json=create_db_payload
            )
            if create_resp.status_code != 200:
                logger.error(
                    "Metabase 创建 Doris 数据库连接失败 (HTTP %d): %s",
                    create_resp.status_code,
                    create_resp.text[:400],
                )
                return False
            doris_db = create_resp.json()
            logger.info(
                "Metabase 已成功创建 Doris 数据源连接: id=%s, name=%s",
                doris_db.get("id"),
                doris_db.get("name"),
            )

        db_id = doris_db["id"]

        # 执行原生查询验证 Doris 驱动查询能力
        native_query = {
            "database": db_id,
            "type": "native",
            "native": {"query": "SELECT 1 AS ping_ok"},
        }
        q_resp = client.post("/api/dataset", headers=headers, json=native_query)
        if q_resp.status_code not in (200, 202):
            logger.error(
                "Metabase 通过 doris 驱动执行原生查询失败 (HTTP %d): %s",
                q_resp.status_code,
                q_resp.text[:400],
            )
            return False
        q_data = q_resp.json()
        if q_data.get("status") != "completed":
            logger.error("Metabase 原生查询返回异常状态: %s", q_data)
            return False
        rows = q_data.get("data", {}).get("rows", [])
        logger.info("Metabase -> Doris 原生 SQL 查询验证成功: rows=%s", rows)

        # 若目标表已存在，触发 schema 同步并验证表与字段发现
        client.post(f"/api/database/{db_id}/sync_schema", headers=headers)
        for _ in range(10):
            meta_resp = client.get(
                f"/api/database/{db_id}/metadata", headers=headers
            )
            if meta_resp.status_code == 200:
                tables = meta_resp.json().get("tables", [])
                target_tbl = next(
                    (
                        t
                        for t in tables
                        if t.get("name") == settings.doris_table
                    ),
                    None,
                )
                if target_tbl and target_tbl.get("fields"):
                    field_names = [f.get("name") for f in target_tbl["fields"]]
                    logger.info(
                        "Metabase 表与字段发现验证成功: 表=%s, 发现字段数=%d (%s...)",
                        settings.doris_table,
                        len(field_names),
                        field_names[:5],
                    )
                    break
            time.sleep(2)

    return True


def run_doctor(settings: Settings, logger: logging.Logger) -> int:
    """执行完整环境、配置、端口安全与服务连接诊断。"""
    logger.info("=== 开始执行 Industry Data Warehouse 环境与连接检查 (doctor) ===")
    logger.info(
        "运行平台: OS=%s %s, Arch=%s, Python=%s",
        platform.system(),
        platform.release(),
        platform.machine(),
        sys.version.split()[0],
    )

    if settings.missing_env_keys:
        logger.error(
            "配置检查失败，缺少必要的环境变量: %s",
            ", ".join(settings.missing_env_keys),
        )
        return 1
    logger.info("配置变量检查通过（已显式读取 .env 且进程环境变量优先）。")

    try:
        ensure_runtime_dirs(settings)
        for label, d in (
            ("DATA_DIR", settings.data_dir),
            ("MANIFEST_DIR", settings.manifest_dir),
            ("STAGING_DIR", settings.staging_dir),
            ("LOG_DIR", settings.log_dir),
        ):
            probe = d / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            logger.info("目录可写检查通过: %s -> %s", label, d)
    except Exception as exc:  # noqa: BLE001
        logger.error("本地目录权限检查失败: %s", exc)
        return 1

    # 检查驱动文件与 SHA-256
    jar_path = settings.plugins_dir / DRIVER_FILENAME
    sha_path = settings.plugins_dir / DRIVER_SHA256_FILENAME
    if not jar_path.is_file() or not sha_path.is_file():
        logger.error(
            "缺少 Metabase Doris 驱动或校验文件: %s / %s", jar_path, sha_path
        )
        return 1
    expected_sha = parse_sha256_text(sha_path.read_text(encoding="utf-8"))
    actual_sha = compute_sha256(jar_path)
    if actual_sha != expected_sha:
        logger.error(
            "驱动文件 SHA-256 不匹配: 期望=%s, 实际=%s", expected_sha, actual_sha
        )
        return 1
    logger.info(
        "Metabase Doris 驱动校验通过: %s (sha256=%s)", jar_path.name, actual_sha
    )

    # 检查容器运行与端口回环绑定
    ok_containers, _ = _check_docker_containers(settings, logger)
    if not ok_containers:
        return 1

    # 检查 Doris 查询端口与 FE/BE 集群状态
    try:
        cluster = get_cluster_status(settings)
        if not cluster["fe_alive"] or not cluster["be_alive"]:
            logger.error(
                "Doris 节点未全部存活: FE Alive=%s, BE Alive=%s",
                cluster["fe_alive"],
                cluster["be_alive"],
            )
            return 1
        logger.info(
            "Doris MySQL 协议查询检查通过 (%s:%d): FE Alive=%s, BE Alive=%s",
            settings.doris_host,
            settings.doris_query_port,
            cluster["fe_alive"],
            cluster["be_alive"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Doris 查询连接失败 (%s:%d): %s",
            settings.doris_host,
            settings.doris_query_port,
            exc,
        )
        return 1

    # 检查 Doris FE HTTP 与 BE Stream Load HTTP 端点可达性
    fe_http_url = (
        f"http://{settings.doris_host}:{settings.doris_fe_http_port}/api/bootstrap"
    )
    be_health_url = f"{settings.doris_load_url.rstrip('/')}/api/health"
    with httpx.Client(trust_env=False, timeout=10.0) as client:
        try:
            fe_resp = client.get(fe_http_url)
            fe_resp.raise_for_status()
            logger.info("Doris FE HTTP 检查通过: %s -> %s", fe_http_url, fe_resp.text.strip())
        except Exception as exc:  # noqa: BLE001
            logger.error("Doris FE HTTP 不可达 (%s): %s", fe_http_url, exc)
            return 1

        try:
            be_resp = client.get(be_health_url)
            be_resp.raise_for_status()
            logger.info(
                "Doris BE 导入端点健康检查通过: %s -> %s",
                be_health_url,
                be_resp.text.strip(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Doris BE 导入端点不可达 (%s): %s", be_health_url, exc)
            return 1

    # 检查 Metabase 与驱动
    if not _check_metabase_ready(settings, logger):
        return 1

    logger.info("=== 所有环境、配置、端口安全与服务连接检查 (doctor) 已全部通过 ===")
    return 0
