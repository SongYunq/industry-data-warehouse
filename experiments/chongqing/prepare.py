"""Prepare the Chongqing order detail and daily metrics from the LaDe city files."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data/raw"
PREPARED = ROOT / "data/chongqing/prepared"

ORDER_SCHEMA = {
    "business_type": "VARCHAR(12)", "completed_date": "DATE", "order_id": "BIGINT",
    "region_id": "BIGINT", "courier_id": "BIGINT", "aoi_id": "BIGINT", "aoi_type": "INT",
    "accepted_at": "DATETIME", "completed_at": "DATETIME", "completed_hour": "SMALLINT",
    "duration_seconds": "BIGINT", "window_start": "DATETIME", "window_end": "DATETIME",
    "appointment": "VARCHAR(16)", "target_lng": "DOUBLE", "target_lat": "DOUBLE",
    "accept_gps_m": "DOUBLE", "finish_gps_m": "DOUBLE", "source_ds": "VARCHAR(4)",
    "source_line": "BIGINT",
}
DAILY_SCHEMA = {
    "business_type": "VARCHAR(12)", "completed_date": "DATE", "orders": "BIGINT",
    "duration_n": "BIGINT", "duration_seconds_sum": "BIGINT", "gps_accept_n": "BIGINT",
    "gps_finish_n": "BIGINT", "appointment_early": "BIGINT", "appointment_in_window": "BIGINT",
    "appointment_late": "BIGINT",
}


def local_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(f"2022-{value}")
    except ValueError:
        return None


def coordinate(lng: str, lat: str) -> tuple[float, float] | None:
    try:
        x, y = float(lng), float(lat)
    except ValueError:
        return None
    if -180 <= x <= 180 and -90 <= y <= 90:
        return x, y
    return None


def distance_m(start: tuple[float, float], end: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, start)
    lon2, lat2 = map(math.radians, end)
    haversine = math.sin((lat2 - lat1) / 2) ** 2
    haversine += math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(12_742_000 * math.asin(math.sqrt(min(1, haversine))), 2)


def event_distance(row: dict[str, str], event: str, target: tuple[float, float] | None) -> float | None:
    position = coordinate(row[f"{event}_gps_lng"], row[f"{event}_gps_lat"])
    if target is not None and position is not None:
        return distance_m(target, position)
    return None


def prepare_orders(raw_dir: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    daily = defaultdict(Counter)
    audit = {}
    with TemporaryDirectory(dir=out, prefix="orders-") as temporary:
        staging = Path(temporary)
        with (staging / "dwd_orders.csv").open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(ORDER_SCHEMA)
            for business in ("delivery", "pickup"):
                file = raw_dir / business / f"{business}_cq.csv"
                seen = set()
                statistics = Counter()
                with file.open(encoding="utf-8-sig", newline="") as input_file:
                    for line, row in enumerate(csv.DictReader(input_file), start=2):
                        statistics["source_rows"] += 1
                        if row["city"] != "Chongqing":
                            raise ValueError(f"Unexpected city in {file.name} at line {line}")
                        order = int(row["order_id"])
                        if order in seen:
                            raise ValueError(f"Repeated order in {file.name} at line {line}")
                        seen.add(order)
                        accepted = local_time(row["accept_time"])
                        completed = local_time(row[f"{business}_time"])
                        if completed is None:
                            statistics["invalid_completion"] += 1
                            continue

                        duration = None
                        if accepted is not None and accepted <= completed:
                            duration = int((completed - accepted).total_seconds())
                            statistics["cross_midnight"] += accepted.date() != completed.date()
                        else:
                            statistics["invalid_duration"] += 1

                        start = end = None
                        appointment = "unavailable"
                        if business == "pickup":
                            start = local_time(row["time_window_start"])
                            end = local_time(row["time_window_end"])
                            if start is None or end is None or start > end:
                                appointment = "invalid"
                                statistics["invalid_window"] += 1
                            elif completed < start:
                                appointment = "early"
                            elif completed > end:
                                appointment = "late"
                            else:
                                appointment = "in_window"
                            statistics["appointment_" + appointment] += 1

                        target = coordinate(row["lng"], row["lat"])
                        accept_m = event_distance(row, "accept", target)
                        finish_m = event_distance(row, business, target)
                        day = completed.date().isoformat()
                        stats = daily[business, day]
                        stats["orders"] += 1
                        if duration is not None:
                            stats["duration_n"] += 1
                            stats["duration_seconds_sum"] += duration
                        stats["gps_accept_n"] += accept_m is not None
                        stats["gps_finish_n"] += finish_m is not None
                        stats["appointment_" + appointment] += 1
                        statistics["published_orders"] += 1
                        statistics["target_valid"] += target is not None
                        statistics["accept_gps_valid"] += accept_m is not None
                        statistics["finish_gps_valid"] += finish_m is not None
                        writer.writerow((
                            business, day, order, int(row["region_id"]), int(row["courier_id"]),
                            int(row["aoi_id"]), int(row["aoi_type"]), accepted, completed,
                            completed.hour, duration, start, end, appointment,
                            *(target or (None, None)), accept_m, finish_m, row["ds"], line,
                        ))
                audit[business] = {"source": str(file.relative_to(raw_dir)), **statistics}
                print(f"{file.name}: {statistics['published_orders']:,} orders", flush=True)

        with (staging / "dws_daily.csv").open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(DAILY_SCHEMA)
            for (business, day), stats in sorted(daily.items()):
                writer.writerow((business, day, *(stats[name] for name in tuple(DAILY_SCHEMA)[2:])))
        for name, schema in (("dwd_orders", ORDER_SCHEMA), ("dws_daily", DAILY_SCHEMA)):
            (staging / f"{name}.schema.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        (staging / "orders_manifest.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
        for file in staging.iterdir():
            file.replace(out / file.name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=RAW)
    parser.add_argument("--output-dir", type=Path, default=PREPARED)
    parser.add_argument("--orders-only", action="store_true")
    args = parser.parse_args()
    prepare_orders(args.raw_dir, args.output_dir)
    if not args.orders_only:
        from .spatial import prepare_spatial

        prepare_spatial(args.raw_dir, args.output_dir)


if __name__ == "__main__":
    main()
