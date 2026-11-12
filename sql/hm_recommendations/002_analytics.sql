-- H&M business definitions: docs/02-需求分析.md
-- Run in hm_retail_dw after creating the warehouse tables.

CREATE VIEW IF NOT EXISTS v_purchase_detail AS
SELECT f.t_dat, f.customer_id, f.article_id, f.sales_channel_id, f.source_price_value,
       a.product_code, a.prod_name, a.product_type_no, a.product_type_name,
       a.product_group_name, a.graphical_appearance_no, a.graphical_appearance_name,
       a.colour_group_code, a.colour_group_name, a.perceived_colour_value_id,
       a.perceived_colour_value_name, a.perceived_colour_master_id,
       a.perceived_colour_master_name, a.department_no, a.department_name,
       a.index_code, a.index_name, a.index_group_no, a.index_group_name,
       a.section_no, a.section_name, a.garment_group_no, a.garment_group_name
FROM fact_purchase_item f
JOIN dim_article a ON a.article_id = f.article_id;

CREATE VIEW IF NOT EXISTS v_customer_purchase_day AS
SELECT customer_id, t_dat, COUNT(*) AS item_count,
       COUNT(DISTINCT article_id) AS article_count,
       CASE WHEN MIN(sales_channel_id) = MAX(sales_channel_id)
            THEN MIN(sales_channel_id) END AS channel_id
FROM fact_purchase_item
GROUP BY customer_id, t_dat;

CREATE VIEW IF NOT EXISTS v_customer_purchase_sequence AS
SELECT customer_id, t_dat, item_count, article_count, channel_id,
       ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY t_dat) AS purchase_day_no,
       LAG(t_dat) OVER (PARTITION BY customer_id ORDER BY t_dat) AS prev_purchase_date,
       LEAD(t_dat) OVER (PARTITION BY customer_id ORDER BY t_dat) AS next_purchase_date,
       LAG(channel_id) OVER (PARTITION BY customer_id ORDER BY t_dat) AS prev_channel_id
FROM v_customer_purchase_day;

CREATE VIEW IF NOT EXISTS v_customer_purchase_dates AS
SELECT customer_id, ARRAY_SORT(COLLECT_SET(t_dat)) AS purchase_dates, COUNT(*) AS item_count
FROM fact_purchase_item
GROUP BY customer_id;

CREATE VIEW IF NOT EXISTS v_repurchase_stage AS
WITH horizons AS (
    SELECT 30 AS horizon_days UNION ALL SELECT 60 UNION ALL SELECT 90
), stages AS (
    SELECT 1 AS purchase_day_no UNION ALL SELECT 2 UNION ALL SELECT 3
), counts AS (
    SELECT purchase_day_no, horizon_days, COUNT(*) AS eligible_customers,
           COUNT(CASE WHEN DATEDIFF(purchase_dates[purchase_day_no + 1],
                                    purchase_dates[purchase_day_no]) <= horizon_days THEN 1 END)
               AS repeat_customers
    FROM v_customer_purchase_dates
    CROSS JOIN horizons
    CROSS JOIN stages
    WHERE DATEDIFF(DATE '2020-09-22', purchase_dates[purchase_day_no]) >= horizon_days
    GROUP BY purchase_day_no, horizon_days
)
SELECT purchase_day_no, horizon_days, eligible_customers, repeat_customers,
       eligible_customers - repeat_customers AS no_repeat_customers,
       repeat_customers / CAST(eligible_customers AS DOUBLE) AS repeat_rate
FROM counts;

CREATE VIEW IF NOT EXISTS v_first_month_repurchase AS
WITH horizons AS (
    SELECT 30 AS horizon_days UNION ALL SELECT 60 UNION ALL SELECT 90
), counts AS (
    SELECT CAST(DATE_TRUNC(purchase_dates[1], 'month') AS DATE) AS first_month,
           horizon_days, COUNT(*) AS eligible_customers,
           COUNT(CASE WHEN DATEDIFF(purchase_dates[2], purchase_dates[1]) <= horizon_days THEN 1 END)
               AS repeat_customers
    FROM v_customer_purchase_dates
    CROSS JOIN horizons
    WHERE DATEDIFF(DATE '2020-09-22', purchase_dates[1]) >= horizon_days
    GROUP BY CAST(DATE_TRUNC(purchase_dates[1], 'month') AS DATE), horizon_days
)
SELECT first_month, horizon_days, eligible_customers, repeat_customers,
       eligible_customers - repeat_customers AS no_repeat_customers,
       repeat_customers / CAST(eligible_customers AS DOUBLE) AS repeat_rate
