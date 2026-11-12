"""Inside Airbnb 当前版命令行入口。"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

from inside_airbnb.source import (
    PROJECT_ROOT,
    fetch_entries,
    inspect_entries,
    load_manifest,
    select_entries,
    verify_entries,
    write_csv,
)
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=False)


def _city_snapshot_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--city", help="按城市筛选：hong_kong、tokyo 或 singapore")
    parser.add_argument("--snapshot", help="按快照日期筛选，例如 2026-09-20")
    parser.add_argument("--manifest", help="覆盖默认的数据文件清单")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inside-airbnb",
        description="Inside Airbnb 原始文件获取、完整性核验与结构盘点",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch", help="下载清单中的缺失或校验不匹配文件")
    _city_snapshot_args(fetch)
    fetch.add_argument("--timeout", type=float, default=None, help="单个请求的超时秒数")

    check = commands.add_parser("check", help="校验所选原始文件的字节数和 SHA-256")
    _city_snapshot_args(check)

    inspect = commands.add_parser("inspect", help="统计 CSV、压缩 CSV 和 GeoJSON 的实际结构")
    _city_snapshot_args(inspect)
    inspect.add_argument("--output", help="可选：将数据概况另存为 CSV")

    dashboard = commands.add_parser("dashboard", help="启动原始数据目录检查页")
    dashboard.add_argument(
        "--port", type=int, default=int(os.environ.get("STREAMLIT_PORT", "8501"))
    )

    return parser


def _selected(args: argparse.Namespace):
    return select_entries(load_manifest(args.manifest), args.city, args.snapshot)


def _run_fetch(args: argparse.Namespace) -> int:
    results = fetch_entries(_selected(args), timeout_seconds=args.timeout)
    for item in results:
        suffix = f"：{item['error']}" if item["status"] == "failed" else ""
        print(f"{item['status']:>10}  {item['relative_path']}{suffix}")
    failed = sum(item["status"] == "failed" for item in results)
    print(f"下载完成：成功/复用 {len(results) - failed} 个，失败 {failed} 个。")
    return 1 if failed else 0


def _run_check(args: argparse.Namespace) -> int:
    results = verify_entries(_selected(args))
    for item in results:
        if item["status"] != "verified":
            print(
                f"{item['status']:>14}  {item['relative_path']} "
                f"(期望 {item['expected_bytes']} 字节，实际 {item['actual_bytes']})"
            )
    counts: dict[str, int] = {}
    for item in results:
        key = str(item["status"])
        counts[key] = counts.get(key, 0) + 1
    print("文件核验结果：" + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    return 0 if counts == {"verified": len(results)} else 1


def _run_inspect(args: argparse.Namespace) -> int:
    rows = inspect_entries(_selected(args))
    if args.output:
        fields = [
            "city", "snapshot_date", "file", "records", "columns",
            "date_min", "date_max", "available_values",
        ]
        output = Path(args.output).expanduser()
        output = output.resolve() if output.is_absolute() else (PROJECT_ROOT / output).resolve()
        write_csv(output, rows, fields)
        print(f"已写入数据概况：{output}")
    else:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(("城市", "快照日期", "文件", "记录数", "列数", "日期范围", "可订值/几何"))
        for row in rows:
            date_range = "—"
            if row["date_min"] or row["date_max"]:
                date_range = f"{row['date_min']} 至 {row['date_max']}"
            writer.writerow((
                row["city"], row["snapshot_date"], row["file"], row["records"],
                row["columns"], date_range, row["available_values"],
            ))
    malformed = sum(int(row["malformed_rows"]) for row in rows)
    print(f"结构盘点完成：{len(rows)} 个文件，发现列数不匹配记录 {malformed} 行。")
    return 1 if malformed else 0


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
            host = os.environ.get("STREAMLIT_HOST", "127.0.0.1")
            command = [
                sys.executable, "-m", "streamlit", "run", str(app),
                "--server.address", host, "--server.port", str(args.port),
                "--server.headless", "true", "--browser.gatherUsageStats", "false",
            ]
            return subprocess.call(command, cwd=PROJECT_ROOT)
    except Exception as exc:  # noqa: BLE001
        print(f"任务失败：{exc}", file=sys.stderr)
        return 1
    parser.error("未知命令")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
