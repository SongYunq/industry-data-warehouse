-- H&M DWS / ADS refresh for hm_retail_dw.
-- Business definitions: docs/02-需求分析.md
-- DWD tables stay in 001_schema.sql. This script stores the summary and
-- application results, then republishes the v_* views as read interfaces.
-- Filtered product questions still read v_purchase_detail at query time.
-- Re-run after every fact load. INSERT OVERWRITE replaces the previous build.

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

CREATE TABLE IF NOT EXISTS dws_customer_purchase_day
(
    customer_id    VARCHAR(64) NOT NULL COMMENT "客户维度键",
    t_dat          DATE NOT NULL COMMENT "购买日",
    item_count     BIGINT NOT NULL COMMENT "当日购买件数",
    article_count  BIGINT NOT NULL COMMENT "当日不同商品编号数",
    channel_id     TINYINT NULL COMMENT "当日只有一个渠道编码时保留该编码"
)
DUPLICATE KEY(customer_id, t_dat)
DISTRIBUTED BY HASH(customer_id) BUCKETS 4
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS dws_customer_purchase_sequence
(
    customer_id         VARCHAR(64) NOT NULL COMMENT "客户维度键",
    t_dat               DATE NOT NULL COMMENT "购买日",
    item_count          BIGINT NOT NULL COMMENT "当日购买件数",
    article_count       BIGINT NOT NULL COMMENT "当日不同商品编号数",
    channel_id          TINYINT NULL COMMENT "当日只有一个渠道编码时保留该编码",
    purchase_day_no     BIGINT NOT NULL COMMENT "该客户第几个不同购买日",
    prev_purchase_date  DATE NULL COMMENT "上一个购买日",
    next_purchase_date  DATE NULL COMMENT "下一个购买日",
    prev_channel_id     TINYINT NULL COMMENT "上一个购买日的单一渠道编码"
)
DUPLICATE KEY(customer_id, t_dat)
DISTRIBUTED BY HASH(customer_id) BUCKETS 4
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS dws_customer_purchase_dates
(
    customer_id     VARCHAR(64) NOT NULL COMMENT "客户维度键",
    purchase_dates  ARRAY<DATE> NOT NULL COMMENT "该客户观察期内的购买日，升序",
    item_count      BIGINT NOT NULL COMMENT "观察期购买件数"
)
DUPLICATE KEY(customer_id)
DISTRIBUTED BY HASH(customer_id) BUCKETS 4
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS dws_customer_channel_dates
(
    customer_id    VARCHAR(64) NOT NULL COMMENT "客户维度键",
    code_one_days  BITMAP NOT NULL COMMENT "渠道编码 1 的购买日，相对 2018-09-20 的天数位图",
    code_two_days  BITMAP NOT NULL COMMENT "渠道编码 2 的购买日，相对 2018-09-20 的天数位图"
)
DUPLICATE KEY(customer_id)
DISTRIBUTED BY HASH(customer_id) BUCKETS 4
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS dws_article_description_term
(
    article_id  INT NOT NULL COMMENT "商品编号",
    term        VARCHAR(512) NOT NULL COMMENT "从商品描述抽出的 1 至 3 词条"
)
DUPLICATE KEY(article_id)
DISTRIBUTED BY HASH(article_id) BUCKETS 4
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS ads_repurchase_stage
(
    purchase_day_no      INT NOT NULL COMMENT "第几个购买日，取 1 至 3",
    horizon_days         INT NOT NULL COMMENT "观察窗口天数，30、60 或 90",
    eligible_customers   BIGINT NOT NULL COMMENT "窗口完整的客户数",
    repeat_customers     BIGINT NOT NULL COMMENT "窗口内再次购买的客户数",
    no_repeat_customers  BIGINT NOT NULL COMMENT "窗口内未观察到再次购买的客户数",
    repeat_rate          DOUBLE NULL COMMENT "再次购买客户数除以窗口完整客户数"
)
DUPLICATE KEY(purchase_day_no, horizon_days)
DISTRIBUTED BY HASH(purchase_day_no) BUCKETS 1
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS ads_first_month_repurchase
(
    first_month          DATE NOT NULL COMMENT "观察期首次购买所在月",
    horizon_days         INT NOT NULL COMMENT "观察窗口天数，30、60 或 90",
    eligible_customers   BIGINT NOT NULL COMMENT "窗口完整的客户数",
    repeat_customers     BIGINT NOT NULL COMMENT "窗口内再次购买的客户数",
    no_repeat_customers  BIGINT NOT NULL COMMENT "窗口内未观察到再次购买的客户数",
    repeat_rate          DOUBLE NULL COMMENT "再次购买客户数除以窗口完整客户数"
)
DUPLICATE KEY(first_month, horizon_days)
DISTRIBUTED BY HASH(first_month) BUCKETS 1
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS ads_customer_frequency_recency
(
    recency_band     VARCHAR(16) NOT NULL COMMENT "距 2020-09-22 的天数分段",
    frequency_band   VARCHAR(16) NOT NULL COMMENT "不同购买日数分段",
    customer_count   BIGINT NOT NULL COMMENT "客户数",
    item_count       BIGINT NOT NULL COMMENT "这些客户的历史购买件数"
)
DUPLICATE KEY(recency_band, frequency_band)
DISTRIBUTED BY HASH(recency_band) BUCKETS 1
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS ads_channel_overlap
(
    channel_group    VARCHAR(8) NOT NULL COMMENT "1、2 或 1+2",
    customer_count   BIGINT NOT NULL COMMENT "客户数",
    item_count       BIGINT NOT NULL COMMENT "购买件数"
)
DUPLICATE KEY(channel_group)
DISTRIBUTED BY HASH(channel_group) BUCKETS 1
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS ads_channel_switch
(
    from_channel  TINYINT NOT NULL COMMENT "前一个单编码购买日的渠道",
    to_channel    TINYINT NOT NULL COMMENT "后一个单编码购买日的渠道",
    pair_count    BIGINT NOT NULL COMMENT "相邻购买日对数"
)
DUPLICATE KEY(from_channel, to_channel)
DISTRIBUTED BY HASH(from_channel) BUCKETS 1
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS ads_channel_day_profile
(
    channel_group  VARCHAR(8) NOT NULL COMMENT "1、2 或 1+2",
    day_count      BIGINT NOT NULL COMMENT "客户购买日数"
)
DUPLICATE KEY(channel_group)
DISTRIBUTED BY HASH(channel_group) BUCKETS 1
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS ads_category_journey
(
    from_group  VARCHAR(64) NOT NULL COMMENT "前一个购买日的商品组",
    to_group    VARCHAR(64) NOT NULL COMMENT "当前购买日的商品组",
    pair_count  BIGINT NOT NULL COMMENT "相邻购买日的商品组配对数"
)
DUPLICATE KEY(from_group, to_group)
DISTRIBUTED BY HASH(from_group) BUCKETS 1
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS ads_monthly_overview
(
    purchase_month   DATE NOT NULL COMMENT "购买月",
    month_coverage   VARCHAR(16) NOT NULL COMMENT "完整月或部分月",
    item_count       BIGINT NOT NULL COMMENT "购买件数",
    customer_count   BIGINT NOT NULL COMMENT "购买客户数",
    style_count      BIGINT NOT NULL COMMENT "款式编号数",
    variant_count    BIGINT NOT NULL COMMENT "商品编号数"
)
DUPLICATE KEY(purchase_month)
DISTRIBUTED BY HASH(purchase_month) BUCKETS 1
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS ads_monthly_product_group
(
    purchase_month     DATE NOT NULL COMMENT "购买月",
    sales_channel_id   TINYINT NOT NULL COMMENT "源渠道编码",
    product_group      VARCHAR(64) NOT NULL COMMENT "商品组",
    item_count         BIGINT NOT NULL COMMENT "购买件数",
    customer_count     BIGINT NOT NULL COMMENT "购买客户数"
)
DUPLICATE KEY(purchase_month, sales_channel_id, product_group)
DISTRIBUTED BY HASH(purchase_month) BUCKETS 1
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS ads_monthly_colour_group
(
    purchase_month    DATE NOT NULL COMMENT "购买月",
    sales_channel_id  TINYINT NOT NULL COMMENT "源渠道编码",
    colour_group      VARCHAR(64) NOT NULL COMMENT "颜色组",
    item_count        BIGINT NOT NULL COMMENT "购买件数"
)
DUPLICATE KEY(purchase_month, sales_channel_id, colour_group)
DISTRIBUTED BY HASH(purchase_month) BUCKETS 1
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS ads_monthly_channel
(
    purchase_month    DATE NOT NULL COMMENT "购买月",
    sales_channel_id  TINYINT NOT NULL COMMENT "源渠道编码",
    item_count        BIGINT NOT NULL COMMENT "购买件数",
    customer_count    BIGINT NOT NULL COMMENT "购买客户数"
)
DUPLICATE KEY(purchase_month, sales_channel_id)
DISTRIBUTED BY HASH(purchase_month) BUCKETS 1
PROPERTIES ("replication_num" = "1");

