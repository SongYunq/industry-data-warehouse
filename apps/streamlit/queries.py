"""Read-only Doris queries for the H&M dashboard."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from threading import Lock
from time import sleep
from typing import Any

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
    "index_group_name": "经营大组（Index Group）",
    "index_name": "经营分类（Index）",
    "department_name": "部门",
    "section_name": "版块",
}
APPEARANCES = {
    "colour_group_name": "颜色组",
    "perceived_colour_master_name": "主色系",
    "perceived_colour_value_name": "明暗",
    "graphical_appearance_name": "图案",
}
RANK_KEYS = {"product_code": "款式族候选", "article_id": "具体变体"}
CUSTOMER_GROUPS = {
    "age_band": "年龄段",
    "club_member_status": "会员状态",
    "fashion_news_frequency": "资讯频率",
    "fn_source_flag": "FN 源标记",
    "active_source_flag": "Active 源标记",
}
AGE_BAND = """
    CASE WHEN c.age_years BETWEEN 16 AND 17 THEN '16–17'
         WHEN c.age_years BETWEEN 18 AND 24 THEN '18–24'
         WHEN c.age_years BETWEEN 25 AND 34 THEN '25–34'
         WHEN c.age_years BETWEEN 35 AND 44 THEN '35–44'
         WHEN c.age_years BETWEEN 45 AND 54 THEN '45–54'
         WHEN c.age_years BETWEEN 55 AND 64 THEN '55–64'
         WHEN c.age_years >= 65 THEN '65+'
         ELSE '未知' END
"""


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


def customer_expression(field: str) -> str:
    if field == "age_band":
        return AGE_BAND
    if field not in CUSTOMER_GROUPS:
        raise ValueError("未知客户分组")
    return label(field, "c")


@dataclass(frozen=True)
class Scope:
    start: date
    end: date
    product: tuple[tuple[str, str], ...] = ()
    channel: int | None = None
    customer: tuple[tuple[str, str], ...] = ()

    def customer_conditions(self) -> tuple[str, tuple]:
        conditions = "".join(f" AND {customer_expression(field)} = %s" for field, _ in self.customer)
        values = tuple(value for _, value in self.customer)
        return conditions, values

    def predicates(self, date_field: str) -> tuple[str, tuple]:
        conditions, values = self.customer_conditions()
        return f"{date_field} BETWEEN %s AND %s{conditions}", (self.start, self.end, *values)

    def purchases(self, *conditions: str, customer_dimension: bool = False) -> tuple[str, tuple]:
        anchor, params = self.predicates("p.t_dat")
        extra = []
        values = list(params)
        for field, value in self.product:
            if field in RANK_KEYS:
                extra.append(f"p.{field} = %s")
                values.append(int(value))
            else:
                if field not in PRODUCT_LEVELS and field not in APPEARANCES:
                    raise ValueError("未知商品筛选字段")
                extra.append(f"{label(field)} = %s")
                values.append(value)
        if self.channel is not None:
            extra.append("p.sales_channel_id = %s")
            values.append(self.channel)
        table = "FROM v_purchase_detail p"
        if self.customer or customer_dimension:
            table += " JOIN dim_customer c ON p.customer_id = c.customer_id"
        return table + " WHERE " + " AND ".join((anchor, *extra, *conditions)), tuple(values)


def options(field: str) -> list[str]:
    if field in CUSTOMER_GROUPS:
        source = "dim_customer c"
        expression = customer_expression(field)
    elif field in PRODUCT_LEVELS or field in APPEARANCES:
        source = "dim_article p"
        expression = label(field)
    else:
        raise ValueError("未知筛选字段")
    return read(f"SELECT DISTINCT {expression} AS value FROM {source} ORDER BY value")["value"].tolist()


def totals(scope: Scope) -> pd.Series:
    source, params = scope.purchases()
    return read(f"""
        SELECT COUNT(*) AS items, COUNT(DISTINCT p.customer_id) AS buyers,
               COUNT(DISTINCT p.product_code) AS families, COUNT(DISTINCT p.article_id) AS variants
        {source}
    """, params).iloc[0]


def monthly(scope: Scope, field: str | None = None) -> pd.DataFrame:
    source, params = scope.purchases()
    if field is None:
        group_column = ""
        group_clause = ""
    else:
        if field not in PRODUCT_LEVELS and field not in APPEARANCES:
            raise ValueError("未知月度分组")
        group_column = f"{label(field)} AS category,"
        group_clause = ", category"
    return read(f"""
        SELECT DATE_TRUNC(p.t_dat, 'month') AS month, {group_column}
               COUNT(*) AS items, COUNT(DISTINCT p.customer_id) AS buyers
        {source}
        GROUP BY month{group_clause} ORDER BY month{group_clause}
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


