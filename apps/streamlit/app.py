"""H&M purchase analysis backed by the local Doris warehouse."""

from __future__ import annotations

import calendar
import logging
from dataclasses import replace
from datetime import date, timedelta

import altair as alt
import pandas as pd
import pymysql
import streamlit as st

import queries as q
from hm_recommendations.labels import bilingual
from hm_recommendations.source import FILES, SOURCE_PAGE, verify_files

ACCENT = "#a51c3d"
CHANNEL_COLORS = ["#a51c3d", "#4e7082"]
PURCHASE_COLUMNS = {"items": "购买件数", "buyers": "买家数", "share": "购买占比"}
CHANNEL_NAMES = {1: "线下", 2: "线上"}
CHANNEL_LABELS = {code: f"{name}（{code}）" for code, name in CHANNEL_NAMES.items()}
SOURCE_FILE_LABELS = {
    "articles.csv": "商品目录",
    "customers.csv": "客户属性",
    "transactions_train.csv": "训练期购买明细",
}
SOURCE_STATUS_LABELS = {
    "verified": "校验通过",
    "missing": "文件缺失",
    "manifest_missing": "缺少校验清单",
    "size_mismatch": "字节数不匹配",
    "size_matches": "字节数匹配",
    "sha256_mismatch": "SHA-256 不匹配",
}

st.set_page_config(page_title="H&M 购买分析", layout="wide")


def number(value: object) -> str:
    return "—" if pd.isna(value) else f"{int(value):,}"


def percent(value: object) -> str:
    return "—" if pd.isna(value) else f"{float(value):.2%}"


def article_code(value: object) -> str:
    return str(int(value)).zfill(10) if str(value).isdecimal() else str(value)


def bilingual_columns(data: pd.DataFrame, **fields: str) -> pd.DataFrame:
    """Translate display values without modifying the raw query results."""
    return data.assign(**{
        column: data[column].map(lambda value: bilingual(value, field))
        for column, field in fields.items()
    })


def display_table(data: pd.DataFrame, columns: dict[str, str], rates: tuple[str, ...] = ()) -> None:
    formatted = data.loc[:, columns.keys()].rename(columns=columns).copy()
    for rate in rates:
        formatted[columns[rate]] = formatted[columns[rate]].astype(float)
    config = {columns[rate]: st.column_config.NumberColumn(format="percent") for rate in rates}
    st.dataframe(formatted, width="stretch", hide_index=True, column_config=config)


def no_results(data: pd.DataFrame) -> bool:
    if data.empty:
        st.info("当前筛选范围没有可展示的记录。")
    return data.empty


def coverage_label(value: date, scope: q.Scope) -> str:
    first = value.replace(day=1)
    last = value.replace(day=calendar.monthrange(value.year, value.month)[1])
    return "完整月" if scope.start <= first and scope.end >= last else "部分月"


def show_chart(chart: alt.Chart, height: int = 300) -> None:
    st.altair_chart(
        chart.properties(height=height)
        .configure_view(stroke=None)
        .configure_axis(gridColor="#e4e7ec", gridOpacity=0.6),
        width="stretch",
    )


def bars(data: pd.DataFrame, field: str, field_label: str, limit: int = 15) -> None:
    if no_results(data):
        return
    chart = alt.Chart(data.head(limit)).mark_bar(color=ACCENT, cornerRadiusEnd=3).encode(
        x=alt.X("items:Q", title="购买件数"),
        y=alt.Y(f"{field}:N", title=field_label, sort="-x", axis=alt.Axis(labelLimit=230)),
        tooltip=[
            alt.Tooltip(f"{field}:N", title=field_label),
            alt.Tooltip("items:Q", title="购买件数", format=","),
            alt.Tooltip("buyers:Q", title="买家数", format=","),
            alt.Tooltip("share:Q", title="购买占比", format=".2%"),
        ],
    )
    show_chart(chart, min(360, max(90, 22 * min(limit, len(data)))))


