-- H&M 分析库的 Doris 4.0.8 建表脚本。
-- 来源 CSV 保存在本地 Git 忽略目录，由 data-load 校验和装载。

CREATE DATABASE IF NOT EXISTS hm_retail_dw;
USE hm_retail_dw;

CREATE TABLE IF NOT EXISTS dim_customer
(
    customer_id             VARCHAR(64) NOT NULL COMMENT "源客户伪名键",
    fn_source_flag          VARCHAR(8) NULL COMMENT "源 FN 标记，空值保持未知",
    active_source_flag      VARCHAR(8) NULL COMMENT "源 Active 标记，空值保持未知",
    club_member_status      VARCHAR(32) NULL COMMENT "客户属性快照中的会员状态",
    fashion_news_frequency  VARCHAR(32) NULL COMMENT "客户属性快照中的资讯频率",
    age_years               SMALLINT NULL COMMENT "客户属性快照中的年龄"
)
UNIQUE KEY(customer_id)
DISTRIBUTED BY HASH(customer_id) BUCKETS 1
PROPERTIES (
    "replication_num" = "1"
);

CREATE TABLE IF NOT EXISTS dim_article
(
    article_id                    INT NOT NULL COMMENT "源商品款式键",
    product_code                  INT NULL COMMENT "源商品编码，可作为候选款式族汇总键",
    prod_name                     VARCHAR(64) NULL COMMENT "商品名称",
    product_type_no               SMALLINT NULL COMMENT "商品类型编码",
    product_type_name             VARCHAR(64) NULL COMMENT "商品类型名称",
    product_group_name            VARCHAR(64) NULL COMMENT "商品组名称",
    graphical_appearance_no       INT NULL COMMENT "图案编码",
    graphical_appearance_name     VARCHAR(64) NULL COMMENT "图案名称",
    colour_group_code             SMALLINT NULL COMMENT "颜色组编码",
    colour_group_name             VARCHAR(64) NULL COMMENT "颜色组名称",
    perceived_colour_value_id     SMALLINT NULL COMMENT "感知颜色明暗编码",
    perceived_colour_value_name   VARCHAR(32) NULL COMMENT "感知颜色明暗名称",
    perceived_colour_master_id    SMALLINT NULL COMMENT "主颜色编码",
    perceived_colour_master_name  VARCHAR(32) NULL COMMENT "主颜色名称",
    department_no                 SMALLINT NULL COMMENT "部门编码",
    department_name               VARCHAR(96) NULL COMMENT "部门名称",
    index_code                    VARCHAR(8) NULL COMMENT "源 index 编码",
    index_name                    VARCHAR(64) NULL COMMENT "源 index 名称",
    index_group_no                SMALLINT NULL COMMENT "源 index group 编码",
    index_group_name              VARCHAR(32) NULL COMMENT "源 index group 名称",
    section_no                    SMALLINT NULL COMMENT "版块编码",
    section_name                  VARCHAR(64) NULL COMMENT "版块名称",
    garment_group_no              SMALLINT NULL COMMENT "服装组编码",
    garment_group_name            VARCHAR(64) NULL COMMENT "服装组名称",
    detail_desc                   VARCHAR(1024) NULL COMMENT "商品描述；源表有空值"
)
UNIQUE KEY(article_id)
DISTRIBUTED BY HASH(article_id) BUCKETS 1
PROPERTIES (
    "replication_num" = "1"
);

CREATE TABLE IF NOT EXISTS dim_date
(
    date_key       INT NOT NULL COMMENT "YYYYMMDD 日期键",
    calendar_date  DATE NOT NULL COMMENT "自然日期",
    year_num       SMALLINT NOT NULL COMMENT "自然年",
    quarter_num    TINYINT NOT NULL COMMENT "自然季度",
    month_num      TINYINT NOT NULL COMMENT "自然月",
    day_num        TINYINT NOT NULL COMMENT "月内日",
    weekday_num    TINYINT NOT NULL COMMENT "星期序号，周一为 1",
    iso_week_year  SMALLINT NOT NULL COMMENT "ISO 周所属年份",
    iso_week_num   TINYINT NOT NULL COMMENT "ISO 周序号",
    is_weekend     BOOLEAN NOT NULL COMMENT "是否周末"
)
UNIQUE KEY(date_key)
DISTRIBUTED BY HASH(date_key) BUCKETS 1
PROPERTIES (
    "replication_num" = "1"
);

CREATE TABLE IF NOT EXISTS fact_purchase_item
(
    t_dat             DATE NOT NULL COMMENT "商品购买发生日期",
    customer_id       VARCHAR(64) NOT NULL COMMENT "客户维度键",
    article_id        INT NOT NULL COMMENT "商品款式维度键",
    sales_channel_id  TINYINT NOT NULL COMMENT "源渠道编码，业务标签待核实",
    source_price_value DOUBLE NOT NULL COMMENT "源 price 数值；非货币口径"
)
DUPLICATE KEY(t_dat, customer_id, article_id)
PARTITION BY RANGE(t_dat)
(
    FROM ("2018-09-01") TO ("2020-10-01") INTERVAL 1 MONTH
)
DISTRIBUTED BY HASH(customer_id) BUCKETS 4
PROPERTIES (
    "replication_num" = "1"
);