def monthly_concentration(scope: Scope, key: str) -> pd.DataFrame:
    if key not in RANK_KEYS:
        raise ValueError("未知商品排名键")
    source, params = scope.purchases()
    return read(f"""
        WITH grouped AS (
            SELECT DATE_TRUNC(p.t_dat, 'month') AS month, p.{key} AS code, COUNT(*) AS items
            {source}
            GROUP BY month, p.{key}
        ), ranked AS (
            SELECT month, code, items,
                   ROW_NUMBER() OVER (PARTITION BY month ORDER BY code IS NULL, items DESC, code) AS item_rank
            FROM grouped
        )
        SELECT month, SUM(items) AS items,
               SUM(IF(code IS NOT NULL AND item_rank <= 10, items, 0)) / CAST(SUM(items) AS DOUBLE) AS top10,
               SUM(IF(code IS NOT NULL AND item_rank <= 20, items, 0)) / CAST(SUM(items) AS DOUBLE) AS top20
        FROM ranked GROUP BY month ORDER BY month
    """, params)


def appearance_matrix(scope: Scope, appearance: str, level: str) -> pd.DataFrame:
    if appearance not in APPEARANCES or level not in PRODUCT_LEVELS:
        raise ValueError("未知商品属性或分类")
    source, params = scope.purchases()
    return read(f"""
        SELECT {label(appearance)} AS appearance, {label(level)} AS category,
               COUNT(*) AS items, COUNT(DISTINCT p.customer_id) AS buyers
        {source}
        GROUP BY appearance, category ORDER BY items DESC, category, appearance
    """, params)


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


def family_cross_day(scope: Scope, code: int) -> pd.DataFrame:
    source, params = scope.purchases("p.product_code = %s")
    return read(f"""
        WITH family_items AS (
            SELECT p.customer_id, p.article_id, p.t_dat {source}
        ), variant_buyers AS (
            SELECT customer_id, article_id, COUNT(DISTINCT t_dat) AS days
            FROM family_items GROUP BY customer_id, article_id
        ), family_buyers AS (
            SELECT customer_id, COUNT(DISTINCT t_dat) AS days
            FROM family_items GROUP BY customer_id
        )
        SELECT CAST(article_id AS STRING) AS code, COUNT(*) AS buyers, SUM(IF(days >= 2, 1, 0)) AS repeat_customers
        FROM variant_buyers GROUP BY article_id
        UNION ALL
        SELECT '款式族' AS code, COUNT(*) AS buyers, SUM(IF(days >= 2, 1, 0)) AS repeat_customers
        FROM family_buyers
        ORDER BY code
    """, (*params, code))


