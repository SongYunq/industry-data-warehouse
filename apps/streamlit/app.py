"""Streamlit 首版技术接入与原始数据连接检查页面（不暴露敏感凭据，不提供文件上传按钮）。"""

from __future__ import annotations

import json
import streamlit as st

from warehouse.config import load_settings
from warehouse.db import connect_doris, get_cluster_status

st.set_page_config(
    page_title="末端揽派数仓 - 初次构建技术接入检查",
    layout="wide",
)

settings = load_settings()

st.title("国内快递末端揽派数仓系统（LaDe）— 初次构建技术接入检查")
st.caption(
    "当前页面为初次构建阶段的技术连通与原始接入核验入口；正式业务分层指标与多维可视化将在后续阶段开展。"
)

col1, col2, col3 = st.columns(3)
with col1:
    st.metric("Doris 查询地址", f"{settings.doris_host}:{settings.doris_query_port}")
with col2:
    st.metric("目标原始接入表", f"{settings.doris_database}.{settings.doris_table}")
with col3:
    st.metric("默认接入文件标识", settings.lade_default_file)

st.subheader("1. Apache Doris 集群与连接状态")
try:
    cluster = get_cluster_status(settings)
    st.success(
        f"Doris 连接正常 | FE Alive: {cluster['fe_alive']} | BE Alive: {cluster['be_alive']}"
    )

    with connect_doris(settings, database=settings.doris_database) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT `_ingest_batch_id`, `_source_file`, `_source_sha256`, "
                f"MIN(`_ingested_at`) AS min_ingested_at, MAX(`_ingested_at`) AS max_ingested_at, "
                f"COUNT(*) AS row_count "
                f"FROM `{settings.doris_database}`.`{settings.doris_table}` "
                f"GROUP BY `_ingest_batch_id`, `_source_file`, `_source_sha256` "
                f"ORDER BY max_ingested_at DESC"
            )
            batch_rows = list(cur.fetchall())

            cur.execute(
                f"SELECT * FROM `{settings.doris_database}`.`{settings.doris_table}` LIMIT 20"
            )
            sample_rows = list(cur.fetchall())

    st.subheader("2. 已导入批次与元数据核验")
    if batch_rows:
        st.dataframe(batch_rows, use_container_width=True)
    else:
        st.info("当前接入表暂无批次记录。")

    st.subheader("3. 原始接入表抽样记录（前 20 条）")
    if sample_rows:
        st.dataframe(sample_rows, use_container_width=True)
    else:
        st.info("暂无抽样数据。")

except Exception as exc:  # noqa: BLE001
    st.error(f"连接或查询 Doris 失败: {exc}")

latest_check = settings.manifest_dir / "batches" / "latest_check.json"
if latest_check.exists():
    st.subheader("4. 最近一次本地核验清单 (latest_check.json)")
    st.json(json.loads(latest_check.read_text(encoding="utf-8")))