def trend(data: pd.DataFrame, scope: q.Scope, columns: dict[str, str]) -> None:
    if no_results(data):
        return
    monthly = data.copy()
    monthly["coverage"] = monthly["month"].map(lambda value: coverage_label(value, scope))
    monthly = monthly.melt(id_vars=["month", "coverage"], value_vars=list(columns), var_name="series", value_name="value")
    monthly["series"] = monthly["series"].map(columns)
    monthly["value"] = monthly["value"].astype(float)
    chart = alt.Chart(monthly).mark_line(point=True, strokeWidth=2).encode(
        x=alt.X("month:T", title="月份", axis=alt.Axis(format="%Y-%m", labelOverlap=True)),
        y=alt.Y("value:Q", title=None, axis=alt.Axis(format="~s")),
        color=alt.Color("series:N", title=None, scale=alt.Scale(range=CHANNEL_COLORS)),
        tooltip=[
            alt.Tooltip("month:T", title="月份", format="%Y-%m"),
            alt.Tooltip("coverage:N", title="覆盖"),
            alt.Tooltip("series:N", title="指标"),
            alt.Tooltip("value:Q", title="数值", format=","),
        ],
    )
    show_chart(chart, 235)
    if (monthly["coverage"] == "部分月").any():
        partial = monthly.loc[monthly["coverage"] == "部分月", "month"].drop_duplicates()
        st.caption(f"部分月：{'、'.join(f'{value:%Y-%m}' for value in partial)}。趋势图显示实际覆盖天数内的观测量。")


def sidebar_scope(*, products: bool, channels: bool, repurchase: bool) -> q.Scope:
    scope = st.session_state.get("purchase_scope", q.Scope(q.START, q.CUTOFF))
    with st.sidebar.form("purchase_filters", border=False):
        dates = st.date_input(
            "起始购买日范围" if repurchase else "购买日期",
            value=(scope.start, scope.end), min_value=q.START, max_value=q.CUTOFF, key="purchase_dates",
        )
        channel = scope.channel
        if channels:
            channel = st.selectbox(
                "渠道", [None, *CHANNEL_NAMES], index=scope.channel or 0, key="purchase_channel",
                format_func=lambda value: "全部" if value is None else CHANNEL_LABELS[value],
            )
        product_type = scope.product_type
        if products:
            options = [None, *q.product_types()]
            product_type = st.selectbox(
                "商品类型", options, index=options.index(product_type) if product_type in options else 0,
                key="purchase_product_type",
                format_func=lambda value: "全部" if value is None else bilingual(value, "product_type_name"),
            )
        if st.form_submit_button("应用筛选", type="primary", width="stretch"):
            if len(dates) != 2:
                st.warning("请选择起止日期。")
            else:
                scope = q.Scope(*dates, product_type, channel)
                st.session_state.purchase_scope = scope
    return replace(
        scope, product_type=scope.product_type if products else None, channel=scope.channel if channels else None,
    )


def show_scope(scope: q.Scope) -> None:
    terms = [f"{scope.start:%Y-%m-%d} 至 {scope.end:%Y-%m-%d}"]
    if scope.channel is not None:
        terms.append(CHANNEL_LABELS[scope.channel])
    if scope.product_type is not None:
        terms.append(bilingual(scope.product_type, "product_type_name"))
    st.caption(" · ".join(terms))


def show_overlap(data: pd.DataFrame) -> None:
    if no_results(data):
        return
    data = data.copy()
    data["share"] = data["customer_count"] / data["customer_count"].sum()
    data["channel_group"] = data["channel_group"].map({"1": "只在线下买过", "2": "只在线上买过", "1+2": "线上线下都买过"})
    display_table(
        data,
        {"channel_group": "购买渠道", "customer_count": "客户数", "item_count": "购买件数", "share": "客户占比"},
        ("share",),
    )


def overview(scope: q.Scope) -> None:
    with st.spinner("正在汇总购买记录…"):
        totals = q.totals(scope)
    labels = {"items": "购买件数", "buyers": "购买客户数", "families": "买过的款式数", "variants": "买过的商品数"}
    with st.container(horizontal=True, horizontal_alignment="distribute"):
        for key, title in labels.items():
            st.metric(title, number(totals[key]), border=True, width=216)
    if not totals["items"]:
        st.info("当前范围没有购买记录。")
        return
    st.subheader("每月购买")
    metric = st.radio("趋势指标", ("items", "buyers"), format_func=PURCHASE_COLUMNS.get, horizontal=True)
    trend(q.monthly(scope), scope, {metric: PURCHASE_COLUMNS[metric]})
    st.subheader("购买最多的商品组")
    grouped = bilingual_columns(q.group_products(scope, "product_group_name"), category="product_group_name")
    bars(grouped, "category", "商品组", 10)


