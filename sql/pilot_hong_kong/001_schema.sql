CREATE DATABASE IF NOT EXISTS hk_airbnb_pilot;

USE hk_airbnb_pilot;

CREATE TABLE IF NOT EXISTS dim_city (
    city_key VARCHAR(32) NOT NULL COMMENT '城市自然键',
    city_name VARCHAR(64) NOT NULL COMMENT '城市名称',
    country_or_region VARCHAR(64) NOT NULL COMMENT '国家或地区',
    currency_code CHAR(3) NOT NULL COMMENT '挂牌价源字段币种代码',
    time_zone VARCHAR(64) NOT NULL COMMENT '城市时区',
    source_name VARCHAR(64) NOT NULL COMMENT '数据来源'
)
ENGINE=OLAP
UNIQUE KEY(city_key)
DISTRIBUTED BY HASH(city_key) BUCKETS 1
PROPERTIES ("replication_num" = "1", "enable_unique_key_merge_on_write" = "true");

CREATE TABLE IF NOT EXISTS dim_snapshot (
    city_key VARCHAR(32) NOT NULL COMMENT '城市自然键',
    snapshot_date DATE NOT NULL COMMENT '来源快照日期',
    scrape_id VARCHAR(32) COMMENT '来源抓取批次标识',
    last_scraped_date DATE COMMENT '房源记录源字段抓取日期',
    snapshot_year SMALLINT COMMENT '快照年份',
    snapshot_quarter TINYINT COMMENT '快照季度',
    listing_source_rows BIGINT COMMENT '房源源文件记录数'
)
ENGINE=OLAP
UNIQUE KEY(city_key, snapshot_date)
DISTRIBUTED BY HASH(snapshot_date) BUCKETS 1
PROPERTIES ("replication_num" = "1", "enable_unique_key_merge_on_write" = "true");

CREATE TABLE IF NOT EXISTS dim_date (
    calendar_date DATE NOT NULL COMMENT '自然日日期键',
    calendar_year SMALLINT COMMENT '年份',
    calendar_quarter TINYINT COMMENT '季度',
    calendar_month TINYINT COMMENT '月份',
    day_of_month TINYINT COMMENT '月内日期',
    day_of_week TINYINT COMMENT '星期序号，周一为 1',
    day_name VARCHAR(16) COMMENT '星期名称',
    is_weekend BOOLEAN COMMENT '是否周末'
)
ENGINE=OLAP
UNIQUE KEY(calendar_date)
DISTRIBUTED BY HASH(calendar_date) BUCKETS 1
PROPERTIES ("replication_num" = "1", "enable_unique_key_merge_on_write" = "true");

CREATE TABLE IF NOT EXISTS dim_neighbourhood (
    city_key VARCHAR(32) NOT NULL COMMENT '城市自然键',
    neighbourhood VARCHAR(128) NOT NULL COMMENT '清洗后的街区名称',
    neighbourhood_group VARCHAR(128) COMMENT '来源街区组；香港数据当前为空',
    geometry_json STRING COMMENT '街区边界 GeoJSON Geometry'
)
ENGINE=OLAP
UNIQUE KEY(city_key, neighbourhood)
DISTRIBUTED BY HASH(neighbourhood) BUCKETS 1
PROPERTIES ("replication_num" = "1", "enable_unique_key_merge_on_write" = "true");

CREATE TABLE IF NOT EXISTS dim_room_type (
    room_type VARCHAR(64) NOT NULL COMMENT '来源房型类别',
    sort_order INT COMMENT '看板排序序号'
)
ENGINE=OLAP
UNIQUE KEY(room_type)
DISTRIBUTED BY HASH(room_type) BUCKETS 1
PROPERTIES ("replication_num" = "1", "enable_unique_key_merge_on_write" = "true");

CREATE TABLE IF NOT EXISTS dim_property_type (
    property_type VARCHAR(256) NOT NULL COMMENT '来源房屋类型类别',
    sort_order INT COMMENT '看板排序序号'
)
ENGINE=OLAP
UNIQUE KEY(property_type)
DISTRIBUTED BY HASH(property_type) BUCKETS 1
PROPERTIES ("replication_num" = "1", "enable_unique_key_merge_on_write" = "true");

CREATE TABLE IF NOT EXISTS dim_host_snapshot (
    city_key VARCHAR(32) NOT NULL COMMENT '城市自然键',
    snapshot_date DATE NOT NULL COMMENT '属性观测快照日期',
    host_id BIGINT NOT NULL COMMENT '来源公开房东标识，不在看板展示',
    host_is_superhost BOOLEAN COMMENT '来源超赞房东标记',
    host_identity_verified BOOLEAN COMMENT '来源身份验证标记',
    host_since DATE COMMENT '房东加入日期',
    host_listings_count INT COMMENT '房东房源数来源字段',
    host_total_listings_count INT COMMENT '房东总房源数来源字段',
    calculated_host_listings_count INT COMMENT '当前城市已计算房源数',
    host_response_rate DECIMAL(5,2) COMMENT '来源回复率百分比',
    host_acceptance_rate DECIMAL(5,2) COMMENT '来源接受率百分比',
    entire_home_listing_count INT COMMENT '整套房源数',
    private_room_listing_count INT COMMENT '独立房间房源数',
    shared_room_listing_count INT COMMENT '合住房间房源数'
)
ENGINE=OLAP
UNIQUE KEY(city_key, snapshot_date, host_id)
DISTRIBUTED BY HASH(host_id) BUCKETS 1
PROPERTIES ("replication_num" = "1", "enable_unique_key_merge_on_write" = "true");

