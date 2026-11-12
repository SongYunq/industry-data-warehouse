"""Validate the H&M snapshot and load it into Doris through isolated staging."""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TextIO
from uuid import uuid4

import httpx

from hm_recommendations.source import FILES_BY_NAME, SourceFile, read_manifest, verify_files
from warehouse.config import Settings, setup_task_logger
from warehouse.db import connect_doris

DATABASE = "hm_retail_dw"
FACT = "fact_purchase_item"
FACT_COLUMNS = "t_dat,customer_id,article_id,source_price_value,sales_channel_id"
FIRST_DATE = date(2018, 9, 20)
LAST_DATE = date(2020, 9, 22)
DATES = tuple(FIRST_DATE + timedelta(days=i) for i in range((LAST_DATE - FIRST_DATE).days + 1))
MONTHS = tuple(dict.fromkeys(day.isoformat()[:7] for day in DATES))
CUSTOMER_KEY = re.compile(r"[0-9a-f]{64}")
ARTICLE_NUMBERS = {
    "article_id", "product_code", "product_type_no", "graphical_appearance_no",
    "colour_group_code", "perceived_colour_value_id", "perceived_colour_master_id",
    "department_no", "index_group_no", "section_no", "garment_group_no",
}
CUSTOMER_COLUMNS = (
    "customer_id", "fn_source_flag", "active_source_flag", "club_member_status",
    "fashion_news_frequency", "age_years",
)


@dataclass(frozen=True)
class Snapshot:
    dimensions: dict[str, int]
    daily_counts: Counter[tuple[str, int]]
    monthly_counts: Counter[tuple[str, int]]


def _rows(spec: SourceFile) -> Iterator[tuple[int, list[str]]]:
    with spec.local_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        if next(reader, None) != list(spec.expected_columns):
            raise ValueError(f"{spec.file_name} 表头与预期不符")
        try:
            for number, row in enumerate(reader, start=2):
                if len(row) != len(spec.expected_columns):
                    raise ValueError(f"{spec.file_name} 第 {number} 条记录的列数不符")
                yield number, row
        except csv.Error:
            raise ValueError(f"{spec.file_name} 第 {reader.line_num} 行 CSV 格式无效") from None


def _write_json_row(stream: TextIO, row: dict) -> None:
    stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _stage_articles(path: Path) -> set[int]:
    spec = FILES_BY_NAME["articles.csv"]
    keys: set[int] = set()
    with path.open("w", encoding="utf-8", newline="\n") as target:
        for number, row in _rows(spec):
            try:
                values = {
                    field: (int(value) if field in ARTICLE_NUMBERS else value) if value else None
                    for field, value in zip(spec.expected_columns, row, strict=True)
                }
                article = values["article_id"]
                if article is None or not 0 < article < 2**31 or article in keys:
                    raise ValueError
            except ValueError:
                raise ValueError(f"{spec.file_name} 第 {number} 条记录商品键缺失、重复或编码无效") from None
            keys.add(article)
            _write_json_row(target, values)
    return keys


def _stage_customers(path: Path) -> set[str]:
    spec = FILES_BY_NAME["customers.csv"]
    keys: set[str] = set()
    with path.open("w", encoding="utf-8", newline="\n") as target:
        for number, row in _rows(spec):
            customer = row[0]
            try:
                age = int(row[5]) if row[5] else None
                if not CUSTOMER_KEY.fullmatch(customer) or customer in keys:
                    raise ValueError
            except ValueError:
                raise ValueError(f"{spec.file_name} 第 {number} 条记录客户键缺失、重复或年龄无效") from None
            keys.add(customer)
            values = [value or None for value in row[:5]] + [age]
            _write_json_row(target, dict(zip(CUSTOMER_COLUMNS, values, strict=True)))
    return keys


