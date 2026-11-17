-- Run as a Doris administrator after analytics-install
-- and after creating the dedicated user for the Metabase container.
-- Grants stay on the v_* read views. Do not grant the customer-level DWS tables.
GRANT SELECT_PRIV ON internal.hm_retail_dw.v_monthly_overview TO 'hm_metabase'@'172.30.0.13';
GRANT SELECT_PRIV ON internal.hm_retail_dw.v_monthly_product_group TO 'hm_metabase'@'172.30.0.13';
GRANT SELECT_PRIV ON internal.hm_retail_dw.v_monthly_colour_group TO 'hm_metabase'@'172.30.0.13';
GRANT SELECT_PRIV ON internal.hm_retail_dw.v_monthly_channel TO 'hm_metabase'@'172.30.0.13';
GRANT SELECT_PRIV ON internal.hm_retail_dw.v_repurchase_stage TO 'hm_metabase'@'172.30.0.13';
GRANT SELECT_PRIV ON internal.hm_retail_dw.v_first_month_repurchase TO 'hm_metabase'@'172.30.0.13';
GRANT SELECT_PRIV ON internal.hm_retail_dw.v_customer_frequency_recency TO 'hm_metabase'@'172.30.0.13';
GRANT SELECT_PRIV ON internal.hm_retail_dw.v_channel_overlap TO 'hm_metabase'@'172.30.0.13';
GRANT SELECT_PRIV ON internal.hm_retail_dw.v_channel_switch TO 'hm_metabase'@'172.30.0.13';
GRANT SELECT_PRIV ON internal.hm_retail_dw.v_channel_day_profile TO 'hm_metabase'@'172.30.0.13';
GRANT SELECT_PRIV ON internal.hm_retail_dw.v_category_journey TO 'hm_metabase'@'172.30.0.13';
GRANT SELECT_PRIV ON internal.hm_retail_dw.v_display_labels TO 'hm_metabase'@'172.30.0.13';

CREATE WORKLOAD GROUP IF NOT EXISTS hm_dashboard PROPERTIES (
    "max_concurrency" = "1",
    "max_queue_size" = "20",
    "queue_timeout" = "120000"
);
GRANT USAGE_PRIV ON WORKLOAD GROUP 'hm_dashboard' TO 'hm_metabase'@'172.30.0.13';
SET PROPERTY FOR 'hm_metabase' 'default_workload_group' = 'hm_dashboard';