FROM counts;

CREATE VIEW IF NOT EXISTS v_customer_frequency_recency AS
WITH customer_frequency AS (
    SELECT DATEDIFF(DATE '2020-09-22', purchase_dates[-1]) AS recency_days,
           SIZE(purchase_dates) AS frequency, item_count AS items
    FROM v_customer_purchase_dates
), bands AS (
    SELECT CASE WHEN recency_days <= 30 THEN '0–30'
                WHEN recency_days <= 60 THEN '31–60'
                WHEN recency_days <= 90 THEN '61–90'
                ELSE '91+' END AS recency_band,
           CASE WHEN frequency >= 4 THEN '4+' ELSE CAST(frequency AS VARCHAR) END AS frequency_band,
           items
    FROM customer_frequency
)
SELECT recency_band, frequency_band, COUNT(*) AS customer_count, SUM(items) AS item_count
FROM bands
GROUP BY recency_band, frequency_band;

CREATE VIEW IF NOT EXISTS v_channel_overlap AS
WITH customer_channels AS (
    SELECT customer_id, MIN(sales_channel_id) AS first_code, MAX(sales_channel_id) AS last_code,
           COUNT(*) AS items
    FROM fact_purchase_item
    GROUP BY customer_id
), channels AS (
    SELECT CASE WHEN first_code = last_code THEN CAST(first_code AS VARCHAR)
                ELSE '1+2' END AS channel_group,
           items
    FROM customer_channels
)
SELECT channel_group, COUNT(*) AS customer_count, SUM(items) AS item_count
FROM channels
GROUP BY channel_group;

CREATE VIEW IF NOT EXISTS v_customer_channel_dates AS
-- The loader validates 64 lowercase hex digits, so UNHEX preserves the full customer key.
SELECT BITMAP_UNION(CASE WHEN sales_channel_id = 1
                         THEN TO_BITMAP(DATEDIFF(t_dat, DATE '2018-09-20'))
                         ELSE BITMAP_EMPTY() END) AS code_one_days,
       BITMAP_UNION(CASE WHEN sales_channel_id = 2
                         THEN TO_BITMAP(DATEDIFF(t_dat, DATE '2018-09-20'))
                         ELSE BITMAP_EMPTY() END) AS code_two_days
FROM fact_purchase_item
GROUP BY UNHEX(customer_id);

CREATE VIEW IF NOT EXISTS v_channel_switch AS
WITH customer_channels AS (
    SELECT ARRAY_MAP(day_offset -> IF(BITMAP_CONTAINS(code_one_days, day_offset), 1, 0) +
                                   IF(BITMAP_CONTAINS(code_two_days, day_offset), 2, 0),
                     BITMAP_TO_ARRAY(BITMAP_OR(code_one_days, code_two_days))) AS daily_channels
    FROM v_customer_channel_dates
), pairs AS (
    SELECT ARRAY_MAP((p, n) -> p * 10 + n, ARRAY_POPBACK(daily_channels),
                     ARRAY_POPFRONT(daily_channels)) AS pair_codes
    FROM customer_channels
), codes AS (
    SELECT 11 AS pair_code UNION ALL SELECT 12 UNION ALL SELECT 21 UNION ALL SELECT 22
)
SELECT CAST(pair_code DIV 10 AS TINYINT) AS from_channel,
       CAST(pair_code % 10 AS TINYINT) AS to_channel,
       SUM(ARRAY_COUNT(p -> p = pair_code, pair_codes)) AS pair_count
FROM pairs CROSS JOIN codes
GROUP BY pair_code
HAVING pair_count > 0;

CREATE VIEW IF NOT EXISTS v_channel_day_profile AS
WITH totals AS (
    SELECT SUM(BITMAP_COUNT(code_one_days)) AS one_days,
           SUM(BITMAP_COUNT(code_two_days)) AS two_days,
           SUM(BITMAP_AND_COUNT(code_one_days, code_two_days)) AS mixed_days
    FROM v_customer_channel_dates
), channels AS (
    SELECT '1' AS channel_group UNION ALL SELECT '2' UNION ALL SELECT '1+2'
)
SELECT channel_group, CASE channel_group WHEN '1' THEN one_days - mixed_days
                                         WHEN '2' THEN two_days - mixed_days
                                         ELSE mixed_days END AS day_count
