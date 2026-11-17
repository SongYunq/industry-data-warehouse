"""Read-only Doris queries for the H&M dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from threading import Lock
from time import sleep

import pandas as pd
import pymysql
import streamlit as st

from warehouse.config import load_settings
from warehouse.db import connect_doris

DATABASE = "hm_retail_dw"
START = date(2018, 9, 20)
CUTOFF = date(2020, 9, 22)
QUERY_LOCK = Lock()

PRODUCT_LEVELS = {
    "product_group_name": "商品组",
    "product_type_name": "商品类型",
    "garment_group_name": "服装组",
    "section_name": "商品版块",
}
APPEARANCES = {
    "colour_group_name": "颜色",
    "graphical_appearance_name": "图案",
}
RANK_KEYS = {"product_code": "款式编号", "article_id": "商品编号"}
PURCHASE_NUMBERS = (1, 2, 3)
REPURCHASE_WINDOWS = (30, 60, 90)


@st.cache_data(ttl=600, max_entries=128, show_spinner=False)
def read(sql: str, params: tuple = ()) -> pd.DataFrame:
    # The course deployment has one BE. Keep uncached dashboard requests from
    # competing for its memory while other users can still read cached results.
    with QUERY_LOCK, connect_doris(load_settings(), database=DATABASE, read_timeout=150) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SET query_timeout = 140, enable_spill = false, exec_mem_limit = 2147483648,
                    parallel_pipeline_task_num = 1
            """)
            try:
                cursor.execute(sql, params or None)
            except pymysql.OperationalError as exc:
                if "MEM_LIMIT_EXCEEDED" not in str(exc):
                    raise
                sleep(1)
                cursor.execute("SET enable_spill = true")
                cursor.execute(sql, params or None)
            return pd.DataFrame.from_records(cursor.fetchall(), columns=[col[0] for col in cursor.description])


def warehouse_ready() -> bool:
    counts = read("""
        SELECT COUNT(*) AS items, MIN(t_dat) AS first_day, MAX(t_dat) AS last_day,
               (SELECT COUNT(*) FROM dim_customer) AS customers,
               (SELECT COUNT(*) FROM dim_article) AS articles,
               (SELECT COUNT(*) FROM dim_date) AS calendar_days
        FROM fact_purchase_item
    """).iloc[0]
    return tuple(counts) == (31_788_324, START, CUTOFF, 1_371_980, 105_542, 734)


def label(field: str, alias: str = "p") -> str:
    return f"COALESCE(NULLIF({alias}.{field}, ''), '未知')"


@dataclass(frozen=True)
class Scope:
    start: date
    end: date
    product_type: str | None = None
    channel: int | None = None

    def purchases(self, *conditions: str) -> tuple[str, tuple]:
        filters = ["p.t_dat BETWEEN %s AND %s"]
        params = [self.start, self.end]
        if self.product_type is not None:
            filters.append(f"{label('product_type_name')} = %s")
            params.append(self.product_type)
        if self.channel is not None:
            filters.append("p.sales_channel_id = %s")
            params.append(self.channel)
        return "FROM v_purchase_detail p WHERE " + " AND ".join((*filters, *conditions)), tuple(params)


def product_types() -> list[str]:
    return read(
        f"SELECT DISTINCT {label('product_type_name')} AS value FROM dim_article p ORDER BY value"
    )["value"].tolist()


def description_terms(scope: Scope) -> pd.DataFrame:
    source, params = scope.purchases()
    catalog_where = f"WHERE {label('product_type_name')} = %s" if scope.product_type is not None else ""
    catalog_params = (scope.product_type,) if scope.product_type is not None else ()
    return read(f"""
        WITH purchases AS (
            SELECT p.article_id, COUNT(*) AS items
            {source}
            GROUP BY p.article_id
        ), totals AS (
            SELECT t.term, COALESCE(SUM(b.items), 0) AS items, COUNT(*) AS variants
            FROM v_article_description_term t
            JOIN dim_article p ON p.article_id = t.article_id
            LEFT JOIN purchases b ON b.article_id = t.article_id
            {catalog_where}
            GROUP BY t.term
        )
        SELECT ROW_NUMBER() OVER (ORDER BY items DESC, term) AS term_rank, term, items, variants
        FROM totals
        ORDER BY term_rank
    """, (*params, *catalog_params))