INSERT OVERWRITE TABLE dws_customer_purchase_day
SELECT customer_id, t_dat, COUNT(*) AS item_count,
       COUNT(DISTINCT article_id) AS article_count,
       CASE WHEN MIN(sales_channel_id) = MAX(sales_channel_id)
            THEN MIN(sales_channel_id) END AS channel_id
FROM fact_purchase_item
GROUP BY customer_id, t_dat;

INSERT OVERWRITE TABLE dws_customer_purchase_sequence
SELECT customer_id, t_dat, item_count, article_count, channel_id,
       ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY t_dat) AS purchase_day_no,
       LAG(t_dat) OVER (PARTITION BY customer_id ORDER BY t_dat) AS prev_purchase_date,
       LEAD(t_dat) OVER (PARTITION BY customer_id ORDER BY t_dat) AS next_purchase_date,
       LAG(channel_id) OVER (PARTITION BY customer_id ORDER BY t_dat) AS prev_channel_id
FROM dws_customer_purchase_day;

INSERT OVERWRITE TABLE dws_customer_purchase_dates
SELECT customer_id, ARRAY_SORT(COLLECT_SET(t_dat)) AS purchase_dates, COUNT(*) AS item_count
FROM fact_purchase_item
GROUP BY customer_id;

INSERT OVERWRITE TABLE dws_customer_channel_dates
SELECT customer_id,
       IFNULL(BITMAP_UNION(CASE WHEN sales_channel_id = 1
                                THEN TO_BITMAP(DATEDIFF(t_dat, DATE '2018-09-20'))
                                ELSE BITMAP_EMPTY() END), BITMAP_EMPTY()) AS code_one_days,
       IFNULL(BITMAP_UNION(CASE WHEN sales_channel_id = 2
                                THEN TO_BITMAP(DATEDIFF(t_dat, DATE '2018-09-20'))
                                ELSE BITMAP_EMPTY() END), BITMAP_EMPTY()) AS code_two_days
