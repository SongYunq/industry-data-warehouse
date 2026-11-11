"""Industry Data Warehouse 统一命令行任务入口（由 Pixi tasks 调用）。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from warehouse.config import load_settings, setup_task_logger
from warehouse.data_pipeline import (
    data_check,
    data_fetch,
    data_import,
    data_inspect,
    db_init,
)
from warehouse.doctor import run_doctor
from warehouse.services import services_down, services_status, services_up


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warehouse",
        description="Industry Data Warehouse 初次构建与数据接入任务入口",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="显式指定配置文件路径（默认读取项目根目录 .env）",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="覆盖默认原始数据目录 DATA_DIR",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("services-up", help="启动所需容器服务并保留既有数据卷")
    sub.add_parser("services-down", help="停止项目容器，保留数据卷和本地文件")
    sub.add_parser("services-status", help="显示容器服务与 Doris 节点状态")
    p_doctor = sub.add_parser("doctor", help="检查配置、路径、端口回环、Doris 与 Metabase 连接（默认只读检查）")
    p_doctor.add_argument(
        "--bootstrap",
        dest="allow_bootstrap",
        action="store_true",
        help="允许首次引导创建 Metabase 管理员、Doris 库及数据源连接",
    )

    p_fetch = sub.add_parser("data-fetch", help="自动获取默认原始文件、全量数据源文件或校验已有本地文件")
    p_fetch.add_argument("--file", default=None, help="指定已存在的本地原始文件路径")
    p_fetch.add_argument("--data-dir", dest="fetch_data_dir", default=None, help="指定原始数据保存目录")
    p_fetch.add_argument("--all", dest="fetch_all", action="store_true", help="按 LADE_DATASET_FILES 批量获取全部数据源文件至本地磁盘（不入 Doris）")

    p_inspect = sub.add_parser("data-inspect", help="核验分隔文本表（CSV/TSV）表头、编码、列数一致性与记录数")
    p_inspect.add_argument("--file", default=None, help="指定待核验的原始文件路径")
    p_inspect.add_argument("--encoding", default="utf-8", help="指定文件字符编码（默认 utf-8）")
    p_inspect.add_argument("--delimiter", default=None, help="指定字段分隔符（默认自动识别 ',' 或 '\\t'）")

    p_init = sub.add_parser("db-init", help="根据已核验表头初始化 Doris 原始接入库表")
    p_init.add_argument("--file", default=None, help="指定已核验的原始文件路径")

    p_import = sub.add_parser("data-import", help="通过 Doris Stream Load 批量导入原始文件")
    p_import.add_argument("--file", default=None, help="指定待导入的原始文件路径")
    p_import.add_argument("--batch-id", default=None, help="显式指定或重用批次标识")
    p_import.add_argument("--load-url", default=None, help="覆盖 Stream Load 导入端点 URL")
    p_import.add_argument(
        "--abort-unknown-batch",
        action="store_true",
        help="在人工确认原 UNKNOWN/IN_PROGRESS 批次已终止且无落库记录后，显式将其标记为 ABORTED_EMPTY",
    )

    p_check = sub.add_parser("data-check", help="核对导入批次状态、记录数与字段完整性")
    p_check.add_argument("--file", default=None, help="指定对应的原始文件路径")
    p_check.add_argument("--batch-id", default=None, help="指定要核对的导入批次标识")

    p_dash = sub.add_parser("dashboard", help="启动本机 Streamlit 技术接入检查页面")
    p_dash.add_argument("--port", type=int, default=None, help="覆盖默认监听端口")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    env_path = Path(args.env_file).resolve() if args.env_file else None
    data_dir_override = getattr(args, "fetch_data_dir", None) or args.data_dir
    settings = load_settings(env_file=env_path, data_dir_override=data_dir_override)
    logger, log_file = setup_task_logger(args.command, settings)
    logger.info("启动任务 [%s]，日志文件: %s", args.command, log_file)

    cmd = args.command
    if cmd == "services-up":
        return services_up(settings, logger)
    if cmd == "services-down":
        return services_down(settings, logger)
    if cmd == "services-status":
        return services_status(settings, logger)
    if cmd == "doctor":
        return run_doctor(
            settings,
            logger,
            allow_bootstrap=bool(getattr(args, "allow_bootstrap", False)),
        )
    if cmd == "data-fetch":
        return data_fetch(
            settings,
            logger,
            file_arg=args.file,
            data_dir_arg=getattr(args, "fetch_data_dir", None),
            fetch_all=bool(getattr(args, "fetch_all", False)),
        )
    if cmd == "data-inspect":
        code, _ = data_inspect(
            settings,
            logger,
            file_arg=args.file,
            encoding=args.encoding,
            delimiter=getattr(args, "delimiter", None),
        )
        return code
    if cmd == "db-init":
        return db_init(settings, logger, file_arg=args.file)
    if cmd == "data-import":
        return data_import(
            settings,
            logger,
            file_arg=args.file,
            batch_id_arg=args.batch_id,
            load_url_override=args.load_url,
            abort_unknown_batch=bool(
                getattr(args, "abort_unknown_batch", False)
            ),
        )
    if cmd == "data-check":
        return data_check(
            settings,
            logger,
            file_arg=args.file,
            batch_id_arg=args.batch_id,
        )
    if cmd == "dashboard":
        app_path = settings.project_root / "apps" / "streamlit" / "app.py"
        port = args.port or settings.streamlit_port
        host = settings.streamlit_host
        st_cmd = [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app_path),
            "--server.address",
            host,
            "--server.port",
            str(port),
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ]
        logger.info(
            "启动 Streamlit 本机应用: http://%s:%d (命令: %s)",
            host,
            port,
            " ".join(st_cmd),
        )
        return subprocess.call(st_cmd, cwd=settings.project_root)

    logger.error("未知命令: %s", cmd)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
