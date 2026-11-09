"""Apache Doris 数据库连接、集群状态核查与节点注册辅助模块。"""

from __future__ import annotations

import logging
import time
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from warehouse.config import Settings


def connect_doris(
    settings: Settings,
    database: str | None = None,
    connect_timeout: int = 10,
    read_timeout: int = 30,
) -> pymysql.connections.Connection:
    """使用 MySQL 协议连接 Doris FE 查询端口。"""
    return pymysql.connect(
        host=settings.doris_host,
        port=settings.doris_query_port,
        user=settings.doris_user,
        password=settings.doris_password,
        database=database,
        charset="utf8mb4",
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        write_timeout=read_timeout,
        autocommit=True,
        cursorclass=DictCursor,
    )


def get_cluster_status(settings: Settings) -> dict[str, Any]:
    """查询 Doris FE 与 BE 节点列表及存活状态。"""
    with connect_doris(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW FRONTENDS")
            frontends: list[dict[str, Any]] = list(cur.fetchall())
            cur.execute("SHOW BACKENDS")
            backends: list[dict[str, Any]] = list(cur.fetchall())
            cur.execute("SELECT VERSION() AS version")
            ver_row = cur.fetchone() or {}

    fe_alive = any(
        str(fe.get("Alive", "")).lower() == "true" for fe in frontends
    )
    be_alive = any(
        str(be.get("Alive", "")).lower() == "true" for be in backends
    )
    return {
        "version": ver_row.get("version", ""),
        "frontends": frontends,
        "backends": backends,
        "fe_alive": fe_alive,
        "be_alive": be_alive,
    }


def ensure_be_registered(
    settings: Settings,
    logger: logging.Logger,
    be_heartbeat_addr: str = "172.30.0.11:9050",
    timeout_sec: int = 180,
) -> dict[str, Any]:
    """等待 Doris FE 就绪并确保 BE 节点完成注册且处于 Alive 状态。"""
    deadline = time.monotonic() + timeout_sec
    last_err: Exception | None = None
    added_attempted = False

    while time.monotonic() < deadline:
        try:
            status = get_cluster_status(settings)
            backends = status["backends"]
            if status["be_alive"]:
                return status

            if not backends and not added_attempted:
                logger.info(
                    "检测到 Doris 尚未注册 BE 节点，尝试注册 %s ...",
                    be_heartbeat_addr,
                )
                with connect_doris(settings) as conn:
                    with conn.cursor() as cur:
                        try:
                            cur.execute(
                                f'ALTER SYSTEM ADD BACKEND "{be_heartbeat_addr}"'
                            )
                        except pymysql.MySQLError as exc:
                            if "already exists" not in str(exc).lower():
                                logger.warning("注册 BE 节点返回提示: %s", exc)
                added_attempted = True
        except Exception as exc:  # noqa: BLE001
            last_err = exc

        time.sleep(3)

    raise RuntimeError(
        f"等待 Doris FE/BE 集群就绪超时（{timeout_sec}s），最后错误: {last_err}"
    )
