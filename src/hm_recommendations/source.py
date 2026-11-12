"""H&M Kaggle 竞赛数据下载、完整性核验和结构盘点。"""

from __future__ import annotations

import csv
import hashlib
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPETITION = "h-and-m-personalized-fashion-recommendations"
SOURCE_PAGE = f"https://www.kaggle.com/competitions/{COMPETITION}/data"
DEFAULT_RAW_ROOT = PROJECT_ROOT / "data" / "raw" / "hm_recommendations"
DEFAULT_MANIFEST = PROJECT_ROOT / "docs" / "附件-数据下载校验清单.csv"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "docs"
DOWNLOAD_CACHE = PROJECT_ROOT / "data" / "staging" / "kaggle_hm"

MANIFEST_FIELDS = (
    "file_name", "source_page", "relative_path", "bytes", "sha256", "records", "columns", "status",
)


@dataclass(frozen=True)
class SourceFile:
    file_name: str
    relative_path: str
    expected_columns: tuple[str, ...]

    @property
    def local_path(self) -> Path:
        return PROJECT_ROOT / self.relative_path


FILES = (
    SourceFile(
        "articles.csv",
        "data/raw/hm_recommendations/articles.csv",
        (
            "article_id", "product_code", "prod_name", "product_type_no", "product_type_name",
            "product_group_name", "graphical_appearance_no", "graphical_appearance_name",
            "colour_group_code", "colour_group_name", "perceived_colour_value_id",
            "perceived_colour_value_name", "perceived_colour_master_id", "perceived_colour_master_name",
            "department_no", "department_name", "index_code", "index_name", "index_group_no",
            "index_group_name", "section_no", "section_name", "garment_group_no",
            "garment_group_name", "detail_desc",
        ),
    ),
    SourceFile(
        "customers.csv",
        "data/raw/hm_recommendations/customers.csv",
        (
            "customer_id", "FN", "Active", "club_member_status", "fashion_news_frequency", "age", "postal_code",
        ),
    ),
    SourceFile(
        "transactions_train.csv",
        "data/raw/hm_recommendations/transactions_train.csv",
        ("t_dat", "customer_id", "article_id", "price", "sales_channel_id"),
    ),
)
FILES_BY_NAME = {item.file_name: item for item in FILES}


def select_files(names: Iterable[str] | None = None) -> list[SourceFile]:
    if names is None:
        return list(FILES)
    selected: list[SourceFile] = []
    seen: set[str] = set()
    for name in names:
        normalized = Path(name).name
        if normalized not in FILES_BY_NAME:
            raise ValueError(f"不支持的 H&M 数据文件：{name}")
        if normalized not in seen:
            selected.append(FILES_BY_NAME[normalized])
            seen.add(normalized)
    if not selected:
        raise ValueError("至少选择一个数据文件")
    return selected


def _manifest_path() -> Path:
    value = os.environ.get("HM_MANIFEST")
    if not value:
        return DEFAULT_MANIFEST
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def read_manifest() -> dict[str, dict[str, str]]:
    path = _manifest_path()
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = set(MANIFEST_FIELDS).difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"H&M 下载清单缺少必要列：{', '.join(sorted(missing))}")
        rows: dict[str, dict[str, str]] = {}
        for row in reader:
            name = row["file_name"].strip()
            if name in FILES_BY_NAME:
                rows[name] = {key: (row.get(key) or "").strip() for key in MANIFEST_FIELDS}
        return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_manifest(rows: dict[str, dict[str, str]]) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=MANIFEST_FIELDS, lineterminator="\n")
        writer.writeheader()
        for spec in FILES:
            row = rows.get(spec.file_name, {})
            writer.writerow({key: row.get(key, "") for key in MANIFEST_FIELDS})
    temporary.replace(path)


def _refresh_file_metadata(rows: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    refreshed = {name: dict(row) for name, row in rows.items()}
    for spec in FILES:
        row = refreshed.setdefault(spec.file_name, {})
        row.update({
            "file_name": spec.file_name,
            "source_page": SOURCE_PAGE,
            "relative_path": spec.relative_path,
        })
        if spec.local_path.is_file():
            row["bytes"] = str(spec.local_path.stat().st_size)
            row["sha256"] = sha256_file(spec.local_path)
            row["status"] = "downloaded"
        else:
            row.setdefault("status", "待下载")
    _write_manifest(refreshed)
    return refreshed


def verify_files(
    files: Iterable[SourceFile] | None = None,
    *,
    digest: bool = True,
) -> list[dict[str, object]]:
    rows = read_manifest()
    results: list[dict[str, object]] = []
    for spec in files or FILES:
        row = rows.get(spec.file_name, {})
        result: dict[str, object] = {
            "file_name": spec.file_name,
            "relative_path": spec.relative_path,
            "expected_bytes": row.get("bytes") or None,
            "expected_sha256": row.get("sha256") or None,
            "actual_bytes": None,
            "actual_sha256": None,
            "status": "verified",
        }
        if not spec.local_path.is_file():
            result["status"] = "missing"
        elif not row.get("bytes") or not row.get("sha256"):
            result["status"] = "manifest_missing"
            result["actual_bytes"] = spec.local_path.stat().st_size
        else:
            actual_bytes = spec.local_path.stat().st_size
            result["actual_bytes"] = actual_bytes
            if actual_bytes != int(row["bytes"]):
                result["status"] = "size_mismatch"
            elif not digest:
                result["status"] = "size_matches"
            else:
                actual_sha = sha256_file(spec.local_path)
                result["actual_sha256"] = actual_sha
                if actual_sha != row["sha256"]:
                    result["status"] = "sha256_mismatch"
        results.append(result)
    return results


def _extract_download(download_zip: Path, spec: SourceFile) -> None:
    temporary = spec.local_path.with_name(spec.local_path.name + ".part")
    with zipfile.ZipFile(download_zip) as archive:
        matches = [
            member for member in archive.infolist()
            if not member.is_dir() and Path(member.filename).name == spec.file_name
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Kaggle 下载包中应恰有一个 {spec.file_name}，实际找到 {len(matches)} 个"
            )
        member = matches[0]
        with archive.open(member) as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
    if temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"下载的 {spec.file_name} 是空文件")
    temporary.replace(spec.local_path)