def _stage_calendar(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as target:
        for day in DATES:
            iso_year, iso_week, weekday = day.isocalendar()
            _write_json_row(target, {
                "date_key": day.year * 10000 + day.month * 100 + day.day,
                "calendar_date": day.isoformat(),
                "year_num": day.year,
                "quarter_num": (day.month - 1) // 3 + 1,
                "month_num": day.month,
                "day_num": day.day,
                "weekday_num": weekday,
                "iso_week_year": iso_year,
                "iso_week_num": iso_week,
                "is_weekend": weekday >= 6,
            })


def _stage_snapshot(
    folder: Path,
    months: tuple[str, ...],
    manifest: dict[str, dict[str, str]],
    logger: logging.Logger,
) -> Snapshot:
    articles = _stage_articles(folder / "dim_article")
    customers = _stage_customers(folder / "dim_customer")
    _stage_calendar(folder / "dim_date")
    dimensions = {"dim_article": len(articles), "dim_customer": len(customers), "dim_date": len(DATES)}
    for name, table in (("articles.csv", "dim_article"), ("customers.csv", "dim_customer")):
        if dimensions[table] != int(manifest[name]["records"]):
            raise ValueError(f"{name} 维度行数与校验清单不符")
    logger.info("源维度转换和键唯一性检查通过，开始按月转换交易。")

    spec = FILES_BY_NAME["transactions_train.csv"]
    date_month = {day.isoformat(): day.isoformat()[:7] for day in DATES}
    daily_counts: Counter[tuple[str, int]] = Counter()
    with ExitStack() as stack:
        writers = {
            month: stack.enter_context((folder / month).open("w", encoding="ascii", newline="\n"))
            for month in months
        }
        for number, (day, customer, article, price, channel) in _rows(spec):
            try:
                month = date_month[day]
                price_value = float(price)
                article_id = int(article)
                if (
                    not math.isfinite(price_value) or price_value <= 0 or channel not in ("1", "2")
                    or article_id not in articles or customer not in customers
                ):
                    raise ValueError
            except (ValueError, KeyError):
                raise ValueError(f"{spec.file_name} 第 {number} 条记录日期、价格、渠道或外键检查失败") from None
            daily_counts[day, int(channel)] += 1
            if month in writers:
                writers[month].write(f"{day},{customer},{article_id},{price_value},{channel}\n")
    if daily_counts.total() != int(manifest[spec.file_name]["records"]) or {
        day for day, _ in daily_counts
    } != date_month.keys():
        raise ValueError("交易行数或日期覆盖与校验基线不符")
    monthly_counts: Counter[tuple[str, int]] = Counter()
    for (day, channel), count in daily_counts.items():
        monthly_counts[date_month[day], channel] += count
    return Snapshot(dimensions, daily_counts, monthly_counts)


def _stream_load(
    client: httpx.Client, settings: Settings, table: str, path: Path, expected: int, label: str,
    *, partition: str | None = None,
) -> dict:
    headers = {
        "label": label,
        "strict_mode": "true",
        "max_filter_ratio": "0",
        "Content-Length": str(path.stat().st_size),
        "timeout": "600",
    }
    if partition:
        headers.update(format="csv", column_separator=",", columns=FACT_COLUMNS, temporary_partitions=partition)
    else:
        headers.update(format="json", read_json_by_line="true")

    url = httpx.URL(scheme="http", host=settings.doris_host, port=settings.doris_be_http_port,
                    path=f"/api/{DATABASE}/{table}/_stream_load")
    with path.open("rb") as stream:
        result = client.put(url, headers=headers, content=iter(lambda: stream.read(1024 * 1024), b""))
    result.raise_for_status()
    response = result.json()
    counts = tuple(int(response.get(key, -1)) for key in (
        "NumberTotalRows", "NumberLoadedRows", "NumberFilteredRows", "NumberUnselectedRows",
    ))
    if response.get("Status") != "Success" or counts != (expected, expected, 0, 0):
        raise RuntimeError(
            f"Stream Load {label} 失败，状态={response.get('Status')}，"
            f"输入/装载/过滤/跳过={counts}，事务={response.get('TxnId')}"
        )
    return {"label": label, "rows": expected, "transaction": response.get("TxnId")}


@contextmanager
def _load_lock(path: Path) -> Iterator[None]:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RuntimeError(f"已有装载运行或上次异常退出留下锁：{path}") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"{os.getpid()}\n")
        yield
    finally:
        path.unlink(missing_ok=True)