def totals(scope: Scope) -> pd.Series:
    source, params = scope.purchases()
    return read(f"""
        SELECT COUNT(*) AS items, COUNT(DISTINCT p.customer_id) AS buyers,
               COUNT(DISTINCT p.product_code) AS families, COUNT(DISTINCT p.article_id) AS variants
        {source}
    """, params).iloc[0]


def monthly(scope: Scope) -> pd.DataFrame:
    source, params = scope.purchases()
    return read(f"""
        SELECT DATE_TRUNC(p.t_dat, 'month') AS month,
               COUNT(*) AS items, COUNT(DISTINCT p.customer_id) AS buyers
        {source}
        GROUP BY month ORDER BY month
    """, params)

def group_products(scope: Scope, field: str) -> pd.DataFrame:
    if field not in PRODUCT_LEVELS and field not in APPEARANCES:
        raise ValueError("未知商品分组")
    source, params = scope.purchases()
    return read(f"""
        WITH grouped AS (
            SELECT {label(field)} AS category, COUNT(*) AS items, COUNT(DISTINCT p.customer_id) AS buyers
            {source}
            GROUP BY category
        )
        SELECT category, items, buyers, items / CAST(SUM(items) OVER () AS DOUBLE) AS share
        FROM grouped ORDER BY items DESC, category
    """, params)


def rank_products(scope: Scope, key: str) -> pd.DataFrame:
    if key not in RANK_KEYS:
        raise ValueError("未知商品排名键")
    source, params = scope.purchases()
    ranked = read(f"""
        WITH grouped AS (
            SELECT p.{key} AS code, COUNT(*) AS items
            {source}
            GROUP BY p.{key}
        ), with_shares AS (
            SELECT code, items, items / CAST(SUM(items) OVER () AS DOUBLE) AS share
            FROM grouped
        )
        SELECT code, items, share FROM with_shares
        WHERE code IS NOT NULL ORDER BY items DESC, code LIMIT 20
    """, params)
    if ranked.empty:
        return ranked.assign(buyers=pd.Series(dtype="int64"))
    codes = tuple(int(value) for value in ranked["code"])
    placeholders = ", ".join(["%s"] * len(codes))
    buyers = read(f"""
        SELECT p.{key} AS code, COUNT(DISTINCT p.customer_id) AS buyers
        {source} AND p.{key} IN ({placeholders})
        GROUP BY p.{key}
    """, (*params, *codes))
    return ranked.merge(buyers, on="code", validate="one_to_one")


def family_catalogue(code: int) -> pd.DataFrame:
    return read("""
        SELECT a.article_id, a.prod_name,
               COALESCE(NULLIF(a.product_group_name, ''), '未知') AS product_group_name,
               COALESCE(NULLIF(a.product_type_name, ''), '未知') AS product_type_name,
               COALESCE(NULLIF(a.colour_group_name, ''), '未知') AS colour_group_name,
               COALESCE(NULLIF(a.graphical_appearance_name, ''), '未知') AS graphical_appearance_name,
               COALESCE(p.items, 0) AS training_items
        FROM dim_article a
        LEFT JOIN (
            SELECT f.article_id, COUNT(*) AS items
            FROM fact_purchase_item f JOIN dim_article b ON f.article_id = b.article_id
            WHERE b.product_code = %s
            GROUP BY f.article_id
        ) p ON a.article_id = p.article_id
        WHERE a.product_code = %s
        ORDER BY a.article_id
    """, (code, code))


def family_variants(scope: Scope, code: int) -> pd.DataFrame:
    source, params = scope.purchases("p.product_code = %s")
    return read(f"""
        SELECT p.article_id, COUNT(*) AS items, COUNT(DISTINCT p.customer_id) AS buyers
        {source} GROUP BY p.article_id
    """, (*params, code))