FROM fact_purchase_item
GROUP BY customer_id;

INSERT OVERWRITE TABLE dws_article_description_term
WITH clauses AS (
    SELECT article_id, clause
    FROM dim_article
    LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(LOWER(detail_desc), "[^a-z[:space:]'’-]+"))
        segments AS clause
    WHERE detail_desc IS NOT NULL
), tokenized AS (
    SELECT article_id, SPLIT_BY_STRING(TRIM(REGEXP_REPLACE(clause, '[^a-z]+', ' ')), ' ') AS words
    FROM clauses
), positions AS (
    SELECT article_id, words, start_at, span
    FROM tokenized
    LATERAL VIEW EXPLODE(ARRAY_RANGE(1, CAST(SIZE(words) + 1 AS INT))) starts AS start_at
    LATERAL VIEW EXPLODE([1, 2, 3]) widths AS span
    WHERE start_at + span - 1 <= SIZE(words)
), boundaries AS (
    SELECT article_id, words, start_at, span, ELEMENT_AT(words, start_at) AS first_word,
           ELEMENT_AT(words, start_at + span - 1) AS last_word,
           ['a', 'an', 'and', 'are', 'as', 'at', 'be', 'been', 'being', 'but', 'by', 'can', 'could',
            'did', 'do', 'does', 'for', 'from', 'had', 'has', 'have', 'he', 'her', 'here', 'hers',
            'him', 'his', 'how', 'i', 'if', 'in', 'into', 'is', 'it', 'its', 'may', 'me', 'might',
            'my', 'nor', 'of', 'on', 'onto', 'or', 'our', 'ours', 'she', 'should', 'so', 'than', 'that',
            'the', 'their', 'theirs', 'them', 'then', 'there', 'these', 'they', 'this', 'those', 'through',
            'to', 'too', 'up', 'upon', 'us', 'was', 'we', 'were', 'what', 'when', 'where', 'which',
            'while', 'who', 'whom', 'will', 'with', 'would', 'you', 'your'] AS stop_words
    FROM positions
)
SELECT DISTINCT article_id, ARRAY_JOIN(ARRAY_SLICE(words, start_at, span), ' ') AS term
FROM boundaries
WHERE first_word <> '' AND NOT ARRAY_CONTAINS(stop_words, first_word)
  AND NOT ARRAY_CONTAINS(stop_words, last_word);

