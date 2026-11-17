"""Refresh H&M DWS and ADS tables, then point the analysis views at them."""

from __future__ import annotations

import re
from time import perf_counter

from hm_recommendations.labels import translations
from warehouse.config import load_settings
from warehouse.db import connect_doris

DATABASE = "hm_retail_dw"
VIEW_HEADER = re.compile(r"CREATE VIEW IF NOT EXISTS (\w+) AS", re.IGNORECASE)
OBJECT_NAME = re.compile(
    r"^(?:CREATE TABLE IF NOT EXISTS|INSERT OVERWRITE TABLE|CREATE VIEW IF NOT EXISTS|ALTER VIEW) (\w+)",
    re.IGNORECASE,
)
DETAIL_VIEW = "v_purchase_detail"
FACT_ROWS = 31_788_324
BUYERS = 1_362_281
BOTH_CHANNELS = 483_255
FIRST_30 = (376_347, 1_338_029)
FIRST_60 = (521_714, 1_316_521)
JULY_OFFLINE = (535_082, 145_757)
SWITCH_PAIRS = 7_545_101
SWITCH_CHANGES = 1_938_345
FIRST_UNTIL_END = (914_308, BUYERS)


def _statements(script: str) -> list[str]:
    stripped = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("--")
    )
    return [statement.strip() for statement in stripped.split(";") if statement.strip()]


def _name(statement: str) -> str:
    header = OBJECT_NAME.match(statement)
    if header is None:
        raise ValueError("分析脚本含无法识别的语句")
    return header[1]


def _execute(cursor, statement: str) -> None:
    name = _name(statement)
    print(f"start {name}", flush=True)
    started = perf_counter()
    cursor.execute(statement)
    print(f"done {name} {perf_counter() - started:.1f}s", flush=True)


def _apply_view(cursor, statement: str, existing: set[str]) -> None:
    header = VIEW_HEADER.match(statement)
    if header is None:
        raise ValueError("分析脚本含无法识别的视图定义")
    name = header[1]
    if name in existing:
        statement = f"ALTER VIEW {name} AS" + statement[header.end():]
    _execute(cursor, statement)


def _integer(cursor, sql: str) -> tuple[int, ...]:
    cursor.execute(sql)
    row = cursor.fetchone()
    return tuple(int(value) for value in row.values())


def _expect(cursor, sql: str, expected: tuple[int, ...]) -> None:
    actual = _integer(cursor, sql)
    if actual != expected:
        raise RuntimeError(f"汇总核对失败，得到 {actual}，预期 {expected}: {sql.strip()}")


def _check_tables(cursor) -> None:
    _expect(cursor, "SELECT SUM(item_count) FROM dws_customer_purchase_day", (FACT_ROWS,))
    day_count = _integer(cursor, "SELECT COUNT(*) FROM dws_customer_purchase_day")
    sequence_count = _integer(cursor, "SELECT COUNT(*) FROM dws_customer_purchase_sequence")
    if day_count != sequence_count:
        raise RuntimeError(f"购买日序列行数 {sequence_count[0]} 与购买日 {day_count[0]} 不一致")
    _expect(cursor, "SELECT COUNT(*), SUM(item_count) FROM dws_customer_purchase_dates", (BUYERS, FACT_ROWS))
    _expect(cursor, "SELECT COUNT(*) FROM dws_customer_channel_dates", (BUYERS,))
    term_count = _integer(cursor, "SELECT COUNT(*) FROM dws_article_description_term")
    if term_count[0] <= 0:
        raise RuntimeError("商品描述词条表为空")
    _expect(
        cursor,
        """
        SELECT repeat_customers, eligible_customers FROM ads_repurchase_stage
        WHERE purchase_day_no = 1 AND horizon_days = 30
        """,
        FIRST_30,
    )
    _expect(
        cursor,
        """
        SELECT repeat_customers, eligible_customers FROM ads_repurchase_stage
        WHERE purchase_day_no = 1 AND horizon_days = 60
        """,
        FIRST_60,
    )
    _expect(cursor, "SELECT COUNT(*) FROM ads_repurchase_stage", (9,))
    _expect(
        cursor,
        """
        SELECT SUM(repeat_customers), SUM(eligible_customers)
        FROM ads_first_month_repurchase WHERE horizon_days = 30
        """,
        FIRST_30,
    )
    _expect(
        cursor,
        "SELECT SUM(customer_count), SUM(item_count) FROM ads_customer_frequency_recency",
        (BUYERS, FACT_ROWS),
    )
    _expect(
        cursor,
        "SELECT SUM(customer_count), SUM(item_count) FROM ads_channel_overlap",
        (BUYERS, FACT_ROWS),
    )
    _expect(
        cursor,
        "SELECT customer_count FROM ads_channel_overlap WHERE channel_group = '1+2'",
        (BOTH_CHANNELS,),
    )
    _expect(cursor, "SELECT SUM(pair_count) FROM ads_channel_switch", (SWITCH_PAIRS,))
    _expect(
        cursor,
        "SELECT SUM(pair_count) FROM ads_channel_switch WHERE from_channel <> to_channel",
        (SWITCH_CHANGES,),
    )
    _expect(cursor, "SELECT COUNT(*) FROM ads_channel_day_profile WHERE day_count > 0", (3,))
    journey_rows = _integer(cursor, "SELECT COUNT(*) FROM ads_category_journey")
    if journey_rows[0] <= 0:
        raise RuntimeError("品类旅程表为空")
    _expect(cursor, "SELECT SUM(item_count) FROM ads_monthly_overview", (FACT_ROWS,))
    _expect(cursor, "SELECT SUM(item_count) FROM ads_monthly_product_group", (FACT_ROWS,))
    _expect(cursor, "SELECT SUM(item_count) FROM ads_monthly_colour_group", (FACT_ROWS,))
    _expect(cursor, "SELECT SUM(item_count) FROM ads_monthly_channel", (FACT_ROWS,))
    _expect(
        cursor,
        """
        SELECT item_count, customer_count FROM ads_monthly_channel
        WHERE purchase_month = DATE '2020-07-01' AND sales_channel_id = 1
        """,
        JULY_OFFLINE,
    )