def family_repurchase_windows(scope: Scope, code: int) -> pd.DataFrame:
    anchor, params = scope.predicates("p.first_day")
    join_customer = "JOIN dim_customer c ON p.customer_id = c.customer_id" if scope.customer else ""
    return read(f"""
        WITH family_purchase_dates AS (
            SELECT f.customer_id, ARRAY_SORT(COLLECT_SET(f.t_dat)) AS dates
            FROM v_purchase_detail f
            WHERE f.product_code = %s
            GROUP BY f.customer_id
        ), first_family_day AS (
            SELECT customer_id, ELEMENT_AT(dates, 1) AS first_day, ELEMENT_AT(dates, 2) AS next_family_day
            FROM family_purchase_dates
        ), next_purchase AS (
            SELECT p.customer_id, p.first_day, p.next_family_day, MIN(f.t_dat) AS next_day
            FROM first_family_day p {join_customer}
            LEFT JOIN fact_purchase_item f ON f.customer_id = p.customer_id
                AND f.t_dat > p.first_day AND DATEDIFF(f.t_dat, p.first_day) <= 90
            WHERE {anchor}
            GROUP BY p.customer_id, p.first_day, p.next_family_day
        ), horizons AS (
            SELECT 30 AS horizon_days UNION ALL SELECT 60 UNION ALL SELECT 90
        )
        SELECT horizon_days, COUNT(*) AS eligible_customers,
               SUM(IF(DATEDIFF(next_family_day, first_day) <= horizon_days, 1, 0)) AS repeat_customers,
               SUM(IF(DATEDIFF(next_day, first_day) <= horizon_days, 1, 0)) AS any_repeat_customers
        FROM next_purchase CROSS JOIN horizons
        WHERE DATEDIFF(%s, first_day) >= horizon_days
        GROUP BY horizon_days ORDER BY horizon_days
    """, (code, *params, CUTOFF))


def stage(scope: Scope, group: str | None = None, by_month: bool = False) -> pd.DataFrame:
    anchor, params = scope.predicates("p.anchor_date")
    if group:
        group_expr = customer_expression(group)
    elif by_month:
        group_expr = "CAST(DATE_TRUNC(p.anchor_date, 'month') AS DATE)"
    else:
        group_expr = "CAST('全部' AS STRING)"
    join_customer = "JOIN dim_customer c ON p.customer_id = c.customer_id" if group or scope.customer else ""
    positions = "SELECT 1 AS purchase_day_no"
    if not by_month:
        positions += " UNION ALL SELECT 2 UNION ALL SELECT 3"
    return read(f"""
        WITH positions AS ({positions}), sequenced AS (
            SELECT customer_id, purchase_day_no, ELEMENT_AT(purchase_dates, purchase_day_no) AS anchor_date,
                   ELEMENT_AT(purchase_dates, purchase_day_no + 1) AS next_date
            FROM v_customer_purchase_dates CROSS JOIN positions
        ), stage_days AS (
            SELECT p.purchase_day_no, p.anchor_date, DATEDIFF(p.next_date, p.anchor_date) AS gap_days,
                   {group_expr} AS cohort
            FROM sequenced p {join_customer}
            WHERE {anchor}
        ), horizons AS (
            SELECT 30 AS horizon_days UNION ALL SELECT 60 UNION ALL SELECT 90
        )
        SELECT purchase_day_no, horizon_days, cohort,
               COUNT(*) AS eligible_customers,
               SUM(IF(gap_days <= horizon_days, 1, 0)) AS repeat_customers,
               SUM(IF(gap_days <= horizon_days, 0, 1)) AS no_repeat_customers,
               SUM(IF(gap_days <= horizon_days, 1, 0)) / CAST(COUNT(*) AS DOUBLE) AS repeat_rate,
               PERCENTILE_APPROX(IF(gap_days <= horizon_days, gap_days, NULL), 0.25) AS gap_p25,
               PERCENTILE_APPROX(IF(gap_days <= horizon_days, gap_days, NULL), 0.50) AS gap_median,
               PERCENTILE_APPROX(IF(gap_days <= horizon_days, gap_days, NULL), 0.75) AS gap_p75
        FROM stage_days CROSS JOIN horizons
        WHERE DATEDIFF(%s, anchor_date) >= horizon_days
        GROUP BY purchase_day_no, horizon_days, cohort
        ORDER BY cohort, purchase_day_no, horizon_days
    """, (*params, CUTOFF))


