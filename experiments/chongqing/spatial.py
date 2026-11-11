"""Prepare the independent Chongqing trajectory and road tables for the experiment."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass, field
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
from typing import Any
import zipfile


CITY = "重庆市"
PAIRED_SOURCES = {
    "delivery": "delivery_five_cities.csv",
    "pickup": "pickup_five_cities.csv",
}
ARCHIVE = "courier_detailed_trajectory.zip"
ZIP_MEMBER = "courier_detailed_trajectory.csv"
ROAD_SOURCE = "road-network/roads.csv"
DAY_KEY = tuple[str, str]

SCHEMAS = {
    "b_orders": {
        "business_type": "VARCHAR(16)",
        "order_id": "VARCHAR(64)",
        "courier_id": "VARCHAR(64)",
        "ds": "VARCHAR(4)",
        "month_day": "VARCHAR(5)",
        "region_id": "VARCHAR(64)",
        "aoi_id": "VARCHAR(64)",
        "aoi_type": "VARCHAR(64)",
        "accepted_at": "VARCHAR(32)",
        "completed_at": "VARCHAR(32)",
        "window_start": "VARCHAR(32)",
        "window_end": "VARCHAR(32)",
        "source_x": "DOUBLE",
        "source_y": "DOUBLE",
        "source_file": "VARCHAR(128)",
        "source_line": "BIGINT",
    },
    "b_trajectory_points": {
        "courier_id": "VARCHAR(64)",
        "ds": "VARCHAR(4)",
        "month_day": "VARCHAR(5)",
        "gps_time": "VARCHAR(32)",
        "source_x": "DOUBLE",
        "source_y": "DOUBLE",
        "source_line": "BIGINT",
    },
    "b_trajectory_days": {
        "courier_id": "VARCHAR(64)",
        "ds": "VARCHAR(4)",
        "month_day": "VARCHAR(5)",
        "delivery_order_count": "BIGINT",
        "pickup_order_count": "BIGINT",
        "trajectory_point_count": "BIGINT",
        "first_gps_time": "VARCHAR(32)",
        "last_gps_time": "VARCHAR(32)",
    },
    "b_sample_polylines": {
        "sample_id": "VARCHAR(16)",
        "business_type": "VARCHAR(16)",
        "courier_id": "VARCHAR(64)",
        "ds": "VARCHAR(4)",
        "month_day": "VARCHAR(5)",
        "point_index": "BIGINT",
        "gps_time": "VARCHAR(32)",
        "relative_x": "DOUBLE",
        "relative_y": "DOUBLE",
    },
    "b_coverage": {
        "business_type": "VARCHAR(16)",
        "city": "VARCHAR(32)",
        "source_file": "VARCHAR(128)",
        "source_order_count": "BIGINT",
        "city_order_count": "BIGINT",
        "matched_order_count": "BIGINT",
        "courier_day_count": "BIGINT",
        "matched_courier_day_count": "BIGINT",
        "match_rate_pct": "DOUBLE",
        "trajectory_point_count": "BIGINT",
    },
    "c_roads": {
        "source_line": "BIGINT",
        "osm_id": "BIGINT",
        "fclass": "VARCHAR(64)",
        "oneway": "VARCHAR(4)",
        "bridge": "VARCHAR(4)",
        "tunnel": "VARCHAR(4)",
        "geometry_wkt": "VARCHAR(65533)",
    },
    "c_road_classes": {
        "fclass": "VARCHAR(64)",
        "road_count": "BIGINT",
        "oneway_both": "BIGINT",
        "oneway_forward": "BIGINT",
        "oneway_reverse": "BIGINT",
        "oneway_other": "BIGINT",
        "bridge_count": "BIGINT",
        "tunnel_count": "BIGINT",
    },
}


@dataclass
class CourierDay:
    orders: Counter[str] = field(default_factory=Counter)
    point_count: int = 0
    first_time: str = ""
    last_time: str = ""

    def add_point(self, gps_time: str) -> None:
        self.point_count += 1
        if gps_time:
            self.first_time = min(self.first_time or gps_time, gps_time)
            self.last_time = max(self.last_time, gps_time)


def month_day(ds: str) -> str:
    return f"{ds[:2]}-{ds[2:]}"


def finite_text(text: str) -> str:
    if not text:
        return ""
    return text if math.isfinite(float(text)) else ""


def prepare_paired_orders(
    raw_dir: Path, stage: Path
) -> tuple[dict[DAY_KEY, set[str]], dict[DAY_KEY, CourierDay], dict[str, dict[str, int]]]:
    city_by_day: dict[DAY_KEY, set[str]] = defaultdict(set)
    city_days: dict[DAY_KEY, CourierDay] = defaultdict(CourierDay)
    counts = {}
    with (stage / "b_orders.csv").open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(SCHEMAS["b_orders"])
        for business, filename in PAIRED_SOURCES.items():
            source_rows = city_rows = 0
            with (raw_dir / filename).open(encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source, strict=True)
                for row in reader:
                    source_rows += 1
                    city = row["from_city_name"]
                    ds = row["ds"].zfill(4)
                    key = row["delivery_user_id"], ds
                    city_by_day[key].add(city)
                    if city != CITY:
                        continue
                    city_rows += 1
                    city_days[key].orders[business] += 1
                    is_pickup = business == "pickup"
                    writer.writerow(
                        (
                            business, row["order_id"], key[0], ds, month_day(ds),
                            row["from_dipan_id"], row["aoi_id"], row["typecode"],
                            row["accept_time" if is_pickup else "receipt_time"],
                            row["got_time" if is_pickup else "sign_time"],
                            row["book_start_time"] if is_pickup else "",
                            row["expect_got_time"] if is_pickup else "",
                            finite_text(row["poi_lng"]), finite_text(row["poi_lat"]),
                            filename, reader.line_num,
                        )
                    )
            counts[business] = {"source_order_count": source_rows, "city_order_count": city_rows}
    return city_by_day, city_days, counts


def prepare_trajectory(
    raw_dir: Path,
    stage: Path,
    city_by_day: dict[DAY_KEY, set[str]],
    city_days: dict[DAY_KEY, CourierDay],
) -> dict[str, Any]:
    all_rows = city_rows = unknown_city_rows = ambiguous_city_rows = missing_coordinate_rows = 0
    city_label = {CITY}
    with (
        zipfile.ZipFile(raw_dir / ARCHIVE) as archive,
        archive.open(ZIP_MEMBER) as member,
        io.TextIOWrapper(member, encoding="utf-8", newline="") as source,
        (stage / "b_trajectory_points.csv").open("w", encoding="utf-8", newline="") as out,
    ):
        member_info = archive.getinfo(ZIP_MEMBER)
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(SCHEMAS["b_trajectory_points"])
        reader = csv.reader(source, strict=True)
        if next(reader) != ["ds", "postman_id", "gps_time", "lat", "lng"]:
            raise ValueError(f"Unexpected header in {ZIP_MEMBER}")
        for raw_ds, courier, gps_time, lat, lng in reader:
            all_rows += 1
            ds = raw_ds.zfill(4)
            key = courier, ds
            cities = city_by_day.get(key)
            if not cities:
                unknown_city_rows += 1
            elif len(cities) > 1:
                ambiguous_city_rows += 1
            elif cities == city_label:
                x, y = finite_text(lng), finite_text(lat)
                missing_coordinate_rows += not (x and y)
                writer.writerow((courier, ds, month_day(ds), gps_time, x, y, reader.line_num))
                city_days[key].add_point(gps_time)
                city_rows += 1
    return {
        "archive_member": ZIP_MEMBER,
        "archive_member_bytes": member_info.file_size,
        "archive_member_crc32": f"{member_info.CRC:08x}",
        "source_trajectory_point_count": all_rows,
        "city_trajectory_point_count": city_rows,
        "unknown_city_point_count": unknown_city_rows,
        "ambiguous_city_point_count": ambiguous_city_rows,
        "other_city_point_count": all_rows - city_rows - unknown_city_rows - ambiguous_city_rows,
        "city_point_missing_coordinate_count": missing_coordinate_rows,
    }


def write_day_and_coverage_tables(
    stage: Path,
    city_days: dict[DAY_KEY, CourierDay],
    counts: dict[str, dict[str, int]],
) -> dict[str, dict[str, int | float]]:
    with (stage / "b_trajectory_days.csv").open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(SCHEMAS["b_trajectory_days"])
        for (courier, ds), day in sorted(city_days.items()):
            writer.writerow(
                (
                    courier, ds, month_day(ds), day.orders["delivery"], day.orders["pickup"],
                    day.point_count, day.first_time, day.last_time,
                )
            )
    coverage = {}
    with (stage / "b_coverage.csv").open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(SCHEMAS["b_coverage"])
        for business, filename in PAIRED_SOURCES.items():
            days = [day for day in city_days.values() if day.orders[business]]
            matched_days = [day for day in days if day.point_count]
            metrics = {
                **counts[business],
                "matched_order_count": sum(day.orders[business] for day in matched_days),
                "courier_day_count": len(days),
                "matched_courier_day_count": len(matched_days),
                "match_rate_pct": round(100 * len(matched_days) / len(days), 6) if days else 0.0,
                "trajectory_point_count": sum(day.point_count for day in matched_days),
            }
            coverage[business] = metrics
            writer.writerow((business, CITY, filename, *metrics.values()))
    return coverage


def write_polylines(stage: Path, city_days: dict[DAY_KEY, CourierDay]) -> int:
    # Choose up to two different courier days per topic, sorted by point count.
    selected: dict[DAY_KEY, str] = {}
    for business in PAIRED_SOURCES:
        candidates = sorted(
            (key for key, day in city_days.items() if day.point_count > 1 and day.orders[business]),
            key=lambda key: (-city_days[key].point_count, key),
        )
        selected.update((key, business) for key in [k for k in candidates if k not in selected][:2])

    points: dict[DAY_KEY, list[tuple[str, int, float, float]]] = {
        key: [] for key in selected
    }
    with (stage / "b_trajectory_points.csv").open(encoding="utf-8", newline="") as source:
        reader = csv.reader(source)
        next(reader)
        for courier, ds, _, gps_time, x, y, source_line in reader:
            sample = points.get((courier, ds))
            if sample is not None and gps_time and x and y:
                sample.append((gps_time, int(source_line), float(x), float(y)))

    rows = 0
    with (stage / "b_sample_polylines.csv").open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(SCHEMAS["b_sample_polylines"])
        for index, ((courier, ds), business) in enumerate(selected.items(), 1):
            full_path = sorted(points[courier, ds])
            if len(full_path) < 2:
                continue
            # Sample at even sequence positions, keeping both endpoints.
            point_count = min(len(full_path), 480)
            last = len(full_path) - 1
            _, _, x0, y0 = full_path[0]
            for point_index in range(point_count):
                time, _, x, y = full_path[point_index * last // (point_count - 1)]
                writer.writerow(
                    (
                        f"B{index:02d}", business, courier, ds, month_day(ds), point_index + 1,
                        time, x - x0, y - y0,
                    )
                )
                rows += 1
    return rows


def prepare_roads(raw_dir: Path, stage: Path) -> dict[str, Any]:
    categories: dict[str, Counter[str]] = defaultdict(Counter)
    geometry_types: Counter[str] = Counter()
    directions = {"B": "oneway_both", "F": "oneway_forward", "T": "oneway_reverse"}
    source_rows = city_rows = 0
    with (
        (raw_dir / ROAD_SOURCE).open(encoding="utf-8", newline="") as source,
        (stage / "c_roads.csv").open("w", encoding="utf-8", newline="") as out,
    ):
        reader = csv.DictReader(source, delimiter="\t", strict=True)
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(SCHEMAS["c_roads"])
        for row in reader:
            source_rows += 1
            if row["city"] != CITY:
                continue
            city_rows += 1
            stats = categories[row["fclass"]]
            stats["road_count"] += 1
            stats[directions.get(row["oneway"], "oneway_other")] += 1
            stats["bridge_count"] += row["bridge"] == "T"
            stats["tunnel_count"] += row["tunnel"] == "T"
            geometry = row["geometry"]
            geometry_types[geometry.partition(" ")[0] or "empty"] += 1
            if len(geometry.encode("utf-8")) > 65_533:
                raise ValueError(f"Road geometry exceeds the target width at row {reader.line_num}")
            writer.writerow(
                (
                    reader.line_num, int(row["osm_id"]), row["fclass"], row["oneway"],
                    row["bridge"], row["tunnel"], geometry,
                )
            )

    with (stage / "c_road_classes.csv").open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(SCHEMAS["c_road_classes"])
        metric_names = list(SCHEMAS["c_road_classes"])[1:]
        for road_class, stats in sorted(categories.items()):
            writer.writerow((road_class, *(stats[metric] for metric in metric_names)))
    return {
        "source_road_count": source_rows,
        "city_road_count": city_rows,
        "road_class_count": len(categories),
        "geometry_type_counts": dict(geometry_types),
    }


def file_evidence(path: Path) -> dict[str, int | str]:
    with path.open("rb") as source:
        return {"size_bytes": path.stat().st_size, "sha256": hashlib.file_digest(source, "sha256").hexdigest()}


def prepare_spatial(raw_dir: Path, output_dir: Path) -> dict[str, Any]:
    raw_dir = raw_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir.is_relative_to(raw_dir):
        raise ValueError("The output directory cannot be inside the raw data directory")
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".spatial-", dir=output_dir) as temp_dir:
        stage = Path(temp_dir)
        city_by_day, city_days, order_counts = prepare_paired_orders(raw_dir, stage)
        trajectory = prepare_trajectory(raw_dir, stage, city_by_day, city_days)
        coverage = write_day_and_coverage_tables(stage, city_days, order_counts)
        polyline_rows = write_polylines(stage, city_days)
        roads = prepare_roads(raw_dir, stage)
        output_counts = {
            "b_orders": sum(item["city_order_count"] for item in order_counts.values()),
            "b_trajectory_points": trajectory["city_trajectory_point_count"],
            "b_trajectory_days": len(city_days),
            "b_sample_polylines": polyline_rows,
            "b_coverage": len(coverage),
            "c_roads": roads["city_road_count"],
            "c_road_classes": roads["road_class_count"],
        }
        outputs = {}
        for name, schema in SCHEMAS.items():
            filename = f"{name}.csv"
            (stage / f"{name}.schema.json").write_text(
                json.dumps(schema, indent=2) + "\n", encoding="utf-8"
            )
            outputs[filename] = {"row_count": output_counts[name], **file_evidence(stage / filename)}
        manifest = {
            "city": CITY,
            "sources": {
                filename: file_evidence(raw_dir / filename)
                for filename in (*PAIRED_SOURCES.values(), ARCHIVE, ROAD_SOURCE)
            },
            "trajectory": trajectory,
            "coverage": coverage,
            "roads": roads,
            "outputs": outputs,
            "rules": {
                "trajectory_city": "The courier and zero-padded source ds must map to exactly one city across both paired order files.",
                "coverage": "Distinct matched courier-days divided by distinct city order courier-days, separately per business type.",
                "coverage_overlap": "The same point may count for both business types when the courier-day has both types of order.",
                "trajectory_coordinates": "B coordinates retain the source axes and units. Sample coordinates are offsets from the first chronological point.",
                "trajectory_samples": "Up to two distinct courier-days per business type, ranked by point count; up to 480 points per day, sorted by time and source row.",
                "trajectory_year": "Unverified; neither dates nor times are assigned a year.",
                "road_geometry": "Source WKT unchanged. CRS is not declared in the CSV; no point-to-road matching.",
                "line_numbers": "One-based physical source lines including the header (ZIP member for trajectory points).",
            },
        }
        (stage / "spatial_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for name in SCHEMAS:
            for suffix in (".csv", ".schema.json"):
                filename = f"{name}{suffix}"
                (stage / filename).replace(output_dir / filename)
        (stage / "spatial_manifest.json").replace(output_dir / "spatial_manifest.json")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/chongqing/prepared"))
    args = parser.parse_args()
    manifest = prepare_spatial(args.raw_dir, args.output_dir)
    print(json.dumps(manifest["outputs"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
