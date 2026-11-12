"""H&M 原始 CSV 获取、校验和结构盘点命令。"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from hm_recommendations.source import (
    FILES,
    PROJECT_ROOT,
    fetch_files,
    inspect_files,
    select_files,
    verify_files,
)

load_dotenv(PROJECT_ROOT / ".env", override=False)


def _add_file_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--file",
        action="append",
        choices=[spec.file_name for spec in FILES],
        help="只处理指定文件；可重复传入，默认处理三张核心表",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hm-recommendations",
        description="H&M Kaggle 数据文件获取、SHA-256 核验与 CSV 结构盘点",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch", help="通过 Kaggle CLI 下载三张核心 CSV")
    _add_file_options(fetch)
    fetch.add_argument("--force", action="store_true", help="重新下载并覆盖现有源文件")

    check = commands.add_parser("check", help="按记录字节数和 SHA-256 校验原始文件")
    _add_file_options(check)

    inspect = commands.add_parser("inspect", help="完整读取 CSV 并导出字段与记录概况")
    _add_file_options(inspect)

    dashboard = commands.add_parser("dashboard", help="启动原始文件准备状态页")
    dashboard.add_argument("--port", type=int, default=8501)
    return parser


def _run_fetch(args: argparse.Namespace) -> int:
    results = fetch_files(select_files(args.file), force=args.force)
    for item in results:
        suffix = f"：{item['error']}" if item.get("error") else ""
        print(f"{item['status']:>10}  {item['file_name']}{suffix}")
    failed = sum(item["status"] == "failed" for item in results)
    print(f"下载处理完成：成功/复用 {len(results) - failed} 个，失败 {failed} 个。")
    return 1 if failed else 0


def _run_check(args: argparse.Namespace) -> int:
    results = verify_files(select_files(args.file))
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(("文件", "状态", "预期字节", "实际字节"))
    for item in results:
        writer.writerow((item["file_name"], item["status"], item["expected_bytes"] or "", item["actual_bytes"] or ""))
    verified = sum(item["status"] == "verified" for item in results)
    print(f"文件核验结果：verified={verified}，总数={len(results)}。")
    return 0 if verified == len(results) else 1


def _run_inspect(args: argparse.Namespace) -> int:
    rows = inspect_files(select_files(args.file))
    for item in rows:
        print(
            f"{item['file_name']}: {item['records']:,} 行，{item['columns']} 列，"
            f"结构匹配={item['expected_columns_match']}，列数异常={item['malformed_rows']:,}"
        )
        if item["date_min"]:
            print(f"  日期范围：{item['date_min']} 至 {item['date_max']}")
    print("概况已写入 docs/附件-数据概况.csv 和 docs/附件-字段清单.csv。")
    return 0 if all(row["malformed_rows"] == 0 for row in rows) else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            return _run_fetch(args)
        if args.command == "check":
            return _run_check(args)
        if args.command == "inspect":
            return _run_inspect(args)
        if args.command == "dashboard":
            app = PROJECT_ROOT / "apps" / "streamlit" / "app.py"
            return subprocess.call([
                sys.executable, "-m", "streamlit", "run", str(app),
                "--server.address", "127.0.0.1", "--server.port", str(args.port),
                "--server.headless", "true", "--browser.gatherUsageStats", "false",
            ])
    except Exception as exc:  # noqa: BLE001
        print(f"任务失败：{exc}", file=sys.stderr)
        return 1
    parser.error("未知命令")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
