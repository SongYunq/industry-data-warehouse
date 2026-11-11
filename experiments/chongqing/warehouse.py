"""Publish prepared Chongqing experiment tables to the local cq_pilot database."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import httpx
import pymysql
from dotenv import dotenv_values
from pymysql.cursors import DictCursor

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PREPARED_DIR = ROOT / "data" / "chongqing" / "prepared"
DATABASE = "cq_pilot"
SCHEMA_SUFFIX = ".schema.json"
STAGE_PREFIX = "cqstage_"
CHUNK_BYTES = 24 * 1024 * 1024
IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{0,63}\Z")
SQL_TYPE = re.compile(
    r"(?:TINYINT|SMALLINT|INT|INTEGER|BIGINT|LARGEINT|FLOAT|DOUBLE|"
    r"BOOLEAN|BOOL|STRING|TEXT|DATE(?:V2)?|DATETIME(?:V2)?(?:\([0-6]\))?|"
    r"(?:VAR)?CHAR\([1-9]\d*\)|DECIMAL(?:V[23])?\([1-9]\d*,\s*\d+\))\Z"
)
TEXT_TYPES = ("VARCHAR", "CHAR", "STRING", "TEXT")


@dataclass(frozen=True)
class Table:
    name: str
    path: Path
    columns: dict[str, str]


@dataclass(frozen=True)
class LocalDoris:
    query_port: int
    be_port: int
    user: str
    password: str


def _local_doris() -> LocalDoris:
    settings = dotenv_values(ROOT / ".env")

    def value(key: str, default: str) -> str:
        return os.environ.get(key, settings.get(key) or default)

    return LocalDoris(
        query_port=int(value("DORIS_QUERY_PORT", "9030")),
        be_port=int(value("DORIS_BE_HTTP_PORT", "8040")),
        user=value("DORIS_USER", "root"),
        password=value("DORIS_PASSWORD", ""),
    )


def _connect(settings: LocalDoris, *, database: str | None = DATABASE):
    return pymysql.connect(
        host="127.0.0.1",
        port=settings.query_port,
        user=settings.user,
        password=settings.password,
        database=database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        connect_timeout=5,
        read_timeout=60,
        write_timeout=60,
        autocommit=True,
    )


def connect():
    """Open a read or query connection to the local cq_pilot database."""
    return _connect(_local_doris())


def _name(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid Doris identifier: {value!r}")
    return f"`{value}`"


def discover(prepared: Path) -> list[Table]:
    tables = []
    for schema in sorted(prepared.glob(f"*{SCHEMA_SUFFIX}")):
        table_name = schema.name.removesuffix(SCHEMA_SUFFIX)
        _name(table_name)
        if table_name.startswith(STAGE_PREFIX) or len(table_name) > 40:
            raise ValueError(f"Reserved or too long pilot table name: {table_name}")
        path = prepared / f"{table_name}.csv"
        if not path.is_file():
            raise ValueError(f"Missing CSV for {schema.name}")
        columns = json.loads(schema.read_text(encoding="utf-8"))
        if not isinstance(columns, dict) or not columns:
            raise ValueError(f"Expected a nonempty column-to-type object in {schema.name}")
        normalized = {}
        for column, raw_type in columns.items():
            _name(column)
            sql_type = raw_type.strip().upper() if isinstance(raw_type, str) else ""
            if not SQL_TYPE.fullmatch(sql_type):
                raise ValueError(f"Unsupported type in {schema.name}: {column}={raw_type!r}")
            normalized[column] = sql_type
        if len({column.lower() for column in normalized}) != len(normalized):
            raise ValueError(f"Case-insensitive duplicate column in {schema.name}")
        tables.append(Table(table_name, path, normalized))
    if not tables:
        raise ValueError(f"No prepared table schemas found in {prepared}")
    return tables


def _rows(table: Table) -> Iterator[list[str]]:
    with table.path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, strict=True)
        if next(reader, None) != list(table.columns):
            raise ValueError(f"{table.path.name}: CSV header does not match schema order")
        for record, row in enumerate(reader, start=2):
            if len(row) != len(table.columns):
                raise ValueError(
                    f"{table.path.name}: record {record} has {len(row)} columns, "
                    f"expected {len(table.columns)}"
                )
            yield row


def _payloads(table: Table) -> Iterator[tuple[bytearray, int]]:
    columns = list(table.columns)
    is_text = [value.startswith(TEXT_TYPES) for value in table.columns.values()]
    payload = bytearray()
    count = 0
    for row in _rows(table):
        record = {
            column: value if value or text else None
            for column, value, text in zip(columns, row, is_text, strict=True)
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(line) > CHUNK_BYTES:
            raise ValueError(f"{table.path.name}: a single record exceeds {CHUNK_BYTES} bytes")
        if payload and len(payload) + len(line) > CHUNK_BYTES:
            yield payload, count
            payload, count = bytearray(), 0
        payload.extend(line)
        count += 1
    if payload:
        yield payload, count


def _create_sql(table: Table, destination: str) -> str:
    columns = ", ".join(f"{_name(name)} {sql_type} NULL" for name, sql_type in table.columns.items())
    buckets = 4 if table.path.stat().st_size >= 32 * 1024 * 1024 else 1
    return (
        f"CREATE TABLE {_name(destination)} ({columns}) "
        f"DISTRIBUTED BY RANDOM BUCKETS {buckets} "
        'PROPERTIES ("replication_num" = "1", "enable_duplicate_without_keys_by_default" = "true")'
    )


def _stream_table(
    client: httpx.Client, settings: LocalDoris, table: Table, destination: str, run_id: str
) -> int:
    total = 0
    for index, (payload, count) in enumerate(_payloads(table), start=1):
        response = client.put(
            f"http://127.0.0.1:{settings.be_port}/api/{DATABASE}/{destination}/_stream_load",
            content=bytes(payload),
            auth=(settings.user, settings.password),
            headers={
                "Expect": "100-continue",
                "label": f"cqp_{run_id}_{table.name}_{index}",
                "format": "json",
                "read_json_by_line": "true",
                "strict_mode": "true",
                "max_filter_ratio": "0",
                "columns": ", ".join(_name(column) for column in table.columns),
                "timeout": "150",
            },
        )
        response.raise_for_status()
        result = response.json()
        if (
            result.get("Status") not in {"Success", "Publish Timeout"}
            or result.get("NumberTotalRows") != count
            or result.get("NumberLoadedRows") != count
            or result.get("NumberFilteredRows") != 0
            or result.get("NumberUnselectedRows") != 0
        ):
            raise RuntimeError(
                f"{table.name} chunk {index}: expected {count} rows, Doris returned "
                f"{result.get('Status')!r}, loaded={result.get('NumberLoadedRows')}, "
                f"filtered={result.get('NumberFilteredRows')}, "
                f"message={result.get('Message')!r}, error_url={result.get('ErrorURL')!r}"
            )
        total += count
        print(f"{table.name}: loaded {total:,} rows (chunk {index})", flush=True)
    return total


def _count(cursor, name: str) -> int:
    cursor.execute(f"SELECT COUNT(*) AS n FROM {_name(name)}")
    return int(cursor.fetchone()["n"])


def _check_table(cursor, table: Table, destination: str, expected_rows: int, *, wait: bool) -> int:
    cursor.execute(f"DESC {_name(destination)}")
    if [row["Field"] for row in cursor.fetchall()] != list(table.columns):
        raise RuntimeError(f"{destination}: Doris column names or order differ from {table.path.name}")
    deadline = time.monotonic() + (45 if wait else 0)
    while True:
        actual = _count(cursor, destination)
        if actual == expected_rows:
            return actual
        if actual > expected_rows or time.monotonic() >= deadline:
            raise RuntimeError(
                f"{destination}: expected {expected_rows:,} visible rows, found {actual:,}"
            )
        time.sleep(1)


@contextmanager
def _publish_lock() -> Iterator[None]:
    # The OS releases the lock when the process exits. Keep the file as its
    # stable identity so another process cannot obtain a lock on a new inode.
    lock_path = DEFAULT_PREPARED_DIR.parent / ".doris-publish.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            lock_region = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            lock_region = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lock_region()
        except OSError as exc:
            raise RuntimeError("Another local cq_pilot publication holds the lock") from exc
        yield


def _replace(cursor, original: str, candidate: str) -> None:
    cursor.execute(
        f"ALTER TABLE {_name(original)} REPLACE WITH TABLE {_name(candidate)} "
        "PROPERTIES ('swap' = 'true')"
    )


def _drop_candidates(cursor, names: list[str]) -> None:
    for name in names:
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {_name(name)}")
        except pymysql.MySQLError as exc:
            print(f"Could not clean candidate {name}: {exc}", file=sys.stderr, flush=True)


def publish(prepared: Path) -> dict[str, int]:
    tables = discover(prepared)
    settings = _local_doris()
    run_id = uuid.uuid4().hex[:12]
    staged: dict[str, str] = {}
    counts: dict[str, int] = {}
    with _publish_lock():
        with _connect(settings, database=None) as connection, connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS {_name(DATABASE)}")
        with (
            _connect(settings) as connection,
            connection.cursor() as cursor,
            httpx.Client(timeout=httpx.Timeout(180, connect=10), trust_env=False) as client,
        ):
            cursor.execute("SHOW TABLES")
            previous = {next(iter(row.values())) for row in cursor.fetchall()}
            try:
                for table in tables:
                    candidate = f"{STAGE_PREFIX}{run_id}_{table.name}"
                    cursor.execute(_create_sql(table, candidate))
                    staged[table.name] = candidate
                    count = _stream_table(client, settings, table, candidate, run_id)
                    counts[table.name] = _check_table(cursor, table, candidate, count, wait=True)
                    print(f"{table.name}: staged and verified {count:,} rows", flush=True)
            except BaseException:
                _drop_candidates(cursor, list(staged.values()))
                raise

            promoted = []
            try:
                for name, candidate in staged.items():
                    if name in previous:
                        _replace(cursor, name, candidate)
                    else:
                        cursor.execute(f"ALTER TABLE {_name(candidate)} RENAME {_name(name)}")
                    promoted.append(name)
                for table in tables:
                    _check_table(cursor, table, table.name, counts[table.name], wait=False)
            except BaseException:
                for name in reversed(promoted):
                    candidate = staged[name]
                    try:
                        if name in previous:
                            _replace(cursor, name, candidate)
                        else:
                            cursor.execute(f"ALTER TABLE {_name(name)} RENAME {_name(candidate)}")
                    except pymysql.MySQLError as exc:
                        print(f"Could not roll back {name}: {exc}", file=sys.stderr, flush=True)
                print(
                    f"Publication interrupted while switching tables. Inspect remaining {STAGE_PREFIX}"
                    f"{run_id}_ tables in {DATABASE} before cleanup.",
                    file=sys.stderr, flush=True,
                )
                raise
            _drop_candidates(cursor, [staged[name] for name in previous if name in staged])
    return counts


def verify(prepared: Path) -> dict[str, int]:
    tables = discover(prepared)
    counts = {}
    with connect() as connection, connection.cursor() as cursor:
        for table in tables:
            expected = sum(1 for _ in _rows(table))
            counts[table.name] = _check_table(cursor, table, table.name, expected, wait=False)
            print(f"{table.name}: verified {expected:,} rows", flush=True)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["publish", "verify"])
    parser.add_argument("--prepared-dir", type=Path, default=DEFAULT_PREPARED_DIR)
    args = parser.parse_args()
    try:
        counts = (publish if args.action == "publish" else verify)(args.prepared_dir)
    except (OSError, ValueError, csv.Error, RuntimeError, pymysql.MySQLError, httpx.HTTPError) as exc:
        print(f"{args.action} failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"database": DATABASE, "action": args.action, "table_rows": counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
