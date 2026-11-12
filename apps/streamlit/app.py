"""H&M 数据下载和原始文件检查页；不展示业务分析指标。"""

from __future__ import annotations

import streamlit as st

from hm_recommendations.source import FILES, SOURCE_PAGE, verify_files

st.set_page_config(
    page_title="H&M 数据接入检查",
    page_icon="🧵",
    layout="wide",
)

st.title("H&M 数据接入检查")
st.caption("当前页面只检查 W03 原始文件，不代表 Doris 入库、W04 需求或业务看板已经完成。")
st.markdown(f"数据须从 [Kaggle 官方竞赛数据页]({SOURCE_PAGE}) 获取；下载前需在该页面登录并接受竞赛规则。")

results = verify_files(digest=False)
present = sum(item["status"] in {"size_matches", "manifest_missing"} for item in results)
left, right = st.columns(2)
left.metric("本地存在文件", f"{present} / {len(FILES)}")
right.metric("原始文件目录", "data/raw/hm_recommendations/")

st.subheader("文件状态")
st.dataframe(
    [
        {
            "文件": item["file_name"],
            "状态": item["status"],
            "清单字节": item["expected_bytes"] or "—",
            "实际字节": item["actual_bytes"] or "—",
            "路径": item["relative_path"],
        }
        for item in results
    ],
    use_container_width=True,
    hide_index=True,
)

if st.button("核验三张表的字节数与 SHA-256"):
    results = verify_files()
    verified = sum(item["status"] == "verified" for item in results)
    st.dataframe(
        [{"文件": item["file_name"], "校验状态": item["status"], "实际 SHA-256": item["actual_sha256"] or "—"}
         for item in results],
        use_container_width=True,
        hide_index=True,
    )
    if verified == len(FILES):
        st.success("三张核心 CSV 的字节数与 SHA-256 均和本地清单一致。")
    else:
        st.error("至少一个文件缺失，或与本地清单的摘要不一致。")
elif present != len(FILES):
    st.info("完成 Kaggle 规则确认和 CLI 身份验证后，运行 `pixi run data-fetch` 下载文件。")
else:
    st.info("文件均存在。点击上方按钮后才会逐个读取完整文件并计算 SHA-256。")

st.caption("W04 业务问题、使用角色和指标口径仍待确定。")
