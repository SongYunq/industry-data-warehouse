-- 自动生成自已核验的真实文件表头（初次原始接入方案）
CREATE DATABASE IF NOT EXISTS `raw_lade`;

CREATE TABLE IF NOT EXISTS `raw_lade`.`delivery_jl` (
    `_ingest_batch_id` VARCHAR(64) NOT NULL COMMENT '导入批次标识',
    `_source_file` VARCHAR(1024) NOT NULL COMMENT '源文件标识（不含敏感本机路径）',
    `_source_sha256` VARCHAR(64) NOT NULL COMMENT '原始文件 SHA-256 校验值',
    `_ingested_at` DATETIMEV2(3) NOT NULL COMMENT '批次接入时间',
    `order_id` STRING NULL COMMENT '源文件原始文本字段: order_id',
    `region_id` STRING NULL COMMENT '源文件原始文本字段: region_id',
    `city` STRING NULL COMMENT '源文件原始文本字段: city',
    `courier_id` STRING NULL COMMENT '源文件原始文本字段: courier_id',
    `lng` STRING NULL COMMENT '源文件原始文本字段: lng',
    `lat` STRING NULL COMMENT '源文件原始文本字段: lat',
    `aoi_id` STRING NULL COMMENT '源文件原始文本字段: aoi_id',
    `aoi_type` STRING NULL COMMENT '源文件原始文本字段: aoi_type',
    `accept_time` STRING NULL COMMENT '源文件原始文本字段: accept_time',
    `accept_gps_time` STRING NULL COMMENT '源文件原始文本字段: accept_gps_time',
    `accept_gps_lng` STRING NULL COMMENT '源文件原始文本字段: accept_gps_lng',
    `accept_gps_lat` STRING NULL COMMENT '源文件原始文本字段: accept_gps_lat',
    `delivery_time` STRING NULL COMMENT '源文件原始文本字段: delivery_time',
    `delivery_gps_time` STRING NULL COMMENT '源文件原始文本字段: delivery_gps_time',
    `delivery_gps_lng` STRING NULL COMMENT '源文件原始文本字段: delivery_gps_lng',
    `delivery_gps_lat` STRING NULL COMMENT '源文件原始文本字段: delivery_gps_lat',
    `ds` STRING NULL COMMENT '源文件原始文本字段: ds'
)
DUPLICATE KEY(`_ingest_batch_id`)
DISTRIBUTED BY HASH(`_ingest_batch_id`) BUCKETS 1
PROPERTIES (
    "replication_allocation" = "tag.location.default: 1"
);