INSERT OVERWRITE TABLE ads_repurchase_stage
WITH horizons AS (
    SELECT 30 AS horizon_days UNION ALL SELECT 60 UNION ALL SELECT 90
), stages AS (
    SELECT 1 AS purchase_day_no UNION ALL SELECT 2 UNION ALL SELECT 3
), counts AS (
    SELECT purchase_day_no, horizon_days, COUNT(*) AS eligible_customers,
           COUNT(CASE WHEN DATEDIFF(purchase_dates[purchase_day_no + 1],
                                    purchase_dates[purchase_day_no]) <= horizon_days THEN 1 END)
               AS repeat_customers
    FROM dws_customer_purchase_dates
    CROSS JOIN horizons
    CROSS JOIN stages
    WHERE DATEDIFF(DATE '2020-09-22', purchase_dates[purchase_day_no]) >= horizon_days
    GROUP BY purchase_day_no, horizon_days
)
SELECT purchase_day_no, horizon_days, eligible_customers, repeat_customers,
       eligible_customers - repeat_customers AS no_repeat_customers,
       repeat_customers / CAST(eligible_customers AS DOUBLE) AS repeat_rate
FROM counts;

INSERT OVERWRITE TABLE ads_first_month_repurchase
WITH horizons AS (
    SELECT 30 AS horizon_days UNION ALL SELECT 60 UNION ALL SELECT 90
), counts AS (
    SELECT CAST(DATE_TRUNC(purchase_dates[1], 'month') AS DATE) AS first_month,
           horizon_days, COUNT(*) AS eligible_customers,
           COUNT(CASE WHEN DATEDIFF(purchase_dates[2], purchase_dates[1]) <= horizon_days THEN 1 END)
               AS repeat_customers
    FROM dws_customer_purchase_dates
    CROSS JOIN horizons
    WHERE DATEDIFF(DATE '2020-09-22', purchase_dates[1]) >= horizon_days
    GROUP BY CAST(DATE_TRUNC(purchase_dates[1], 'month') AS DATE), horizon_days
)
SELECT first_month, horizon_days, eligible_customers, repeat_customers,
       eligible_customers - repeat_customers AS no_repeat_customers,
       repeat_customers / CAST(eligible_customers AS DOUBLE) AS repeat_rate
FROM counts;

