"""Inside Airbnb 文件清单、下载、完整性检查与结构盘点。"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO
from urllib.parse import urlparse

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = PROJECT_ROOT / "docs" / "附件-数据下载校验清单.csv"
DEFAULT_RAW_ROOT = PROJECT_ROOT / "data" / "raw" / "inside_airbnb"
CHINESE_CITY = {"hong_kong": "香港", "tokyo": "东京", "singapore": "新加坡"}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SourceFile:
    city: str
    city_key: str
    snapshot_date: str
    file_name: str
    relative_path: str
    source_url: str
    size_bytes: int
    sha256: str
    status: str
    local_path: Path


def manifest_path(override: str | Path | None = None) -> Path:
    value = override or os.environ.get("INSIDE_AIRBNB_MANIFEST")
    if value:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    return DEFAULT_MANIFEST


def load_manifest(path: str | Path | None = None) -> list[SourceFile]:
    source = manifest_path(path)
    if not source.is_file():
        raise FileNotFoundError(f"找不到 Inside Airbnb 文件清单：{source}")

    raw_root = DEFAULT_RAW_ROOT.resolve()
    entries: list[SourceFile] = []
    seen: set[str] = set()
    with source.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {
            "city", "city_key", "snapshot_date", "file_name", "relative_path",
            "source_url", "bytes", "sha256", "status",
        }
        missing_columns = required.difference(reader.fieldnames or [])
        if missing_columns:
            raise ValueError(f"清单缺少必要列：{', '.join(sorted(missing_columns))}")

        for line_number, row in enumerate(reader, start=2):
            relative = Path(row["relative_path"])
            local_path = relative.resolve() if relative.is_absolute() else (PROJECT_ROOT / relative).resolve()
            if local_path != raw_root and raw_root not in local_path.parents:
                raise ValueError(f"清单第 {line_number} 行的落盘路径超出 Inside Airbnb 原始数据目录")
            relative_key = local_path.as_posix()
            if relative_key in seen:
                raise ValueError(f"清单重复记录落盘路径：{relative_key}")
            seen.add(relative_key)

            parsed_url = urlparse(row["source_url"])
            host = (parsed_url.hostname or "").casefold()
            if parsed_url.scheme != "https" or not (
                host == "insideairbnb.com" or host.endswith(".insideairbnb.com")
            ):
                raise ValueError(f"清单第 {line_number} 行不是 Inside Airbnb HTTPS 下载地址")

            digest = row["sha256"].strip().lower()
            if not SHA256_PATTERN.fullmatch(digest):
                raise ValueError(f"清单第 {line_number} 行的 SHA-256 格式无效")
            try:
                size_bytes = int(row["bytes"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"清单第 {line_number} 行的文件大小无效") from exc
            if size_bytes <= 0:
                raise ValueError(f"清单第 {line_number} 行的文件大小必须大于 0")

            entries.append(SourceFile(
                city=row["city"].strip(),
                city_key=row["city_key"].strip(),
                snapshot_date=row["snapshot_date"].strip(),
                file_name=row["file_name"].strip(),
                relative_path=row["relative_path"].strip(),
                source_url=row["source_url"].strip(),
                size_bytes=size_bytes,
                sha256=digest,
                status=row["status"].strip(),
                local_path=local_path,
            ))
    if not entries:
        raise ValueError("Inside Airbnb 文件清单为空")
    return entries


def select_entries(
    entries: Iterable[SourceFile],
    city: str | None = None,
    snapshot: str | None = None,
) -> list[SourceFile]:
    selected = list(entries)
    if city:
        key = city.strip().casefold()
        selected = [item for item in selected if key in {item.city_key.casefold(), item.city.casefold()}]
    if snapshot:
        selected = [item for item in selected if item.snapshot_date == snapshot]
    if not selected:
        raise ValueError("筛选条件没有匹配到任何数据文件")
    return selected


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_entries(entries: Iterable[SourceFile]) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for item in entries:
        result: dict[str, object] = {
            "city": item.city,
            "snapshot_date": item.snapshot_date,
            "file_name": item.file_name,
            "relative_path": item.relative_path,
            "expected_bytes": item.size_bytes,
            "expected_sha256": item.sha256,
            "status": "verified",
            "actual_bytes": None,
            "actual_sha256": None,
        }
        if not item.local_path.is_file():
            result["status"] = "missing"
        else:
            actual_bytes = item.local_path.stat().st_size
            result["actual_bytes"] = actual_bytes
            if actual_bytes != item.size_bytes:
                result["status"] = "size_mismatch"
            else:
                actual_sha = sha256_file(item.local_path)
                result["actual_sha256"] = actual_sha
                if actual_sha != item.sha256:
                    result["status"] = "sha256_mismatch"
        results.append(result)
    return results


def fetch_entries(entries: Iterable[SourceFile], timeout_seconds: float | None = None) -> list[dict[str, object]]:
    timeout = timeout_seconds or float(os.environ.get("INSIDE_AIRBNB_TIMEOUT", "90"))
    timeout_config = httpx.Timeout(timeout, connect=min(timeout, 20.0))
    results: list[dict[str, object]] = []
    with httpx.Client(follow_redirects=True, timeout=timeout_config) as client:
        for item in entries:
            if item.local_path.is_file() and item.local_path.stat().st_size == item.size_bytes:
                if sha256_file(item.local_path) == item.sha256:
                    results.append({"relative_path": item.relative_path, "status": "reused"})
                    continue

            item.local_path.parent.mkdir(parents=True, exist_ok=True)
            partial = item.local_path.with_name(item.local_path.name + ".part")
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    with client.stream("GET", item.source_url) as response:
                        response.raise_for_status()
                        with partial.open("wb") as output:
                            for chunk in response.iter_bytes(chunk_size=1024 * 512):
                                output.write(chunk)
                    actual_bytes = partial.stat().st_size
                    actual_sha = sha256_file(partial)
                    if actual_bytes != item.size_bytes or actual_sha != item.sha256:
                        raise ValueError(
                            f"下载校验不匹配（字节数 {actual_bytes}/{item.size_bytes}，SHA-256 {actual_sha}/{item.sha256}）"
                        )
                    partial.replace(item.local_path)
                    results.append({"relative_path": item.relative_path, "status": "downloaded"})
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    partial.unlink(missing_ok=True)
                    last_error = exc
                    if attempt < 2:
                        time.sleep(2 ** attempt)
            if last_error is not None:
                results.append({
                    "relative_path": item.relative_path,
                    "status": "failed",
                    "error": str(last_error),
                })
    return results


def _open_csv(path: Path) -> TextIO:
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def inspect_entry(item: SourceFile) -> dict[str, object]:
    if not item.local_path.is_file():
        raise FileNotFoundError(item.local_path)
    if item.file_name.endswith(".geojson"):
        payload = json.loads(item.local_path.read_text(encoding="utf-8-sig"))
        features = payload.get("features", [])
        properties = {key for feature in features for key in feature.get("properties", {})}
        geometry_types = Counter(
            feature.get("geometry", {}).get("type", "unknown")
            for feature in features
        )
        geometry_summary = "; ".join(
            f"geometry={kind}" for kind, _ in sorted(geometry_types.items())
        )
        return {
            "city": item.city,
            "snapshot_date": item.snapshot_date,
            "file": item.file_name,
            "records": len(features),
            "columns": len(properties),
            "date_min": "",
            "date_max": "",
            "available_values": geometry_summary,
            "fields": sorted(properties),
            "malformed_rows": 0,
        }

    with _open_csv(item.local_path) as stream:
        reader = csv.DictReader(stream)
        fields = list(reader.fieldnames or [])
        if not fields:
            raise ValueError(f"CSV 文件没有表头：{item.relative_path}")
        records = 0
        malformed_rows = 0
        date_min = ""
        date_max = ""
        availability: Counter[str] = Counter()
        for row in reader:
            records += 1
            if None in row or any(value is None for value in row.values()):
                malformed_rows += 1
            date_value = row.get("date") if item.file_name.endswith(".gz") else None
            if date_value:
                date_min = min(date_min, date_value) if date_min else date_value
                date_max = max(date_max, date_value) if date_max else date_value
            available_value = row.get("available")
            if available_value:
                availability[available_value] += 1
        return {
            "city": item.city,
            "snapshot_date": item.snapshot_date,
            "file": item.file_name,
            "records": records,
            "columns": len(fields),
            "date_min": date_min,
            "date_max": date_max,
            "available_values": (
                json.dumps(dict(sorted(availability.items())), ensure_ascii=False)
                if availability or item.file_name.endswith(".gz") else ""
            ),
            "fields": fields,
            "malformed_rows": malformed_rows,
        }


def inspect_entries(entries: Iterable[SourceFile]) -> list[dict[str, object]]:
    return [inspect_entry(item) for item in entries]


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