def repurchase(scope: Scope) -> pd.DataFrame:
    positions = " UNION ALL ".join(f"SELECT {number} AS purchase_day_no" for number in PURCHASE_NUMBERS)
    windows = " UNION ALL ".join(f"SELECT {days} AS horizon_days" for days in (0, *REPURCHASE_WINDOWS))
    counts = read(f"""
        WITH positions AS ({positions}), horizons AS ({windows}), sequenced AS (
            SELECT purchase_day_no,
                   ELEMENT_AT(purchase_dates, purchase_day_no) AS purchase_date,
                   ELEMENT_AT(purchase_dates, purchase_day_no + 1) AS next_date
            FROM v_customer_purchase_dates CROSS JOIN positions
        )
        SELECT purchase_day_no, horizon_days, COUNT(*) AS eligible_customers,
               COUNT(CASE WHEN next_date <= %s
                           AND (horizon_days = 0 OR DATEDIFF(next_date, purchase_date) <= horizon_days)
                          THEN 1 END) AS repeat_customers
        FROM sequenced CROSS JOIN horizons
        WHERE purchase_date BETWEEN %s AND %s
          AND DATEDIFF(%s, purchase_date) >= horizon_days
        GROUP BY purchase_day_no, horizon_days
    """, (CUTOFF, scope.start, scope.end, CUTOFF))
    rows = pd.MultiIndex.from_product(
        [PURCHASE_NUMBERS, (0, *REPURCHASE_WINDOWS)], names=["purchase_day_no", "horizon_days"],
    )
    return counts.set_index(list(rows.names)).reindex(rows, fill_value=0).reset_index()


def overlap(scope: Scope) -> pd.DataFrame:
    if scope == Scope(START, CUTOFF):
        return read("SELECT * FROM v_channel_overlap ORDER BY channel_group")
    source, params = scope.purchases()
    return read(f"""
        WITH channel_customers AS (
            SELECT p.customer_id, MIN(p.sales_channel_id) AS first_code, MAX(p.sales_channel_id) AS last_code,
                   COUNT(*) AS items
            {source}
            GROUP BY p.customer_id
        )
        SELECT IF(first_code <> last_code, '1+2', CAST(first_code AS STRING)) AS channel_group,
               COUNT(*) AS customer_count, SUM(items) AS item_count
        FROM channel_customers GROUP BY channel_group ORDER BY channel_group
    """, params)


def channel_composition(scope: Scope, field: str) -> pd.DataFrame:
    if field not in PRODUCT_LEVELS and field not in APPEARANCES and field not in RANK_KEYS:
        raise ValueError("未知渠道构成维度")
    expression = f"COALESCE(CAST(p.{field} AS STRING), '未知')" if field in RANK_KEYS else label(field)
    source, params = scope.purchases()
    ranked = read(f"""
        WITH grouped AS (
            SELECT p.sales_channel_id AS channel, {expression} AS category,
                   COUNT(*) AS items
            {source}
            GROUP BY channel, category
        ), ranks AS (
            SELECT channel, category, items,
                   items / CAST(SUM(items) OVER (PARTITION BY channel) AS DOUBLE) AS share,
                   ROW_NUMBER() OVER (PARTITION BY channel ORDER BY items DESC, category) AS item_rank
            FROM grouped
        )
        SELECT channel, category, items, share FROM ranks
        WHERE item_rank <= 20 ORDER BY channel, item_rank
    """, params)
    if ranked.empty:
        return ranked.assign(buyers=pd.Series(dtype="int64"))
    filters = []
    values = list(params)
    for channel, categories in ranked.groupby("channel")["category"]:
        placeholders = ", ".join(["%s"] * len(categories))
        filters.append(f"(p.sales_channel_id = %s AND {expression} IN ({placeholders}))")
        values.extend((int(channel), *categories))
    top_conditions = " OR ".join(filters)
    buyers = read(f"""
        SELECT p.sales_channel_id AS channel, {expression} AS category,
               COUNT(DISTINCT p.customer_id) AS buyers
        {source} AND ({top_conditions})
        GROUP BY channel, category
    """, tuple(values))
    return ranked.merge(buyers, on=["channel", "category"], validate="one_to_one")


def channel_months(scope: Scope) -> pd.DataFrame:
    if scope == Scope(START, CUTOFF):
        return read("""
            SELECT purchase_month AS month, CAST(sales_channel_id AS STRING) AS channel,
                   item_count AS items, customer_count AS buyers
            FROM v_monthly_channel ORDER BY month, channel
        """)
    source, params = scope.purchases()
    return read(f"""
        SELECT DATE_TRUNC(p.t_dat, 'month') AS month,
               CAST(p.sales_channel_id AS STRING) AS channel,
               COUNT(*) AS items, COUNT(DISTINCT p.customer_id) AS buyers
        {source} GROUP BY month, channel ORDER BY month, channel
    """, params)
