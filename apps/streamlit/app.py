"""Inside Airbnb raw-file inventory page; business metrics remain out of scope."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import streamlit as st

from inside_airbnb.source import (
    DEFAULT_MANIFEST,
    inspect_entries,
    load_manifest,
    select_entries,
    verify_entries,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OVERVIEW_FILE = PROJECT_ROOT / "docs" / "附件-数据概况.csv"

st.set_page_config(page_title="Inside Airbnb 数据源盘点", layout="wide")
st.title("Inside Airbnb 三城数据源盘点")
st.caption("此页面核对原始文件与快照规模，不计算实际入住、订单、成交价格或收入指标。")

try:
    entries = load_manifest()
except Exception as exc:  # noqa: BLE001
    st.error(f"无法读取数据下载清单：{exc}")
    st.stop()

st.write(f"文件清单：`{DEFAULT_MANIFEST.relative_to(PROJECT_ROOT)}` · {len(entries)} 个文件")
city_names = {entry.city_key: entry.city for entry in entries}
city_keys = sorted(city_names)
latest_by_city = {
    city_key: max(entry.snapshot_date for entry in entries if entry.city_key == city_key)
    for city_key in city_keys
}

if OVERVIEW_FILE.is_file():
    with OVERVIEW_FILE.open("r", encoding="utf-8-sig", newline="") as stream:
        overview_rows = list(csv.DictReader(stream))
else:
    overview_rows = []

snapshot_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
for row in overview_rows:
    snapshot_rows[row["city"]].append(row)

columns = st.columns(len(city_keys))
for column, city_key in zip(columns, city_keys):
    city = city_names[city_key]
    snapshot = latest_by_city[city_key]
    latest_rows = [row for row in snapshot_rows[city] if row["snapshot_date"] == snapshot]
    listing_count = next((row["records"] for row in latest_rows if row["file"] == "listings.csv.gz"), "—")
    review_count = next((row["records"] for row in latest_rows if row["file"] == "reviews.csv.gz"), "—")
    calendar_count = next((row["records"] for row in latest_rows if row["file"] == "calendar.csv.gz"), "—")
    with column:
        st.subheader(city)
        st.caption(f"最新快照：{snapshot}")
        st.metric("详细房源记录", listing_count)
        st.metric("日历记录", calendar_count)
        st.metric("评论记录", review_count)

st.divider()
city_label = st.selectbox("查看城市", [city_names[key] for key in city_keys])
selected_key = next(key for key, label in city_names.items() if label == city_label)
snapshots = sorted({entry.snapshot_date for entry in entries if entry.city_key == selected_key}, reverse=True)
selected_snapshot = st.selectbox("查看快照", snapshots)
selected_entries = select_entries(entries, city=selected_key, snapshot=selected_snapshot)

st.subheader("所选快照文件")
st.dataframe(
    [
        {
            "文件": entry.file_name,
            "清单字节数": entry.size_bytes,
            "本地文件": "存在" if entry.local_path.is_file() else "缺失",
            "相对路径": entry.relative_path,
        }
        for entry in selected_entries
    ],
    width="stretch",
    hide_index=True,
)

with st.expander("文件结构盘点", expanded=False):
    if st.button("读取所选快照的文件结构"):
        with st.spinner("正在读取所选快照文件…"):
            try:
                st.dataframe(inspect_entries(selected_entries), width="stretch", hide_index=True)
            except Exception as exc:  # noqa: BLE001
                st.error(f"结构盘点失败：{exc}")

with st.expander("完整性校验", expanded=False):
    st.caption("完整校验会读取全部原始文件并计算 SHA-256；数据量约 1.09 GiB。")
    if st.button("校验三城全部 84 个文件"):
        with st.spinner("正在读取文件并核对 SHA-256…"):
            results = verify_entries(entries)
        failures = [row for row in results if row["status"] != "verified"]
        if failures:
            st.error(f"发现 {len(failures)} 个文件缺失或校验不匹配。")
            st.dataframe(failures, width="stretch", hide_index=True)
        else:
            st.success("84 个文件的大小与 SHA-256 均匹配下载清单。")

st.subheader("当前数据能力边界")
st.info(
    "日历是抓取时可见的未来可订状态，不能区分已预订和房东封锁；日历不含逐日价格。"
    "挂牌价与数据集估算字段都不能直接作为实际成交金额或真实入住率。业务指标需等 W04 明确后再设计。"
)