def recency(scope: Scope) -> pd.DataFrame:
    anchor, params = scope.predicates("p.t_dat")
    if not scope.customer and scope.start == START and scope.end == CUTOFF:
        return read("SELECT * FROM v_customer_frequency_recency ORDER BY recency_band, frequency_band")
    join_customer = "JOIN dim_customer c ON p.customer_id = c.customer_id" if scope.customer else ""
    return read(f"""
        WITH customers AS (
            SELECT p.customer_id, COUNT(DISTINCT p.t_dat) AS frequency, MAX(p.t_dat) AS recent_date, COUNT(*) AS items
            FROM fact_purchase_item p {join_customer}
            WHERE {anchor}
            GROUP BY p.customer_id
        ), bands AS (
            SELECT CASE WHEN DATEDIFF(%s, recent_date) <= 30 THEN '0–30'
                        WHEN DATEDIFF(%s, recent_date) <= 60 THEN '31–60'
                        WHEN DATEDIFF(%s, recent_date) <= 90 THEN '61–90'
                        ELSE '91+' END AS recency_band,
                   IF(frequency >= 4, '4+', CAST(frequency AS STRING)) AS frequency_band,
                   items FROM customers
        )
        SELECT recency_band, frequency_band, COUNT(*) AS customer_count, SUM(items) AS item_count
        FROM bands GROUP BY recency_band, frequency_band ORDER BY recency_band, frequency_band
    """, (*params, CUTOFF, CUTOFF, CUTOFF))


def customer_breadth(scope: Scope, dimension: str, group: str | None) -> pd.DataFrame:
    if dimension not in {"product_group_name", "product_type_name", "colour_group_name"}:
        raise ValueError("未知客户品类广度维度")
    source, params = scope.purchases(customer_dimension=group is not None)
    group_expr = customer_expression(group) if group else "CAST('全部' AS STRING)"
    return read(f"""
        WITH category_counts AS (
            SELECT UNHEX(p.customer_id) AS customer_key, {group_expr} AS segment,
                   {label(dimension)} AS category, COUNT(*) AS items
            {source}
            GROUP BY customer_key, segment, category
        ), customer_counts AS (
            SELECT segment, COUNT(*) AS breadth,
                   MAX(items) / CAST(SUM(items) AS DOUBLE) AS main_category_share
            FROM category_counts GROUP BY customer_key, segment
        )
        SELECT segment, COUNT(*) AS customer_count, AVG(breadth) AS average_breadth,
               AVG(main_category_share) AS average_main_share
        FROM customer_counts GROUP BY segment ORDER BY customer_count DESC, segment
    """, params)


