"""香港 Inside Airbnb 先锋数据的维度装载、摘要处理与看板读取。"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from warehouse.config import load_settings
from warehouse.db import connect_doris

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = PROJECT_ROOT / "data" / "raw" / "inside_airbnb" / "hong_kong"
SCHEMA_FILE = PROJECT_ROOT / "sql" / "pilot_hong_kong" / "001_schema.sql"
DATABASE = "hk_airbnb_pilot"
CITY_KEY = "hong_kong"
BATCH_SIZE = 200
EXPECTED_LISTING_COUNTS = {
    "2025-12-27": 6630,
    "2026-03-29": 2115,
    "2026-06-27": 6734,
    "2026-09-20": 6614,
}
EXPECTED_NEIGHBOURHOODS = 18

LISTING_COLUMNS = (
    "city_key", "snapshot_date", "listing_id", "host_id", "neighbourhood",
    "property_type", "room_type", "accommodates", "bedrooms", "beds",
    "bathrooms", "price", "minimum_nights", "maximum_nights", "availability_30",
    "availability_60", "availability_90", "availability_365", "number_of_reviews",
    "number_of_reviews_ltm", "review_scores_rating", "latitude", "longitude",
)
INTEGER_FIELDS = {
    "host_id", "accommodates", "minimum_nights", "maximum_nights", "availability_30",
    "availability_60", "availability_90", "availability_365", "number_of_reviews",
    "number_of_reviews_ltm",
}
DECIMAL_FIELDS = {
    "bedrooms", "beds", "bathrooms", "price", "review_scores_rating", "latitude", "longitude",
}
SOURCE_FIELDS = {
    "host_id": "host_id",
    "neighbourhood": "neighbourhood_cleansed",
    "property_type": "property_type",
    "room_type": "room_type",
    "accommodates": "accommodates",
    "bedrooms": "bedrooms",
    "beds": "beds",
    "bathrooms": "bathrooms",
    "price": "price",
    "minimum_nights": "minimum_nights",
    "maximum_nights": "maximum_nights",
    "availability_30": "availability_30",
    "availability_60": "availability_60",
    "availability_90": "availability_90",
    "availability_365": "availability_365",
    "number_of_reviews": "number_of_reviews",
    "number_of_reviews_ltm": "number_of_reviews_ltm",
    "review_scores_rating": "review_scores_rating",
    "latitude": "latitude",
    "longitude": "longitude",
}
HOST_COLUMNS = (
    "city_key", "snapshot_date", "host_id", "host_is_superhost", "host_identity_verified",
    "host_since", "host_listings_count", "host_total_listings_count",
    "calculated_host_listings_count", "host_response_rate", "host_acceptance_rate",
    "entire_home_listing_count", "private_room_listing_count", "shared_room_listing_count",
)
HOST_SOURCE_FIELDS = {
    "host_is_superhost": "host_is_superhost",
    "host_identity_verified": "host_identity_verified",
    "host_since": "host_since",
    "host_listings_count": "host_listings_count",
    "host_total_listings_count": "host_total_listings_count",
    "calculated_host_listings_count": "calculated_host_listings_count",
    "host_response_rate": "host_response_rate",
    "host_acceptance_rate": "host_acceptance_rate",
    "entire_home_listing_count": "calculated_host_listings_count_entire_homes",
    "private_room_listing_count": "calculated_host_listings_count_private_rooms",
    "shared_room_listing_count": "calculated_host_listings_count_shared_rooms",
}
AVAILABILITY_COLUMNS = (
    "city_key", "snapshot_date", "calendar_date", "neighbourhood", "room_type", "property_type",
    "listing_count", "available_listing_count", "unavailable_listing_count", "unknown_status_count",
)
REVIEW_COLUMNS = (
    "city_key", "review_month", "review_count", "reviewed_listing_count",
    "earliest_review_date", "latest_review_date",
)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _integer(value: Any) -> int | None:
    cleaned = _text(value)
    if cleaned is None:
        return None
    try:
        return int(Decimal(cleaned))
    except (InvalidOperation, ValueError):
        return None


def _decimal(value: Any, *, currency: bool = False, percentage: bool = False) -> Decimal | None:
    cleaned = _text(value)
    if cleaned is None:
        return None
    if currency:
        cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
    if percentage:
        cleaned = cleaned.replace("%", "").strip()
    try:
        result = Decimal(cleaned)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _boolean(value: Any) -> bool | None:
    cleaned = (_text(value) or "").lower()
    if cleaned in {"t", "true", "1", "yes"}:
        return True
    if cleaned in {"f", "false", "0", "no"}:
        return False
    return None


def _date(value: Any) -> date | None:
    cleaned = _text(value)
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned[:10])
    except ValueError:
        return None


def _listing_values(row: dict[str, str], snapshot_date: str) -> tuple[Any, ...]:
    listing_id = _integer(row.get("id"))
    if listing_id is None:
        raise ValueError(f"{snapshot_date} 的 listings.csv.gz 出现缺失或无效房源 id")

    values: dict[str, Any] = {"city_key": CITY_KEY, "snapshot_date": snapshot_date, "listing_id": listing_id}
    for target, source in SOURCE_FIELDS.items():
        value = row.get(source)
        if target == "neighbourhood" and not _text(value):
            value = row.get("neighbourhood")
        if target in INTEGER_FIELDS:
            values[target] = _integer(value)
        elif target in DECIMAL_FIELDS:
            values[target] = _decimal(value, currency=target == "price")
        else:
            values[target] = _text(value)
    return tuple(values[column] for column in LISTING_COLUMNS)


def _host_values(row: dict[str, str], snapshot_date: str) -> tuple[Any, ...] | None:
    host_id = _integer(row.get("host_id"))
    if host_id is None:
        return None
    values: dict[str, Any] = {"city_key": CITY_KEY, "snapshot_date": snapshot_date, "host_id": host_id}
    for target, source in HOST_SOURCE_FIELDS.items():
        value = row.get(source)
        if target in {"host_is_superhost", "host_identity_verified"}:
            values[target] = _boolean(value)
        elif target == "host_since":
            values[target] = _date(value)
        elif target.endswith("_rate"):
            values[target] = _decimal(value, percentage=True)
        else:
            values[target] = _integer(value)
    return tuple(values[column] for column in HOST_COLUMNS)


def _snapshot_directories() -> list[Path]:
    if not RAW_ROOT.is_dir():
        raise FileNotFoundError(f"找不到香港原始数据目录：{RAW_ROOT}")
    folders = sorted(path for path in RAW_ROOT.iterdir() if path.is_dir())
    if not folders:
        raise FileNotFoundError(f"香港原始数据目录下没有快照：{RAW_ROOT}")
    return folders


def _apply_schema(connection: Any) -> None:
    if not SCHEMA_FILE.is_file():
        raise FileNotFoundError(f"找不到 Doris 表结构文件：{SCHEMA_FILE}")
    statements = [part.strip() for part in SCHEMA_FILE.read_text(encoding="utf-8").split(";")]
    with connection.cursor() as cursor:
        for statement in statements:
            if statement:
                cursor.execute(statement)


def _insert_batch(cursor: Any, table: str, columns: tuple[str, ...], batch: list[tuple[Any, ...]]) -> None:
    if not batch:
        return
    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    cursor.executemany(f"INSERT INTO {table} ({names}) VALUES ({placeholders})", batch)
    batch.clear()


def _load_city(cursor: Any) -> None:
    cursor.execute(
        "INSERT INTO dim_city (city_key, city_name, country_or_region, currency_code, time_zone, source_name) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (CITY_KEY, "香港", "中国香港", "HKD", "Asia/Hong_Kong", "Inside Airbnb"),
    )


def _load_snapshot_dimension(cursor: Any, folder: Path, row_count: int) -> None:
    listing_file = folder / "listings.csv.gz"
    with gzip.open(listing_file, "rt", encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream), {})
    snapshot = _date(folder.name)
    if snapshot is None:
        raise ValueError(f"无效快照目录日期：{folder.name}")
    cursor.execute(
        "INSERT INTO dim_snapshot "
        "(city_key, snapshot_date, scrape_id, last_scraped_date, snapshot_year, snapshot_quarter, listing_source_rows) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (CITY_KEY, snapshot, _text(row.get("scrape_id")), _date(row.get("last_scraped")),
         snapshot.year, (snapshot.month - 1) // 3 + 1, row_count),
    )


def _load_snapshot(cursor: Any, folder: Path) -> tuple[int, int, set[str], set[str]]:
    listing_file = folder / "listings.csv.gz"
    if not listing_file.is_file():
        raise FileNotFoundError(f"快照缺少详细房源文件：{listing_file}")
    seen_ids: set[int] = set()
    hosts: dict[int, tuple[Any, ...]] = {}
    room_types: set[str] = set()
    property_types: set[str] = set()
    count = 0
    listing_batch: list[tuple[Any, ...]] = []
    with gzip.open(listing_file, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            values = _listing_values(row, folder.name)
            listing_id = int(values[2])
            if listing_id in seen_ids:
                raise ValueError(f"{folder.name} 的 listings.csv.gz 存在重复房源 id：{listing_id}")
            seen_ids.add(listing_id)
            listing_batch.append(values)
            count += 1
            host_values = _host_values(row, folder.name)
            if host_values:
                hosts[int(host_values[2])] = host_values
            if room_type := _text(row.get("room_type")):
                room_types.add(room_type)
            if property_type := _text(row.get("property_type")):
                property_types.add(property_type)
            if len(listing_batch) >= BATCH_SIZE:
                _insert_batch(cursor, "fact_listing_snapshot", LISTING_COLUMNS, listing_batch)
        _insert_batch(cursor, "fact_listing_snapshot", LISTING_COLUMNS, listing_batch)
    if count == 0:
        raise ValueError(f"快照 {folder.name} 的详细房源文件没有数据记录")

    host_batch = list(hosts.values())
    for start in range(0, len(host_batch), BATCH_SIZE):
        _insert_batch(cursor, "dim_host_snapshot", HOST_COLUMNS, host_batch[start:start + BATCH_SIZE])
    _load_snapshot_dimension(cursor, folder, count)
    return count, len(hosts), room_types, property_types


def _load_neighbourhoods(cursor: Any, folder: Path) -> int:
    geo_file = folder / "neighbourhoods.geojson"
    payload = json.loads(geo_file.read_text(encoding="utf-8-sig"))
    rows: list[tuple[str, str, str | None, str]] = []
    for feature in payload.get("features", []):
        properties = feature.get("properties") or {}
        name = _text(properties.get("neighbourhood"))
        geometry = feature.get("geometry")
        if name and geometry:
            rows.append((CITY_KEY, name, _text(properties.get("neighbourhood_group")),
                         json.dumps(geometry, ensure_ascii=False, separators=(",", ":"))))
    if not rows:
        raise ValueError(f"没有从 {geo_file} 读取到有效街区边界")
    batch = rows.copy()
    _insert_batch(
        cursor, "dim_neighbourhood",
        ("city_key", "neighbourhood", "neighbourhood_group", "geometry_json"), batch,
    )
    return len(rows)


def _load_category_dimensions(cursor: Any, room_types: set[str], property_types: set[str]) -> None:
    room_rows = [(value, index) for index, value in enumerate(sorted(room_types))]
    property_rows = [(value, index) for index, value in enumerate(sorted(property_types))]
    _insert_batch(cursor, "dim_room_type", ("room_type", "sort_order"), room_rows)
    _insert_batch(cursor, "dim_property_type", ("property_type", "sort_order"), property_rows)


def _listing_dimension_map(folder: Path) -> dict[int, tuple[str, str, str]]:
    listing_file = folder / "listings.csv.gz"
    result: dict[int, tuple[str, str, str]] = {}
    with gzip.open(listing_file, "rt", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            listing_id = _integer(row.get("id"))
            if listing_id is None:
                continue
            neighbourhood = _text(row.get("neighbourhood_cleansed")) or _text(row.get("neighbourhood")) or "未归类"
            room_type = _text(row.get("room_type")) or "未归类"
            property_type = _text(row.get("property_type")) or "未归类"
            result[listing_id] = neighbourhood, room_type, property_type
    return result


def _load_availability(cursor: Any, folder: Path) -> tuple[int, int, date | None, date | None]:
    calendar_file = folder / "calendar.csv.gz"
    if not calendar_file.is_file():
        raise FileNotFoundError(f"快照缺少日历文件：{calendar_file}")
    listing_map = _listing_dimension_map(folder)
    aggregates: dict[tuple[date, str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    source_rows = 0
    min_date: date | None = None
    max_date: date | None = None
    with gzip.open(calendar_file, "rt", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            source_rows += 1
            calendar_date = _date(row.get("date"))
            if calendar_date is None:
                continue
            min_date = calendar_date if min_date is None else min(min_date, calendar_date)
            max_date = calendar_date if max_date is None else max(max_date, calendar_date)
            listing_id = _integer(row.get("listing_id"))
            neighbourhood, room_type, property_type = listing_map.get(
                listing_id or -1, ("未归类", "未归类", "未归类")
            )
            values = aggregates[(calendar_date, neighbourhood, room_type, property_type)]
            values[0] += 1
            status = (_text(row.get("available")) or "").lower()
            if status in {"t", "true", "1"}:
                values[1] += 1
            elif status in {"f", "false", "0"}:
                values[2] += 1
            else:
                values[3] += 1

    batch: list[tuple[Any, ...]] = []
    for (calendar_date, neighbourhood, room_type, property_type), measures in aggregates.items():
        batch.append((CITY_KEY, _date(folder.name), calendar_date, neighbourhood, room_type, property_type, *measures))
        if len(batch) >= BATCH_SIZE:
            _insert_batch(cursor, "fact_calendar_availability_day", AVAILABILITY_COLUMNS, batch)
    if batch:
        _insert_batch(cursor, "fact_calendar_availability_day", AVAILABILITY_COLUMNS, batch)
    return source_rows, len(aggregates), min_date, max_date


def _load_reviews(cursor: Any, folder: Path) -> tuple[int, int, date | None, date | None]:
    review_file = folder / "reviews.csv.gz"
    if not review_file.is_file():
        raise FileNotFoundError(f"快照缺少评论文件：{review_file}")
    counts: Counter[date] = Counter()
    listings_by_month: dict[date, set[int]] = defaultdict(set)
    bounds_by_month: dict[date, list[date]] = {}
    min_review: date | None = None
    max_review: date | None = None
    source_rows = 0
    with gzip.open(review_file, "rt", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            source_rows += 1
            review_date = _date(row.get("date"))
            if review_date is None:
                continue
            month = review_date.replace(day=1)
            counts[month] += 1
            if listing_id := _integer(row.get("listing_id")):
                listings_by_month[month].add(listing_id)
            if month not in bounds_by_month:
                bounds_by_month[month] = [review_date, review_date]
            else:
                bounds_by_month[month][0] = min(bounds_by_month[month][0], review_date)
                bounds_by_month[month][1] = max(bounds_by_month[month][1], review_date)
            min_review = review_date if min_review is None else min(min_review, review_date)
            max_review = review_date if max_review is None else max(max_review, review_date)

    batch: list[tuple[Any, ...]] = []
    for month in sorted(counts):
        # Month-level activity uses only the latest cumulative review export; prior snapshots are not appended.
        earliest, latest = bounds_by_month[month]
        batch.append((CITY_KEY, month, counts[month], len(listings_by_month[month]), earliest, latest))
        if len(batch) >= BATCH_SIZE:
            _insert_batch(cursor, "fact_review_monthly", REVIEW_COLUMNS, batch)
    _insert_batch(cursor, "fact_review_monthly", REVIEW_COLUMNS, batch)
    return source_rows, len(counts), min_review, max_review


def _date_dimension_rows(start: date, end: date) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    current = start
    weekday_names = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
    while current <= end:
        rows.append((current, current.year, (current.month - 1) // 3 + 1, current.month,
                     current.day, current.isoweekday(), weekday_names[current.weekday()], current.weekday() >= 5))
        current += timedelta(days=1)
    return rows


def _load_date_dimension(
    cursor: Any, min_calendar_date: date | None, max_calendar_date: date | None, min_review_date: date | None,
) -> int:
    starts = [value for value in (min_calendar_date, min_review_date.replace(day=1) if min_review_date else None)
              if value is not None]
    ends = [value for value in (max_calendar_date,) if value is not None]
    if not starts or not ends:
        return 0
    rows = _date_dimension_rows(min(starts), max(ends))
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        _insert_batch(
            cursor, "dim_date",
            ("calendar_date", "calendar_year", "calendar_quarter", "calendar_month", "day_of_month",
             "day_of_week", "day_name", "is_weekend"), batch,
        )
    return len(rows)


def load_hong_kong() -> dict[str, Any]:
    """把香港房源、维度和轻量汇总事实幂等写入 Doris。"""
    settings = load_settings()
    folders = _snapshot_directories()
    results: list[dict[str, Any]] = []
    all_room_types: set[str] = set()
    all_property_types: set[str] = set()
    min_calendar_date: date | None = None
    max_calendar_date: date | None = None
    with connect_doris(settings) as connection:
        _apply_schema(connection)
        with connection.cursor() as cursor:
            _load_city(cursor)
            neighbourhood_count = _load_neighbourhoods(cursor, folders[-1])
            for folder in folders:
                listing_count, host_count, room_types, property_types = _load_snapshot(cursor, folder)
                all_room_types.update(room_types)
                all_property_types.update(property_types)
                calendar_source_rows, calendar_aggregate_rows, cal_min, cal_max = _load_availability(cursor, folder)
                min_calendar_date = min(value for value in (min_calendar_date, cal_min) if value is not None) if any(
                    value is not None for value in (min_calendar_date, cal_min)
                ) else None
                max_calendar_date = max(value for value in (max_calendar_date, cal_max) if value is not None) if any(
                    value is not None for value in (max_calendar_date, cal_max)
                ) else None
                results.append({
                    "snapshot_date": folder.name,
                    "listing_rows": listing_count,
                    "host_rows": host_count,
                    "calendar_source_rows": calendar_source_rows,
                    "calendar_aggregate_rows": calendar_aggregate_rows,
                })
                print(
                    f"已加载 {folder.name}: 房源 {listing_count:,} 行、房东 {host_count:,} 行，"
                    f"日历 {calendar_source_rows:,} 行汇总为 {calendar_aggregate_rows:,} 行"
                )

            _load_category_dimensions(cursor, all_room_types, all_property_types)
            latest = folders[-1]
            review_source_rows, review_month_rows, min_review_date, max_review_date = _load_reviews(cursor, latest)
            date_rows = _load_date_dimension(cursor, min_calendar_date, max_calendar_date, min_review_date)

    return {
        "database": DATABASE,
        "neighbourhood_rows": neighbourhood_count,
        "room_type_rows": len(all_room_types),
        "property_type_rows": len(all_property_types),
        "review_source_rows": review_source_rows,
        "review_month_rows": review_month_rows,
        "date_rows": date_rows,
        "snapshots": results,
    }


def _source_field_selection() -> list[str]:
    fields = {"id", *SOURCE_FIELDS.values(), *HOST_SOURCE_FIELDS.values()}
    fields.update({"neighbourhood", "neighbourhood_cleansed"})
    return sorted(fields)


def _local_host_fields(frame: Any) -> None:
    import pandas as pd

    for column in ("host_is_superhost", "host_identity_verified"):
        if column in frame:
            frame[column] = frame[column].map(_boolean).astype("boolean")
    for column in ("host_since",):
        if column in frame:
            frame[column] = pd.to_datetime(frame[column], errors="coerce").dt.date
    for column in (
        "host_listings_count", "host_total_listings_count", "calculated_host_listings_count",
        "calculated_host_listings_count_entire_homes", "calculated_host_listings_count_private_rooms",
        "calculated_host_listings_count_shared_rooms",
    ):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("host_response_rate", "host_acceptance_rate"):
        if column in frame:
            frame[column] = pd.to_numeric(
                frame[column].astype("string").str.replace("%", "", regex=False), errors="coerce"
            )


def load_local_dashboard_data():
    """从香港本地 GZIP CSV 读取房源和房东快照。"""
    import pandas as pd

    frames = []
    usecols = set(_source_field_selection())
    for folder in _snapshot_directories():
        listing_file = folder / "listings.csv.gz"
        frame = pd.read_csv(
            listing_file, compression="gzip", usecols=lambda field: field in usecols, low_memory=False,
        )
        frame = frame.rename(columns={"id": "listing_id"})
        cleaned_neighbourhood = frame.get(
            "neighbourhood_cleansed", pd.Series(index=frame.index, dtype="string")
        ).astype("string").str.strip().replace("", pd.NA)
        source_neighbourhood = frame.get(
            "neighbourhood", pd.Series(index=frame.index, dtype="string")
        ).astype("string").str.strip().replace("", pd.NA)
        frame["neighbourhood"] = cleaned_neighbourhood.fillna(source_neighbourhood)
        frame["city_key"] = CITY_KEY
        frame["snapshot_date"] = folder.name
        if "price" in frame:
            frame["price"] = pd.to_numeric(
                frame["price"].astype("string").str.replace(r"[^0-9.\-]", "", regex=True), errors="coerce"
            )
        for column in INTEGER_FIELDS:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        for column in DECIMAL_FIELDS - {"price"}:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        _local_host_fields(frame)
        frames.append(frame)
    listings = pd.concat(frames, ignore_index=True)
    return listings, _load_local_geojson(_snapshot_directories()[-1])


def _load_local_geojson(folder: Path) -> dict[str, Any]:
    path = folder / "neighbourhoods.geojson"
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_local_availability_data():
    """流式汇总日历，避免把数百万条房态明细整体放入内存。"""
    import pandas as pd

    frames = []
    for folder in _snapshot_directories():
        listing_map = _listing_dimension_map(folder)
        counts: dict[tuple[str, str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        with gzip.open(folder / "calendar.csv.gz", "rt", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                cal_date = _date(row.get("date"))
                if cal_date is None:
                    continue
                listing_id = _integer(row.get("listing_id"))
                neighbourhood, room_type, property_type = listing_map.get(
                    listing_id or -1, ("未归类", "未归类", "未归类")
                )
                measure = counts[(cal_date.isoformat(), neighbourhood, room_type, property_type)]
                measure[0] += 1
                status = (_text(row.get("available")) or "").lower()
                if status in {"t", "true", "1"}:
                    measure[1] += 1
                elif status in {"f", "false", "0"}:
                    measure[2] += 1
                else:
                    measure[3] += 1
        rows = [
            (folder.name, cal_date, neighbourhood, room_type, property_type, *measures)
            for (cal_date, neighbourhood, room_type, property_type), measures in counts.items()
        ]
        frames.append(pd.DataFrame(rows, columns=(
            "snapshot_date", "calendar_date", "neighbourhood", "room_type", "property_type", "listing_count",
            "available_listing_count", "unavailable_listing_count", "unknown_status_count",
        )))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_local_review_data():
    """只汇总最新累积评论文件；评论文本和评论者标识不读取到结果中。"""
    import pandas as pd

    folder = _snapshot_directories()[-1]
    counts: Counter[date] = Counter()
    listings_by_month: dict[date, set[int]] = defaultdict(set)
    date_bounds: dict[date, list[date]] = {}
    with gzip.open(folder / "reviews.csv.gz", "rt", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            review_date = _date(row.get("date"))
            if review_date is None:
                continue
            month = review_date.replace(day=1)
            counts[month] += 1
            if month not in date_bounds:
                date_bounds[month] = [review_date, review_date]
            else:
                date_bounds[month][0] = min(date_bounds[month][0], review_date)
                date_bounds[month][1] = max(date_bounds[month][1], review_date)
            if listing_id := _integer(row.get("listing_id")):
                listings_by_month[month].add(listing_id)
    return pd.DataFrame([
        {
            "review_month": month,
            "review_count": count,
            "reviewed_listing_count": len(listings_by_month[month]),
            "earliest_review_date": date_bounds[month][0],
            "latest_review_date": date_bounds[month][1],
        }
        for month, count in sorted(counts.items())
    ])


def _geojson_from_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    features = []
    for row in rows:
        geometry_value = row.get("geometry_json")
        if not geometry_value:
            continue
        geometry = json.loads(geometry_value) if isinstance(geometry_value, str) else geometry_value
        features.append({
            "type": "Feature", "geometry": geometry,
            "properties": {
                "neighbourhood": row.get("neighbourhood"),
                "neighbourhood_group": row.get("neighbourhood_group"),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def load_warehouse_dashboard_data():
    """从 Doris 读取房源事实、维度属性和两张汇总事实表。"""
    import pandas as pd

    settings = load_settings()
    with connect_doris(settings, database=DATABASE) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT snapshot_date, COUNT(*) AS row_count FROM fact_listing_snapshot "
                "WHERE city_key = %s GROUP BY snapshot_date ORDER BY snapshot_date", (CITY_KEY,),
            )
            snapshot_counts = {
                str(row["snapshot_date"]): int(row["row_count"]) for row in cursor.fetchall()
            }
            if snapshot_counts != EXPECTED_LISTING_COUNTS:
                raise RuntimeError("Doris 房源快照行数未达到本地先锋数据的完整性基线")
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM dim_neighbourhood WHERE city_key = %s", (CITY_KEY,),
            )
            neighbourhood_count = int((cursor.fetchone() or {}).get("row_count", 0))
            if neighbourhood_count < EXPECTED_NEIGHBOURHOODS:
                raise RuntimeError("Doris 街区维度未达到本地先锋数据的完整性基线")
            cursor.execute("SELECT COUNT(*) AS row_count FROM fact_calendar_availability_day")
            availability_count = int((cursor.fetchone() or {}).get("row_count", 0))
            cursor.execute("SELECT COUNT(*) AS row_count FROM fact_review_monthly")
            review_month_count = int((cursor.fetchone() or {}).get("row_count", 0))
            if availability_count == 0 or review_month_count == 0:
                raise RuntimeError("Doris 日历或评论汇总事实尚未装载完成")
            cursor.execute(
                "SELECT f.*, h.host_is_superhost, h.host_identity_verified, h.host_since, "
                "h.calculated_host_listings_count AS host_city_listing_count "
                "FROM fact_listing_snapshot f LEFT JOIN dim_host_snapshot h "
                "ON f.city_key = h.city_key AND f.snapshot_date = h.snapshot_date AND f.host_id = h.host_id "
                "WHERE f.city_key = %s ORDER BY f.snapshot_date, f.listing_id", (CITY_KEY,),
            )
            listing_rows = list(cursor.fetchall())
            cursor.execute(
                "SELECT neighbourhood, neighbourhood_group, geometry_json FROM dim_neighbourhood "
                "WHERE city_key = %s ORDER BY neighbourhood", (CITY_KEY,),
            )
            neighbourhood_rows = list(cursor.fetchall())
            cursor.execute(
                "SELECT snapshot_date, calendar_date, neighbourhood, room_type, property_type, listing_count, "
                "available_listing_count, unavailable_listing_count, unknown_status_count "
                "FROM fact_calendar_availability_day ORDER BY snapshot_date, calendar_date"
            )
            availability_rows = list(cursor.fetchall())
            cursor.execute(
                "SELECT review_month, review_count, reviewed_listing_count, earliest_review_date, latest_review_date "
                "FROM fact_review_monthly ORDER BY review_month"
            )
            review_rows = list(cursor.fetchall())
    if not listing_rows or not neighbourhood_rows:
        raise RuntimeError("Doris 先锋主题表为空；先执行 pixi run data-load-hong-kong")
    return (
        pd.DataFrame(listing_rows), _geojson_from_rows(neighbourhood_rows),
        pd.DataFrame(availability_rows), pd.DataFrame(review_rows),
    )


def load_dashboard_data():
    """优先读取 Doris；连接不可用或主题表不完整时退回本地流式预览。"""
    import os

    mode = os.environ.get("HK_PILOT_DATA_MODE", "auto").strip().lower()
    if mode not in {"auto", "warehouse", "local"}:
        raise ValueError("HK_PILOT_DATA_MODE 只接受 auto、warehouse 或 local")
    if mode != "local":
        try:
            warehouse_data = load_warehouse_dashboard_data()
            return (*warehouse_data, "Doris 数据仓库", None)
        except Exception:  # noqa: BLE001
            if mode == "warehouse":
                raise
    listings, geojson = load_local_dashboard_data()
    availability = load_local_availability_data()
    reviews = load_local_review_data()
    message = None if mode == "local" else (
        "Doris 主题表尚未完整，页面当前使用本地快照和流式日历/评论汇总预览。"
    )
    return listings, geojson, availability, reviews, "本地预览", message


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hk-airbnb-pilot", description="香港先锋数据集处理")
    parser.add_argument("command", choices=("load",), help="将香港快照、维度和汇总事实装载到 Doris")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "load":
            result = load_hong_kong()
            listing_total = sum(item["listing_rows"] for item in result["snapshots"])
            calendar_total = sum(item["calendar_aggregate_rows"] for item in result["snapshots"])
            print(
                f"完成：{result['database']}；房源 {listing_total:,} 行，街区 {result['neighbourhood_rows']} 个，"
                f"房型 {result['room_type_rows']} 类，房屋类型 {result['property_type_rows']} 类，"
                f"日历汇总 {calendar_total:,} 行，评论月汇总 {result['review_month_rows']:,} 行。"
            )
            return 0
    except Exception as exc:  # noqa: BLE001
        print(f"香港数据装载失败：{exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