FROM totals CROSS JOIN channels;

CREATE VIEW IF NOT EXISTS v_category_journey AS
-- The fixed H&M catalog has 19 product groups, which fit in a BIGINT bit mask.
WITH group_names AS (
    SELECT DISTINCT COALESCE(product_group_name, '未知') AS group_name FROM dim_article
), group_codes AS (
    SELECT group_name, ROW_NUMBER() OVER (ORDER BY group_name) - 1 AS group_code FROM group_names
), article_groups AS (
    SELECT article_id, BIT_SHIFT_LEFT(CAST(1 AS BIGINT), group_code) AS group_bit
    FROM dim_article a JOIN group_codes g ON COALESCE(a.product_group_name, '未知') = g.group_name
), day_groups AS (
    SELECT UNHEX(f.customer_id) AS customer_key, f.t_dat, GROUP_BIT_OR(a.group_bit) AS groups_on_day
    FROM fact_purchase_item f JOIN article_groups a ON f.article_id = a.article_id
    GROUP BY UNHEX(f.customer_id), f.t_dat
), adjacent_days AS (
    SELECT groups_on_day,
           LAG(groups_on_day) OVER (PARTITION BY customer_key ORDER BY t_dat) AS groups_on_prev_day
    FROM day_groups
), bit_positions AS (
    SELECT ARRAY_RANGE(CAST(COUNT(*) AS INT)) AS positions FROM group_names
), category_sets AS (
    SELECT ARRAY_FILTER(bit -> BITAND(groups_on_prev_day, BIT_SHIFT_LEFT(CAST(1 AS BIGINT), bit)) <> 0,
                        positions) AS prev_categories,
           ARRAY_FILTER(bit -> BITAND(groups_on_day, BIT_SHIFT_LEFT(CAST(1 AS BIGINT), bit)) <> 0,
                        positions) AS current_categories
    FROM adjacent_days CROSS JOIN bit_positions
    WHERE groups_on_prev_day IS NOT NULL
), pair_counts AS (
    SELECT from_code, to_code, COUNT(*) AS pair_count
    FROM category_sets
    LATERAL VIEW EXPLODE(prev_categories) p AS from_code
    LATERAL VIEW EXPLODE(current_categories) n AS to_code
    GROUP BY from_code, to_code
)
SELECT p.group_name AS from_group, n.group_name AS to_group, pair_count
FROM pair_counts
JOIN group_codes p ON from_code = p.group_code
JOIN group_codes n ON to_code = n.group_code;

CREATE VIEW IF NOT EXISTS v_monthly_overview AS
SELECT CAST(DATE_TRUNC(t_dat, 'month') AS DATE) AS purchase_month,
       CASE WHEN CAST(DATE_TRUNC(t_dat, 'month') AS DATE) IN
                      (DATE '2018-09-01', DATE '2020-09-01') THEN '部分月'
            ELSE '完整月' END AS month_coverage,
       COUNT(*) AS item_count, COUNT(DISTINCT customer_id) AS customer_count,
       COUNT(DISTINCT product_code) AS style_count, COUNT(DISTINCT article_id) AS variant_count
FROM v_purchase_detail
GROUP BY purchase_month, month_coverage;

CREATE VIEW IF NOT EXISTS v_monthly_product_group AS
SELECT CAST(DATE_TRUNC(t_dat, 'month') AS DATE) AS purchase_month, sales_channel_id,
       COALESCE(product_group_name, '未知') AS product_group,
       COUNT(*) AS item_count, COUNT(DISTINCT customer_id) AS customer_count
FROM v_purchase_detail
GROUP BY purchase_month, sales_channel_id, product_group;

CREATE VIEW IF NOT EXISTS v_monthly_colour_group AS
SELECT CAST(DATE_TRUNC(t_dat, 'month') AS DATE) AS purchase_month, sales_channel_id,
       COALESCE(colour_group_name, '未知') AS colour_group,
       COUNT(*) AS item_count
FROM v_purchase_detail
GROUP BY purchase_month, sales_channel_id, colour_group;

CREATE VIEW IF NOT EXISTS v_monthly_channel AS
SELECT CAST(DATE_TRUNC(t_dat, 'month') AS DATE) AS purchase_month, sales_channel_id,
       COUNT(*) AS item_count, COUNT(DISTINCT customer_id) AS customer_count
FROM fact_purchase_item
GROUP BY purchase_month, sales_channel_id;