CREATE TABLE IF NOT EXISTS fact_listing_snapshot (
    city_key VARCHAR(32) NOT NULL COMMENT '城市自然键，对应 dim_city',
    snapshot_date DATE NOT NULL COMMENT '快照日期，对应 dim_snapshot',
    listing_id BIGINT NOT NULL COMMENT 'Inside Airbnb 房源标识',
    host_id BIGINT COMMENT '房东自然键，对应同快照 dim_host_snapshot',
    neighbourhood VARCHAR(128) COMMENT '街区自然键，对应 dim_neighbourhood',
    property_type VARCHAR(256) COMMENT '房屋类型自然键，对应 dim_property_type',
    room_type VARCHAR(64) COMMENT '房型自然键，对应 dim_room_type',
    accommodates INT COMMENT '可容纳人数',
    bedrooms DECIMAL(8, 2) COMMENT '卧室数',
    beds DECIMAL(8, 2) COMMENT '床位数',
    bathrooms DECIMAL(8, 2) COMMENT '浴室数',
    price DECIMAL(16, 2) COMMENT '源字段挂牌价，空值保留为 NULL',
    minimum_nights INT COMMENT '最少入住晚数限制',
    maximum_nights INT COMMENT '最多入住晚数限制',
    availability_30 INT COMMENT '未来 30 天显示可订天数',
    availability_60 INT COMMENT '未来 60 天显示可订天数',
    availability_90 INT COMMENT '未来 90 天显示可订天数',
    availability_365 INT COMMENT '未来 365 天显示可订天数',
    number_of_reviews INT COMMENT '累计评论数',
    number_of_reviews_ltm INT COMMENT '源字段近 12 个月评论数',
    review_scores_rating DECIMAL(4, 2) COMMENT '房源评分',
    latitude DECIMAL(10, 7) COMMENT '源文件匿名化纬度，仅供街区聚合',
    longitude DECIMAL(10, 7) COMMENT '源文件匿名化经度，仅供街区聚合'
)
ENGINE=OLAP
UNIQUE KEY(city_key, snapshot_date, listing_id)
DISTRIBUTED BY HASH(listing_id) BUCKETS 1
PROPERTIES ("replication_num" = "1", "enable_unique_key_merge_on_write" = "true");

CREATE TABLE IF NOT EXISTS fact_calendar_availability_day (
    city_key VARCHAR(32) NOT NULL COMMENT '城市自然键',
    snapshot_date DATE NOT NULL COMMENT '来源快照日期',
    calendar_date DATE NOT NULL COMMENT '日历中的入住日期，对应 dim_date',
    neighbourhood VARCHAR(128) NOT NULL COMMENT '街区自然键，对应 dim_neighbourhood',
    room_type VARCHAR(64) NOT NULL COMMENT '房型自然键，对应 dim_room_type',
    property_type VARCHAR(256) NOT NULL COMMENT '房屋类型自然键，对应 dim_property_type',
    listing_count INT COMMENT '该维度组合的房态记录房源数',
    available_listing_count INT COMMENT '来源标记可订的房源数',
    unavailable_listing_count INT COMMENT '来源标记不可订的房源数',
    unknown_status_count INT COMMENT '状态缺失或无法识别的房源数'
)
ENGINE=OLAP
UNIQUE KEY(city_key, snapshot_date, calendar_date, neighbourhood, room_type, property_type)
DISTRIBUTED BY HASH(neighbourhood) BUCKETS 1
PROPERTIES ("replication_num" = "1", "enable_unique_key_merge_on_write" = "true");

CREATE TABLE IF NOT EXISTS fact_review_monthly (
    city_key VARCHAR(32) NOT NULL COMMENT '城市自然键',
    review_month DATE NOT NULL COMMENT '评论月份，存储该月第一天并对应 dim_date',
    review_count BIGINT COMMENT '评论记录数',
    reviewed_listing_count BIGINT COMMENT '有评论的不同房源数',
    earliest_review_date DATE COMMENT '该月最早评论日期',
    latest_review_date DATE COMMENT '该月最晚评论日期'
)
ENGINE=OLAP
UNIQUE KEY(city_key, review_month)
DISTRIBUTED BY HASH(review_month) BUCKETS 1
PROPERTIES ("replication_num" = "1", "enable_unique_key_merge_on_write" = "true");