def families(scope: q.Scope) -> None:
    rank_key = st.radio("按什么排名", tuple(q.RANK_KEYS), format_func=q.RANK_KEYS.get, horizontal=True)
    ranked = q.rank_products(scope, rank_key).copy()
    if no_results(ranked):
        return
    ranked["code"] = ranked["code"].map(article_code if rank_key == "article_id" else lambda code: str(int(code)))
    with st.container(horizontal=True):
        st.metric("前 10 名购买占比", percent(ranked.head(10)["share"].sum()), border=True)
        st.metric("前 20 名购买占比", percent(ranked["share"].sum()), border=True)
    bars(ranked, "code", q.RANK_KEYS[rank_key], 15)
    display_table(ranked, {"code": q.RANK_KEYS[rank_key], **PURCHASE_COLUMNS}, ("share",))
    if rank_key != "product_code":
        return
    st.caption("这里按款式编号归组，同一编号下可以有多个商品。")
    code = st.selectbox("查看某款的商品明细", ranked["code"], index=None, placeholder="选择款式编号")
    if code is not None:
        family_drilldown(scope, int(code))


def family_drilldown(scope: q.Scope, code: int) -> None:
    st.subheader(f"款式 {code} 的商品明细")
    catalog = q.family_catalogue(code)
    filtered = q.family_variants(scope, code)
    variants = catalog.merge(filtered, how="left", on="article_id")
    variants[["items", "buyers"]] = variants[["items", "buyers"]].fillna(0).astype(int)
    variants["share"] = variants["items"] / variants["items"].sum()
    variants["article_id"] = variants["article_id"].map(article_code)
    covered = int((variants["training_items"] > 0).sum())
    st.caption(f"目录有 {len(variants):,} 个商品编号，全期有购买记录的有 {covered:,} 个。")
    st.metric("当前范围内购买件数", number(variants["items"].sum()))
    groups = catalog["product_group_name"].nunique()
    types = catalog["product_type_name"].nunique()
    if groups > 1 or types > 1:
        st.warning(f"该款式编号下有 {groups} 个商品组、{types} 种商品类型。")
    display_table(
        bilingual_columns(
            variants.sort_values(["items", "article_id"], ascending=[False, True]),
            prod_name="prod_name", product_group_name="product_group_name", product_type_name="product_type_name",
            colour_group_name="colour_group_name", graphical_appearance_name="graphical_appearance_name",
        ),
        {
            "article_id": "商品编号", "prod_name": "商品名", "product_group_name": "商品组",
            "product_type_name": "商品类型", "colour_group_name": "颜色组",
            "graphical_appearance_name": "图案", **PURCHASE_COLUMNS,
        },
        ("share",),
    )
    if catalog["colour_group_name"].nunique() > 1 and variants["items"].sum():
        colors = variants.groupby("colour_group_name", as_index=False)["items"].sum().sort_values("items", ascending=False)
        colors["share"] = colors["items"] / colors["items"].sum()
        display_table(
            bilingual_columns(colors, colour_group_name="colour_group_name"),
            {"colour_group_name": "颜色", "items": "购买件数", "share": "该款购买占比"},
            ("share",),
        )


def product_hierarchy(scope: q.Scope) -> None:
    dimensions = q.PRODUCT_LEVELS
    field = st.selectbox("按什么分类", tuple(dimensions), format_func=dimensions.get)
    products = bilingual_columns(q.group_products(scope, field), category=field)
    if no_results(products):
        return
    bars(products, "category", dimensions[field])
    display_table(products, {"category": dimensions[field], **PURCHASE_COLUMNS}, ("share",))


def appearance_page(scope: q.Scope) -> None:
    field = st.radio("按颜色或图案查看", tuple(q.APPEARANCES), format_func=q.APPEARANCES.get, horizontal=True)
    products = bilingual_columns(q.group_products(scope, field), category=field)
    if no_results(products):
        return
    bars(products, "category", q.APPEARANCES[field])
    display_table(products, {"category": q.APPEARANCES[field], **PURCHASE_COLUMNS}, ("share",))