INSERT OVERWRITE TABLE ads_customer_frequency_recency
WITH customer_frequency AS (
    SELECT DATEDIFF(DATE '2020-09-22', purchase_dates[-1]) AS recency_days,
           SIZE(purchase_dates) AS frequency, item_count AS items
    FROM dws_customer_purchase_dates
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

INSERT OVERWRITE TABLE ads_channel_overlap
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

INSERT OVERWRITE TABLE ads_channel_switch
WITH customer_channels AS (
    SELECT ARRAY_MAP(day_offset -> IF(BITMAP_CONTAINS(code_one_days, day_offset), 1, 0) +
                                   IF(BITMAP_CONTAINS(code_two_days, day_offset), 2, 0),
                     BITMAP_TO_ARRAY(BITMAP_OR(code_one_days, code_two_days))) AS daily_channels
    FROM dws_customer_channel_dates
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

INSERT OVERWRITE TABLE ads_channel_day_profile
WITH totals AS (
    SELECT SUM(BITMAP_COUNT(code_one_days)) AS one_days,
           SUM(BITMAP_COUNT(code_two_days)) AS two_days,
           SUM(BITMAP_AND_COUNT(code_one_days, code_two_days)) AS mixed_days
    FROM dws_customer_channel_dates
), channels AS (
    SELECT '1' AS channel_group UNION ALL SELECT '2' UNION ALL SELECT '1+2'
)
SELECT channel_group, CASE channel_group WHEN '1' THEN one_days - mixed_days
                                         WHEN '2' THEN two_days - mixed_days
                                         ELSE mixed_days END AS day_count
FROM totals CROSS JOIN channels;

INSERT OVERWRITE TABLE ads_category_journey
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

INSERT OVERWRITE TABLE ads_monthly_overview
SELECT CAST(DATE_TRUNC(t_dat, 'month') AS DATE) AS purchase_month,
       CASE WHEN CAST(DATE_TRUNC(t_dat, 'month') AS DATE) IN
                      (DATE '2018-09-01', DATE '2020-09-01') THEN '部分月'
            ELSE '完整月' END AS month_coverage,
       COUNT(*) AS item_count, COUNT(DISTINCT customer_id) AS customer_count,
       COUNT(DISTINCT product_code) AS style_count, COUNT(DISTINCT article_id) AS variant_count
FROM v_purchase_detail
GROUP BY purchase_month, month_coverage;

INSERT OVERWRITE TABLE ads_monthly_product_group
SELECT CAST(DATE_TRUNC(t_dat, 'month') AS DATE) AS purchase_month, sales_channel_id,
       COALESCE(product_group_name, '未知') AS product_group,
       COUNT(*) AS item_count, COUNT(DISTINCT customer_id) AS customer_count
FROM v_purchase_detail
GROUP BY purchase_month, sales_channel_id, product_group;

INSERT OVERWRITE TABLE ads_monthly_colour_group
SELECT CAST(DATE_TRUNC(t_dat, 'month') AS DATE) AS purchase_month, sales_channel_id,
       COALESCE(colour_group_name, '未知') AS colour_group,
       COUNT(*) AS item_count
FROM v_purchase_detail
GROUP BY purchase_month, sales_channel_id, colour_group;

INSERT OVERWRITE TABLE ads_monthly_channel
SELECT CAST(DATE_TRUNC(t_dat, 'month') AS DATE) AS purchase_month, sales_channel_id,
       COUNT(*) AS item_count, COUNT(DISTINCT customer_id) AS customer_count
FROM fact_purchase_item
GROUP BY purchase_month, sales_channel_id;

CREATE VIEW IF NOT EXISTS v_customer_purchase_day AS
SELECT customer_id, t_dat, item_count, article_count, channel_id
FROM dws_customer_purchase_day;

CREATE VIEW IF NOT EXISTS v_customer_purchase_sequence AS
SELECT customer_id, t_dat, item_count, article_count, channel_id,
       purchase_day_no, prev_purchase_date, next_purchase_date, prev_channel_id
FROM dws_customer_purchase_sequence;

CREATE VIEW IF NOT EXISTS v_customer_purchase_dates AS
SELECT customer_id, purchase_dates, item_count
FROM dws_customer_purchase_dates;

CREATE VIEW IF NOT EXISTS v_customer_channel_dates AS
SELECT code_one_days, code_two_days
FROM dws_customer_channel_dates;

CREATE VIEW IF NOT EXISTS v_article_description_term AS
SELECT article_id, term
FROM dws_article_description_term;

CREATE VIEW IF NOT EXISTS v_repurchase_stage AS
SELECT purchase_day_no, horizon_days, eligible_customers, repeat_customers,
       no_repeat_customers, repeat_rate
FROM ads_repurchase_stage;

CREATE VIEW IF NOT EXISTS v_first_month_repurchase AS
SELECT first_month, horizon_days, eligible_customers, repeat_customers,
       no_repeat_customers, repeat_rate
FROM ads_first_month_repurchase;

CREATE VIEW IF NOT EXISTS v_customer_frequency_recency AS
SELECT recency_band, frequency_band, customer_count, item_count
FROM ads_customer_frequency_recency;

CREATE VIEW IF NOT EXISTS v_channel_overlap AS
SELECT channel_group, customer_count, item_count
FROM ads_channel_overlap;

CREATE VIEW IF NOT EXISTS v_channel_switch AS
SELECT from_channel, to_channel, pair_count
FROM ads_channel_switch;

CREATE VIEW IF NOT EXISTS v_channel_day_profile AS
SELECT channel_group, day_count
FROM ads_channel_day_profile;

CREATE VIEW IF NOT EXISTS v_category_journey AS
SELECT from_group, to_group, pair_count
FROM ads_category_journey;

CREATE VIEW IF NOT EXISTS v_monthly_overview AS
SELECT purchase_month, month_coverage, item_count, customer_count, style_count, variant_count
FROM ads_monthly_overview;

CREATE VIEW IF NOT EXISTS v_monthly_product_group AS
SELECT purchase_month, sales_channel_id, product_group, item_count, customer_count
FROM ads_monthly_product_group;

CREATE VIEW IF NOT EXISTS v_monthly_colour_group AS
SELECT purchase_month, sales_channel_id, colour_group, item_count
FROM ads_monthly_colour_group;

CREATE VIEW IF NOT EXISTS v_monthly_channel AS
SELECT purchase_month, sales_channel_id, item_count, customer_count
FROM ads_monthly_channel;