def _download_one(spec: SourceFile, force: bool) -> dict[str, str]:
    manifest = read_manifest()
    recorded = manifest.get(spec.file_name, {})
    if not force and spec.local_path.is_file() and recorded.get("sha256") and recorded.get("bytes"):
        if (
            spec.local_path.stat().st_size == int(recorded["bytes"])
            and sha256_file(spec.local_path) == recorded["sha256"]
        ):
            return {"file_name": spec.file_name, "status": "reused"}

    executable = shutil.which("kaggle")
    if not executable:
        raise RuntimeError("找不到 Kaggle CLI；请先执行 `pixi install` 更新项目环境。")

    DOWNLOAD_CACHE.mkdir(parents=True, exist_ok=True)
    spec.local_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        executable, "competitions", "download", COMPETITION,
        "--file", spec.file_name, "--path", str(DOWNLOAD_CACHE), "--force", "--quiet",
    ]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        for key, value in os.environ.items():
            if key.startswith("KAGGLE_") and value:
                detail = detail.replace(value, "[redacted]")
        detail = detail[-1200:] or "Kaggle CLI 返回非零状态"
        lowered = detail.casefold()
        if any(term in lowered for term in ("403", "forbidden", "permission", "rules", "unauthorized")):
            raise RuntimeError(
                "Kaggle 拒绝访问竞赛数据。请在 Kaggle 竞赛页面登录并接受规则，再完成 CLI 身份验证；"
                f"细节：{detail}"
            )
        raise RuntimeError(f"Kaggle 下载 {spec.file_name} 失败：{detail}")

    download_zip = DOWNLOAD_CACHE / f"{spec.file_name}.zip"
    if not download_zip.is_file():
        raise FileNotFoundError(f"Kaggle CLI 未生成预期下载包：{download_zip}")
    _extract_download(download_zip, spec)
    download_zip.unlink(missing_ok=True)
    return {"file_name": spec.file_name, "status": "downloaded"}


def fetch_files(files: Iterable[SourceFile] | None = None, force: bool = False) -> list[dict[str, str]]:
    selected = list(files or FILES)
    results: list[dict[str, str]] = []
    for spec in selected:
        try:
            results.append(_download_one(spec, force=force))
        except Exception as exc:  # noqa: BLE001
            results.append({"file_name": spec.file_name, "status": "failed", "error": str(exc)})
    _refresh_file_metadata(read_manifest())
    return results


def inspect_file(spec: SourceFile) -> dict[str, object]:
    if not spec.local_path.is_file():
        raise FileNotFoundError(f"找不到原始文件：{spec.relative_path}")
    with spec.local_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        fields = next(reader, None)
        if not fields:
            raise ValueError(f"CSV 文件没有有效表头：{spec.relative_path}")
        records = 0
        malformed_rows = 0
        date_min = ""
        date_max = ""
        for row in reader:
            records += 1
            if len(row) != len(fields):
                malformed_rows += 1
            if spec.file_name == "transactions_train.csv" and row:
                date_value = row[0]
                date_min = min(date_min, date_value) if date_min else date_value
                date_max = max(date_max, date_value) if date_max else date_value
    expected = list(spec.expected_columns)
    return {
        "file_name": spec.file_name,
        "relative_path": spec.relative_path,
        "records": records,
        "columns": len(fields),
        "field_names": fields,
        "expected_columns_match": fields == expected,
        "date_min": date_min,
        "date_max": date_max,
        "malformed_rows": malformed_rows,
    }


def _write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def inspect_files(
    files: Iterable[SourceFile] | None = None,
    output_dir: Path = DEFAULT_REPORT_DIR,
) -> list[dict[str, object]]:
    selected = list(files or FILES)
    report = [inspect_file(spec) for spec in selected]
    manifest = read_manifest()
    field_rows: list[dict[str, object]] = []
    for item in report:
        field_rows.extend(
            {
                "file_name": item["file_name"],
                "ordinal": ordinal,
                "field_name": field,
            }
            for ordinal, field in enumerate(item["field_names"], start=1)
        )
        entry = manifest.setdefault(str(item["file_name"]), {})
        entry.update({
            "file_name": item["file_name"],
            "source_page": SOURCE_PAGE,
            "relative_path": item["relative_path"],
            "records": str(item["records"]),
            "columns": str(item["columns"]),
            "status": "inspected" if item["expected_columns_match"] and item["malformed_rows"] == 0 else "schema_review",
        })
    _write_csv(
        output_dir / "附件-数据概况.csv",
        report,
        [
            "file_name", "relative_path", "records", "columns", "date_min", "date_max",
            "malformed_rows", "expected_columns_match",
        ],
    )
    _write_csv(output_dir / "附件-字段清单.csv", field_rows, ["file_name", "ordinal", "field_name"])
    _write_manifest(manifest)
    return report