def long_gap(scope: Scope) -> dict[str, int | float | None]:
    conditions, params = scope.customer_conditions()
    join_customer = "JOIN dim_customer c ON p.customer_id = c.customer_id" if scope.customer else ""
    distribution = read(f"""
        WITH customer_gaps AS (
            SELECT ARRAY_MAP((prev_day, next_day) -> IF(next_day BETWEEN %s AND %s,
                       DATEDIFF(next_day, prev_day), 0),
                       ARRAY_POPBACK(p.purchase_dates), ARRAY_POPFRONT(p.purchase_dates)) AS gaps
            FROM v_customer_purchase_dates p {join_customer}
            WHERE TRUE{conditions}
        ), long_gaps AS (
            SELECT ARRAY_FILTER(gap -> gap >= 90, gaps) AS gaps FROM customer_gaps
        )
        SELECT /*+ SET_VAR(enable_page_cache=false) */
               gap_days, COUNT(*) AS purchase_days, SUM(IF(gap_pos = 0, 1, 0)) AS first_long_gaps
        FROM long_gaps LATERAL VIEW POSEXPLODE(gaps) g AS gap_pos, gap_days
        GROUP BY gap_days ORDER BY gap_days
    """, (scope.start, scope.end, *params))
    if distribution.empty:
        return {"purchase_days": 0, "customers": 0, "median_days": None}
    totals = distribution["purchase_days"].cumsum().tolist()
    total = int(totals[-1])
    left = bisect_left(totals, (total + 1) // 2)
    right = bisect_left(totals, (total + 2) // 2)
    median = (int(distribution["gap_days"].iloc[left]) + int(distribution["gap_days"].iloc[right])) / 2
    return {"purchase_days": total, "customers": int(distribution["first_long_gaps"].sum()), "median_days": median}


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


def switch(scope: Scope) -> pd.DataFrame:
    if not scope.customer and scope.start == START and scope.end == CUTOFF:
        return read("SELECT * FROM v_channel_switch ORDER BY from_channel, to_channel")
    anchor, params = scope.predicates("p.t_dat")
    join_customer = "JOIN dim_customer c ON p.customer_id = c.customer_id" if scope.customer else ""
    return read(f"""
        WITH channel_days AS (
            SELECT BITMAP_UNION(IF(p.sales_channel_id = 1,
                       TO_BITMAP(DATEDIFF(p.t_dat, DATE '2018-09-20')), BITMAP_EMPTY())) AS one,
                   BITMAP_UNION(IF(p.sales_channel_id = 2,
                       TO_BITMAP(DATEDIFF(p.t_dat, DATE '2018-09-20')), BITMAP_EMPTY())) AS two
            FROM fact_purchase_item p {join_customer}
            WHERE {anchor} GROUP BY UNHEX(p.customer_id)
        ), channels AS (
            SELECT ARRAY_MAP(d -> IF(BITMAP_CONTAINS(one, d), 1, 0) + IF(BITMAP_CONTAINS(two, d), 2, 0),
                             BITMAP_TO_ARRAY(BITMAP_OR(one, two))) AS days
            FROM channel_days
        ), pairs AS (
            SELECT ARRAY_MAP((prev_code, next_code) -> prev_code * 10 + next_code,
                             ARRAY_POPBACK(days), ARRAY_POPFRONT(days)) AS pair_codes
            FROM channels
        ), codes AS (
            SELECT 11 AS code UNION ALL SELECT 12 UNION ALL SELECT 21 UNION ALL SELECT 22
        )
        SELECT CAST(code DIV 10 AS TINYINT) AS from_channel,
               CAST(MOD(code, 10) AS TINYINT) AS to_channel,
               SUM(ARRAY_COUNT(pair_code -> pair_code = code, pair_codes)) AS pair_count
        FROM pairs CROSS JOIN codes GROUP BY code HAVING pair_count > 0
        ORDER BY from_channel, to_channel
    """, params)


def mixed(scope: Scope) -> pd.Series:
    if not scope.customer and scope.start == START and scope.end == CUTOFF:
        return read("""
            SELECT SUM(day_count) AS total_days, SUM(IF(channel_group = '1+2', day_count, 0)) AS mixed_days
            FROM v_channel_day_profile
        """).iloc[0]
    anchor, params = scope.predicates("p.t_dat")
    join_customer = "JOIN dim_customer c ON p.customer_id = c.customer_id" if scope.customer else ""
    return read(f"""
        WITH day_channels AS (
            SELECT MIN(p.sales_channel_id) AS first_code, MAX(p.sales_channel_id) AS last_code
            FROM fact_purchase_item p {join_customer}
            WHERE {anchor} GROUP BY UNHEX(p.customer_id), p.t_dat
        )
        SELECT COUNT(*) AS total_days, SUM(IF(first_code <> last_code, 1, 0)) AS mixed_days
        FROM day_channels
    """, params).iloc[0]