def description_terms(scope: q.Scope) -> None:
    search = st.text_input("查找描述词", placeholder="例如 high waist").strip()
    with st.spinner("正在统计商品描述词…"):
        ranking = q.description_terms(scope)
    if no_results(ranking):
        return
    st.caption("购买件数按当前筛选统计。一件商品的描述可包含多个词，件数可能重复计入。")
    shown = ranking.loc[ranking["term"].str.contains(search, case=False, regex=False)] if search else ranking
    if no_results(shown):
        return
    top = shown.loc[shown["items"] > 0].head(15)
    if not top.empty:
        chart = alt.Chart(top).mark_bar(color=ACCENT, cornerRadiusEnd=3).encode(
            x=alt.X("items:Q", title="购买件数"),
            y=alt.Y("term:N", title="描述词", sort=top["term"].tolist(), axis=alt.Axis(labelLimit=230)),
            tooltip=[
                alt.Tooltip("term:N", title="描述词"),
                alt.Tooltip("items:Q", title="购买件数", format=","),
                alt.Tooltip("variants:Q", title="目录中的商品数", format=","),
            ],
        )
        show_chart(chart, max(90, 22 * len(top)))
    display_table(shown, {"term_rank": "排名", "term": "描述词", "items": "购买件数", "variants": "目录中的商品数"})


def customer_repurchase(scope: q.Scope) -> None:
    st.caption(
        f"筛选的是第 1、2 或 3 次购买发生的日期。后续购买查到 {q.CUTOFF:%Y-%m-%d}；同一天买多件算一次购买。"
        "首次购买指数据中的第一笔记录所在日期。"
    )
    with st.spinner("正在统计复购…"):
        data = q.repurchase(scope)
    data["repeat_rate"] = data["repeat_customers"] / data["eligible_customers"]
    data["purchase"] = data["purchase_day_no"].map(lambda value: f"第 {value} 次购买后")
    columns = {
        "purchase": "从哪次购买算起",
        "eligible_customers": "纳入统计的客户",
        "repeat_customers": "有复购的客户",
        "repeat_rate": "复购率",
    }

    st.subheader("截至数据结束的复购")
    st.caption("只要后来有一天再次购买，就记为复购。购买日期越晚，能看到的后续时间越短。")
    display_table(data.loc[data["horizon_days"] == 0], columns, ("repeat_rate",))

    st.subheader("30、60、90 天内复购")
    last_dates = {days: q.CUTOFF - timedelta(days=days) for days in q.REPURCHASE_WINDOWS}
    fixed = data.loc[data["horizon_days"] > 0].assign(
        window=lambda rows: rows["horizon_days"].map(lambda days: f"{days} 天内"),
        last_date=lambda rows: rows["horizon_days"].map(lambda days: last_dates[days].isoformat()),
    )
    st.caption("只统计购买日期在对应截止日当天或之前的客户。没有符合条件的客户时，复购率留空。")
    display_table(
        fixed,
        {
            "purchase": columns["purchase"], "window": "期限", "last_date": "最晚计入的购买日",
            "eligible_customers": columns["eligible_customers"],
            "repeat_customers": columns["repeat_customers"], "repeat_rate": columns["repeat_rate"],
        },
        ("repeat_rate",),
    )


def channel_mix(scope: q.Scope) -> None:
    monthly = q.channel_months(scope)
    if no_results(monthly):
        return
    monthly["channel"] = monthly["channel"].astype(int).map(CHANNEL_LABELS)
    monthly["coverage"] = monthly["month"].map(lambda value: coverage_label(value, scope))
    chart = alt.Chart(monthly).mark_line(point=True, strokeWidth=2).encode(
        x=alt.X("month:T", title="月份", axis=alt.Axis(format="%Y-%m", labelOverlap=True)),
        y=alt.Y("items:Q", title="购买件数"),
        color=alt.Color("channel:N", title="渠道", scale=alt.Scale(domain=list(CHANNEL_LABELS.values()), range=CHANNEL_COLORS)),
        tooltip=[
            alt.Tooltip("month:T", title="月份", format="%Y-%m"),
            alt.Tooltip("coverage:N", title="覆盖"),
            alt.Tooltip("channel:N", title="渠道"),
            alt.Tooltip("items:Q", title="购买件数", format=","),
            alt.Tooltip("buyers:Q", title="买家数", format=","),
        ],
    )
    st.subheader("每月购买件数")
    show_chart(chart, 245)
    if (monthly["coverage"] == "部分月").any():
        partial = monthly.loc[monthly["coverage"] == "部分月", "month"].drop_duplicates()
        st.caption(f"部分月：{'、'.join(f'{value:%Y-%m}' for value in partial)}。")
    st.subheader("客户在哪些渠道买过")
    show_overlap(q.overlap(scope))
    if scope.product_type is None:
        st.subheader("各渠道购买最多的商品组")
        field = "product_group_name"
        shown = bilingual_columns(q.channel_composition(scope, field), category=field)
        shown["channel"] = shown["channel"].map(CHANNEL_LABELS)
        display_table(shown, {"channel": "渠道", "category": "商品组", **PURCHASE_COLUMNS}, ("share",))
        st.caption("购买占比按各渠道的购买总件数计算。")