def _assert_dimensions(cursor, tables: dict[str, str], snapshot: Snapshot) -> dict[str, int]:
    for target, table in tables.items():
        key = {"dim_article": "article_id", "dim_customer": "customer_id", "dim_date": "date_key"}[target]
        cursor.execute(f"SELECT COUNT(*) AS rows_n, COUNT(DISTINCT {key}) AS keys_n FROM {table}")
        counts = cursor.fetchone()
        if counts != {"rows_n": snapshot.dimensions[target], "keys_n": snapshot.dimensions[target]}:
            raise RuntimeError(f"{target} 的装载行数或键唯一性与源文件不符：{counts}")
    cursor.execute(f"""
        SELECT SUM(type_n > 1) AS type_conflicts, SUM(group_n > 1) AS group_conflicts
        FROM (
            SELECT COUNT(DISTINCT product_type_name) AS type_n, COUNT(DISTINCT product_group_name) AS group_n
            FROM {tables['dim_article']} WHERE product_code IS NOT NULL GROUP BY product_code
        ) AS groups
    """)
    return {name: int(value) for name, value in cursor.fetchone().items()}


def _assert_fact(cursor, fact: str, tables: dict[str, str], snapshot: Snapshot, months: tuple[str, ...]) -> None:
    cursor.execute(f"""
        SELECT t_dat, sales_channel_id AS channel, COUNT(*) AS rows_n,
               COUNT(source_price_value) AS prices
        FROM {fact} GROUP BY t_dat, sales_channel_id
    """)
    expected = {key: value for key, value in snapshot.daily_counts.items() if key[0][:7] in months}
    actual = {}
    for row in cursor.fetchall():
        day = row["t_dat"].isoformat()
        if row["prices"] != row["rows_n"]:
            raise RuntimeError("购买事实存在空源价格")
        actual[day, row["channel"]] = row["rows_n"]
    if actual != expected:
        raise RuntimeError("购买事实的日期／渠道行数与源文件不符")

    for dimension, key, fact_key in (
        ("dim_customer", "customer_id", "customer_id"),
        ("dim_article", "article_id", "article_id"),
        ("dim_date", "calendar_date", "t_dat"),
    ):
        cursor.execute(f"""
            SELECT COUNT(*) AS unmatched FROM {fact} AS f
            LEFT ANTI JOIN {tables[dimension]} AS d ON f.{fact_key} = d.{key}
        """)
        if cursor.fetchone()["unmatched"]:
            raise RuntimeError(f"购买事实存在未匹配的 {dimension} 外键")


def _publish(
    settings: Settings, folder: Path, months: tuple[str, ...], snapshot: Snapshot, run: str,
    report: dict, logger: logging.Logger,
) -> None:
    full_load = months == MONTHS
    formal = {month: f"p_{month.replace('-', '')}" for month in months}
    staging = {month: f"{name}_{run}" for month, name in formal.items()}
    dimensions = {
        name: f"{name}_{run}" if full_load else name
        for name in snapshot.dimensions
    }

    with connect_doris(settings, read_timeout=600) as conn, conn.cursor() as cursor, httpx.Client(
        auth=(settings.doris_user, settings.doris_password), timeout=630, trust_env=False,
    ) as client:
        schema = settings.project_root / "sql/hm_recommendations/001_schema.sql"
        for statement in schema.read_text(encoding="utf-8").split(";"):
            if statement.strip():
                cursor.execute(statement)
        try:
            if full_load:
                for name, table in dimensions.items():
                    cursor.execute(f"CREATE TABLE {table} LIKE {name}")
                    batch = _stream_load(client, settings, table, folder / name, snapshot.dimensions[name], f"hm_{table}")
                    report["batches"].append(batch)
                    logger.info("%s 暂存装载完成：%s 行。", name, batch["rows"])
            report["article_conflicts"] = _assert_dimensions(cursor, dimensions, snapshot)
            for month, partition in staging.items():
                lower = date.fromisoformat(f"{month}-01")
                upper = (lower.replace(day=28) + timedelta(days=4)).replace(day=1)
                cursor.execute(f"""
                    ALTER TABLE {FACT} ADD TEMPORARY PARTITION {partition}
                    VALUES [("{lower}"), ("{upper}")) DISTRIBUTED BY HASH(customer_id) BUCKETS 4
                """)
                expected = sum(snapshot.monthly_counts[month, code] for code in (1, 2))
                batch = _stream_load(client, settings, FACT, folder / month, expected, f"hm_{partition}", partition=partition)
                report["batches"].append(batch)
                logger.info("%s 暂存装载完成：%s 行。", month, batch["rows"])

            temp_names = ", ".join(staging.values())
            _assert_fact(cursor, f"{FACT} TEMPORARY PARTITION ({temp_names})", dimensions, snapshot, months)
            if full_load:
                for name, table in dimensions.items():
                    cursor.execute(f"ALTER TABLE {name} REPLACE WITH TABLE {table} PROPERTIES ('swap' = 'false')")
            cursor.execute(
                f"ALTER TABLE {FACT} REPLACE PARTITION ({', '.join(formal.values())}) "
                f"WITH TEMPORARY PARTITION ({temp_names})"
            )
            report["published"] = True
            _assert_fact(cursor, f"{FACT} PARTITION ({', '.join(formal.values())})",
                         {name: name for name in dimensions}, snapshot, months)
            cursor.execute(f"SHOW PARTITIONS FROM {FACT}")
            actual_names = {row["PartitionName"] for row in cursor.fetchall()}
            expected_names = {f"p_{month.replace('-', '')}" for month in MONTHS}
            if actual_names != expected_names:
                raise RuntimeError("事实表正式分区与建表基线不符")
        finally:
            for partition in staging.values():
                try:
                    cursor.execute(f"ALTER TABLE {FACT} DROP TEMPORARY PARTITION IF EXISTS {partition}")
                except Exception:
                    logger.warning("暂存分区清理未成功：%s", partition)
            if full_load:
                for table in dimensions.values():
                    try:
                        cursor.execute(f"DROP TABLE IF EXISTS {table}")
                    except Exception:
                        logger.warning("暂存维度清理未成功：%s", table)