def _check_repurchase_view(cursor) -> None:
    _expect(
        cursor,
        """
        WITH positions AS (
            SELECT 1 AS purchase_day_no UNION ALL SELECT 2 UNION ALL SELECT 3
        ), horizons AS (
            SELECT 0 AS horizon_days UNION ALL SELECT 30 UNION ALL SELECT 60 UNION ALL SELECT 90
        ), sequenced AS (
            SELECT purchase_day_no,
                   ELEMENT_AT(purchase_dates, purchase_day_no) AS purchase_date,
                   ELEMENT_AT(purchase_dates, purchase_day_no + 1) AS next_date
            FROM v_customer_purchase_dates CROSS JOIN positions
        )
        SELECT repeat_customers, eligible_customers
        FROM (
            SELECT purchase_day_no, horizon_days, COUNT(*) AS eligible_customers,
                   COUNT(CASE WHEN next_date <= DATE '2020-09-22'
                               AND (horizon_days = 0 OR DATEDIFF(next_date, purchase_date) <= horizon_days)
                              THEN 1 END) AS repeat_customers
            FROM sequenced CROSS JOIN horizons
            WHERE purchase_date BETWEEN DATE '2018-09-20' AND DATE '2020-09-22'
              AND DATEDIFF(DATE '2020-09-22', purchase_date) >= horizon_days
            GROUP BY purchase_day_no, horizon_days
        ) counted
        WHERE purchase_day_no = 1 AND horizon_days = 0
        """,
        FIRST_UNTIL_END,
    )


def _prepare_session(cursor) -> None:
    # One BE has 7.75 GiB. Spill and a single pipeline keep the full rebuild
    # from competing with itself for memory.
    for statement in (
        "SET query_timeout = 3600",
        "SET enable_spill = true",
        "SET exec_mem_limit = 4294967296",
        "SET parallel_pipeline_task_num = 1",
    ):
        cursor.execute(statement)


def main() -> None:
    settings = load_settings()
    path = settings.project_root / "sql" / "hm_recommendations" / "002_analytics.sql"
    statements = _statements(path.read_text(encoding="utf-8"))
    immediate: list[str] = []
    published_views: list[str] = []
    for statement in statements:
        header = VIEW_HEADER.match(statement)
        if header is not None and header[1] != DETAIL_VIEW:
            published_views.append(statement)
        else:
            immediate.append(statement)
    with connect_doris(settings, DATABASE, read_timeout=3600) as connection, connection.cursor() as cursor:
        _prepare_session(cursor)
        cursor.execute("SHOW FULL TABLES WHERE Table_type = 'VIEW'")
        existing = {next(iter(row.values())) for row in cursor.fetchall()}
        for statement in immediate:
            if VIEW_HEADER.match(statement):
                _apply_view(cursor, statement, existing)
                existing.add(_name(statement))
            else:
                _execute(cursor, statement)
        _check_tables(cursor)
        for statement in published_views:
            _apply_view(cursor, statement, existing)
            existing.add(_name(statement))
        _publish_labels(cursor, existing)
        _check_repurchase_view(cursor)
    print("analytics-install ok", flush=True)


def _publish_labels(cursor, existing: set[str]) -> None:
    labels = translations()
    rows = [(field, source, zh) for (field, source), zh in labels.items()]
    fields, sources, chinese = (
        "[" + ", ".join(cursor.mogrify("%s", (value,)) for value in column) + "]"
        for column in zip(*rows, strict=True)
    )
    statement = f"""
        CREATE VIEW IF NOT EXISTS v_display_labels AS
        SELECT field_name, source_label, zh_label
        FROM (SELECT 1 AS seed) AS input_row
        LATERAL VIEW EXPLODE({fields}, {sources}, {chinese})
            labels AS field_name, source_label, zh_label
    """.strip()
    _apply_view(cursor, statement, existing)


if __name__ == "__main__":
    main()