def intake() -> None:
    verify = st.button("核验三张表的字节数与 SHA-256", width="stretch")
    st.markdown(f"下载入口为 [Kaggle 官方竞赛数据页]({SOURCE_PAGE})。请在下载前登录并接受竞赛规则。")
    results = verify_files(digest=False)
    present = sum(item["status"] in {"size_matches", "manifest_missing"} for item in results)
    st.metric("本地存在文件", f"{present} / {len(FILES)}")
    st.dataframe(
        [
            {
                "文件": f"{item['file_name']}（{SOURCE_FILE_LABELS[item['file_name']]}）",
                "状态": f"{item['status']}（{SOURCE_STATUS_LABELS[item['status']]}）",
                "清单字节": item["expected_bytes"],
                "实际字节": item["actual_bytes"],
                "路径": item["relative_path"],
            }
            for item in results
        ],
        width="stretch",
        hide_index=True,
    )
    if verify:
        with st.spinner("正在读取原始文件并计算 SHA-256…"):
            results = verify_files()
        st.dataframe(
            [
                {
                    "文件": f"{item['file_name']}（{SOURCE_FILE_LABELS[item['file_name']]}）",
                    "校验状态": f"{item['status']}（{SOURCE_STATUS_LABELS[item['status']]}）",
                    "实际 SHA-256": item["actual_sha256"] or "—",
                }
                for item in results
            ],
            width="stretch",
            hide_index=True,
        )
        if all(item["status"] == "verified" for item in results):
            st.success("三张核心 CSV 的字节数与 SHA-256 均和本地清单一致。")
        else:
            st.error("至少一个文件缺失，或与本地清单的摘要不一致。")
    elif present != len(FILES):
        st.info("接受 Kaggle 竞赛规则并配置身份验证后，运行 `pixi run data-fetch` 下载文件。")


BOARDS = {
    "购买概览": overview,
    "商品分类": product_hierarchy,
    "款式排行": families,
    "颜色与图案": appearance_page,
    "商品描述词": description_terms,
    "客户复购": customer_repurchase,
    "线上与线下": channel_mix,
}

st.sidebar.title("H&M 购买分析")
page = st.sidebar.radio("页面", [*BOARDS, "文件核验"], label_visibility="collapsed")
st.title(page)
if page == "文件核验":
    intake()
else:
    try:
        if not q.warehouse_ready():
            st.warning("Doris 中的 H&M 数据尚未完整发布。请按部署文档完成数据加载。")
            if st.sidebar.button("重新检查数据"):
                q.read.clear()
                st.rerun()
            st.stop()
        is_repurchase = page == "客户复购"
        scope = sidebar_scope(
            products=not is_repurchase, channels=page not in {"客户复购", "线上与线下"}, repurchase=is_repurchase,
        )
        show_scope(scope)
        BOARDS[page](scope)
    except pymysql.MySQLError as exc:
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        logging.getLogger(__name__).warning("Doris dashboard query failed, code=%s", code)
        if code in {1049, 1146}:
            st.warning("分析库或视图尚未就绪。请按部署文档完成 H&M 数据加载和分析视图初始化。")
        elif "MEM_LIMIT_EXCEEDED" in str(exc):
            st.warning("Doris 当前可用内存不足。请等待其他查询结束后重试。")
        else:
            st.error("暂时无法取得 Doris 查询结果。请检查项目服务状态后重试。")
        if st.sidebar.button("重试 Doris 查询"):
            q.read.clear()
            st.rerun()