def _source_signature() -> dict[str, tuple[int, int, int]]:
    signatures = {}
    for name, spec in FILES_BY_NAME.items():
        stat = spec.local_path.stat()
        signatures[name] = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    return signatures


def load_snapshot(settings: Settings, month: str | None = None) -> Path:
    months = (month,) if month else MONTHS
    if any(value not in MONTHS for value in months):
        raise ValueError(f"月份须为 {MONTHS[0]} 至 {MONTHS[-1]} 的 YYYY-MM")
    logger, _ = setup_task_logger("hm-data-load", settings)
    settings.manifest_dir.mkdir(parents=True, exist_ok=True)
    run = uuid4().hex[:12]
    report_path = settings.manifest_dir / f"hm_load_{run}.json"
    report = {
        "run": run, "database": DATABASE, "started_at": datetime.now(UTC).isoformat(),
        "status": "failed", "published": False, "months": months, "batches": [],
    }
    stage_root = settings.project_root / "data" / "staging"
    stage_root.mkdir(parents=True, exist_ok=True)
    with _load_lock(stage_root / "hm_load.lock"):
        try:
            signature = _source_signature()
            manifest = read_manifest()
            files = verify_files()
            if any(result["status"] != "verified" for result in files):
                raise ValueError("原始文件未全部通过字节数和 SHA-256 检查")
            report["inputs"] = {
                result["file_name"]: result["actual_sha256"] for result in files
            }
            logger.info("三份原始文件 SHA-256 通过，批次 %s。", run)
            with TemporaryDirectory(prefix="hm_load_", dir=stage_root) as temporary:
                folder = Path(temporary)
                snapshot = _stage_snapshot(folder, months, manifest, logger)
                if signature != _source_signature():
                    raise RuntimeError("原始文件在校验或转换期间发生变化")
                report["dimension_rows"] = snapshot.dimensions
                report["fact_rows_by_month_and_channel"] = {
                    value: {str(code): snapshot.monthly_counts[value, code] for code in (1, 2)}
                    for value in months
                }
                loaded_dates = [day for day, _ in snapshot.daily_counts if day[:7] in months]
                report["date_range"] = [min(loaded_dates), max(loaded_dates)]
                _publish(settings, folder, months, snapshot, run, report, logger)
            report["status"] = "verified"
            logger.info("Doris H&M 装载与复核通过：%s", report_path)
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            logger.error("批次 %s 失败：%s", run, exc)
            raise
        finally:
            report["ended_at"] = datetime.now(UTC).isoformat()
            temporary_report = report_path.with_suffix(".part")
            temporary_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary_report.replace(report_path)
    return report_path
