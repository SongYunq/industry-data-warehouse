"""H&M purchase analysis backed by the local Doris warehouse."""

from __future__ import annotations

import calendar
import logging
from dataclasses import replace
from datetime import date

import altair as alt
import pandas as pd
import pymysql
import streamlit as st

import queries as q
from hm_recommendations.source import FILES, SOURCE_PAGE, verify_files

ACCENT = "#a51c3d"
CHANNEL_COLORS = ["#a51c3d", "#4e7082"]
PURCHASE_COLUMNS = {"items": "购买件数", "buyers": "买家数", "share": "购买占比"}
STAGE_COLUMNS = {
    "purchase_day_no": "观察购买日序号",
    "horizon_days": "窗口天数",
    "eligible_customers": "合格客户数",
    "repeat_customers": "再次购买客户数",
    "no_repeat_customers": "未观察到再次购买",
    "repeat_rate": "再次购买日率",
    "no_repeat_rate": "未观察到再次购买比例",
    "gap_p25": "间隔 P25（天）",
    "gap_median": "间隔中位数（天）",
    "gap_p75": "间隔 P75（天）",
}
PAGES = ["经营概览", "商品组合", "客户回购", "渠道旅程", "文件核验"]

st.set_page_config(page_title="H&M 购买分析", layout="wide")


def number(value: object) -> str:
    return "—" if pd.isna(value) else f"{int(value):,}"


def percent(value: object) -> str:
    return "—" if pd.isna(value) else f"{float(value):.2%}"


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def article_code(value: object) -> str:
    return str(int(value)).zfill(10) if str(value).isdecimal() else str(value)


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
        y=alt.Y(f"{field}:N", title=field_label, sort="-x", axis=alt.Axis(labelLimit=175)),
        tooltip=[
            alt.Tooltip(f"{field}:N", title=field_label),
            alt.Tooltip("items:Q", title="购买件数", format=","),
            alt.Tooltip("buyers:Q", title="买家数", format=","),
            alt.Tooltip("share:Q", title="购买占比", format=".2%"),
        ],
    )
    show_chart(chart, min(360, max(90, 22 * min(limit, len(data)))))


def trend(data: pd.DataFrame, scope: q.Scope, columns: dict[str, str], *, shares: bool = False) -> None:
    if no_results(data):
        return
    monthly = data.copy()
    monthly["coverage"] = monthly["month"].map(lambda value: coverage_label(value, scope))
    monthly = monthly.melt(id_vars=["month", "coverage"], value_vars=list(columns), var_name="series", value_name="value")
    monthly["series"] = monthly["series"].map(columns)
    monthly["value"] = monthly["value"].astype(float)
    value_format = ".2%" if shares else ","
    axis_format = ".0%" if shares else "~s"
    chart = alt.Chart(monthly).mark_line(point=True, strokeWidth=2).encode(
        x=alt.X("month:T", title="月份", axis=alt.Axis(format="%Y-%m", labelOverlap=True)),
        y=alt.Y("value:Q", title=None, axis=alt.Axis(format=axis_format)),
        color=alt.Color("series:N", title=None, scale=alt.Scale(range=CHANNEL_COLORS)),
        tooltip=[
            alt.Tooltip("month:T", title="月份", format="%Y-%m"),
            alt.Tooltip("coverage:N", title="覆盖"),
            alt.Tooltip("series:N", title="指标"),
            alt.Tooltip("value:Q", title="数值", format=value_format),
        ],
    )
    show_chart(chart, 235)
    if (monthly["coverage"] == "部分月").any():
        partial = monthly.loc[monthly["coverage"] == "部分月", "month"].drop_duplicates()
        st.caption(f"部分月：{'、'.join(f'{value:%Y-%m}' for value in partial)}。趋势图显示实际覆盖天数内的观测量。")


def filter_box(field: str, title: str, current: str | None) -> str | None:
    options = [None, *q.options(field)]
    index = options.index(current) if current in options else 0
    return st.selectbox(title, options, index=index, key=f"filter_{field}", format_func=lambda value: "全部" if value is None else value)


def sidebar_scope() -> q.Scope:
    scope = st.session_state.get("scope", q.Scope(q.START, q.CUTOFF))
    product_values = dict(scope.product)
    customer_values = dict(scope.customer)
    quick_product_fields = ("product_group_name", "product_type_name", "colour_group_name", "graphical_appearance_name")
    advanced_fields = {field: title for field, title in (q.PRODUCT_LEVELS | q.APPEARANCES).items() if field not in quick_product_fields}
    saved_field = next((field for field in product_values if field in advanced_fields), None)
    with st.sidebar:
        st.header("筛选范围")
        with st.expander("更多商品维度"):
            extra_field = st.selectbox(
                "附加商品维度", [None, *advanced_fields],
                index=[None, *advanced_fields].index(saved_field),
                format_func=lambda field: advanced_fields[field] if field else "无",
            )
        with st.form("dashboard_filters"):
            dates = st.date_input("观察日期", value=(scope.start, scope.end), min_value=q.START, max_value=q.CUTOFF)
            channel = st.selectbox("渠道编码", [None, 1, 2], index=scope.channel or 0, format_func=lambda value: "全部" if value is None else str(value))
            with st.expander("商品筛选"):
                product = tuple(
                    (field, value)
                    for field, value in (
                        (field, filter_box(field, (q.PRODUCT_LEVELS | q.APPEARANCES)[field], product_values.get(field)))
                        for field in quick_product_fields
                    )
                    if value is not None
                )
                if extra_field is not None:
                    extra_value = filter_box(extra_field, advanced_fields[extra_field], product_values.get(extra_field))
                    if extra_value is not None:
                        product += ((extra_field, extra_value),)
                family = st.text_input("款式族候选编码", value=product_values.get("product_code", ""), max_chars=10).strip()
                variant = st.text_input("具体变体编码", value=product_values.get("article_id", ""), max_chars=10).strip()
            with st.expander("客户筛选"):
                customer = tuple(
                    (field, value)
                    for field, value in (
                        (field, filter_box(field, title, customer_values.get(field)))
                        for field, title in (
                            ("age_band", "年龄段"),
                            ("club_member_status", "会员状态"),
                            ("fashion_news_frequency", "资讯频率"),
                        )
                    )
                    if value is not None
                )
            submitted = st.form_submit_button("应用筛选", type="primary", width="stretch")
        if submitted:
            if len(dates) != 2:
                st.warning("请选择起止两个日期。")
            elif any(value and not (value.isascii() and value.isdecimal()) for value in (family, variant)):
                st.warning("商品编码应使用数字。")
            else:
                codes = tuple(
                    (field, value)
                    for field, value in (("product_code", family), ("article_id", variant))
                    if value
                )
                scope = q.Scope(*dates, product + codes, channel, customer)
                st.session_state.scope = scope
    return scope


def show_scope(scope: q.Scope, *, purchases: bool = True) -> None:
    terms = [f"{scope.start:%Y-%m-%d} 至 {scope.end:%Y-%m-%d}"]
    if purchases:
        for field, value in scope.product:
            title = (q.PRODUCT_LEVELS | q.APPEARANCES | q.RANK_KEYS)[field]
            terms.append(f"{title} {value}")
        if scope.channel is not None:
            terms.append(f"渠道编码 {scope.channel}")
    for field, value in scope.customer:
        terms.append(f"{q.CUSTOMER_GROUPS[field]} {value}")
    st.caption(" · ".join(terms))


def show_overlap(data: pd.DataFrame) -> None:
    if no_results(data):
        return
    data = data.copy()
    data["share"] = data["customer_count"] / data["customer_count"].sum()
    data["channel_group"] = data["channel_group"].map({"1": "仅编码 1", "2": "仅编码 2", "1+2": "编码 1 和 2"})
    display_table(
        data,
        {"channel_group": "客户编码构成", "customer_count": "客户数", "item_count": "购买件数", "share": "客户占比"},
        ("share",),
    )


def overview(scope: q.Scope) -> None:
    st.header("经营概览")
    show_scope(scope)
    with st.spinner("正在汇总购买记录…"):
        totals = q.totals(scope)
    labels = {"items": "观察购买件数", "buyers": "活跃购买客户", "families": "已购买款式族候选", "variants": "已购买具体变体"}
    for column, (key, title) in zip(st.columns(4), labels.items()):
        column.metric(title, number(totals[key]))
    if not totals["items"]:
        st.info("当前筛选范围没有购买记录。")
        return
    left, right = st.columns(2)
    with left:
        st.subheader("月度购买")
        metric = st.radio("趋势指标", ("items", "buyers"), format_func=PURCHASE_COLUMNS.get, horizontal=True)
        trend(q.monthly(scope), scope, {metric: PURCHASE_COLUMNS[metric]})
    with right:
        st.subheader("品类构成")
        dimension = st.selectbox(
            "经营分组", ("product_group_name", "product_type_name", "section_name"), format_func=q.PRODUCT_LEVELS.get,
        )
        grouped = q.group_products(scope, dimension)
        bars(grouped, "category", q.PRODUCT_LEVELS[dimension], 10)
        st.caption("图表显示件数最多的 10 个分类。购买占比以当前筛选范围的件数为分母。")
        with st.expander("查看全部分类"):
            display_table(grouped, {"category": q.PRODUCT_LEVELS[dimension], **PURCHASE_COLUMNS}, ("share",))
    if st.toggle("查看客户回购与多编码比例"):
        with st.spinner("正在汇总客户历史…"):
            stage = q.stage(scope)
            first_30 = stage.loc[(stage["purchase_day_no"] == 1) & (stage["horizon_days"] == 30)]
            overlap = q.overlap(replace(scope, channel=None))
        customer_count = int(overlap["customer_count"].sum())
        multichannel = int(overlap.loc[overlap["channel_group"] == "1+2", "customer_count"].sum())
        c1, c2 = st.columns(2)
        c1.metric(
            "首个观察购买日后 30 日再次购买日率",
            percent(first_30["repeat_rate"].iloc[0] if len(first_30) else None),
            help="日期筛选限定首个购买日。完整观察窗按源数据截止日判断，使用客户属性筛选。",
        )
        c2.metric("多编码购买客户占比", percent(ratio(multichannel, customer_count)), help="使用当前商品、日期与客户筛选，保留两个编码。")
        c1.caption(f"合格客户数 {number(first_30['eligible_customers'].iloc[0] if len(first_30) else 0)} · 数据截止日 {q.CUTOFF:%Y-%m-%d}")
        c2.caption(f"多编码客户 {number(multichannel)} / 购买客户 {number(customer_count)}")


def families(scope: q.Scope) -> None:
    st.subheader("头部商品集中度")
    rank_key = st.radio("排名口径", tuple(q.RANK_KEYS), format_func=q.RANK_KEYS.get, horizontal=True)
    ranked = q.rank_products(scope, rank_key).copy()
    if no_results(ranked):
        return
    ranked["code"] = ranked["code"].map(article_code if rank_key == "article_id" else lambda code: str(int(code)))
    left, right = st.columns(2)
    left.metric("前 10 名购买占比", percent(ranked.head(10)["share"].sum()))
    right.metric("前 20 名购买占比", percent(ranked["share"].sum()))
    st.caption("分母为当前筛选范围的观察购买件数。")
    bars(ranked, "code", q.RANK_KEYS[rank_key], 15)
    display_table(ranked, {"code": q.RANK_KEYS[rank_key], **PURCHASE_COLUMNS}, ("share",))
    if st.toggle("查看逐月集中度"):
        trend(q.monthly_concentration(scope, rank_key), scope, {"top10": "前 10 名", "top20": "前 20 名"}, shares=True)
    if rank_key != "product_code":
        return
    code = st.selectbox("选择款式族候选下钻", [None, *ranked["code"]], format_func=lambda code: "请选择款式族候选" if code is None else code)
    if code is not None:
        family_drilldown(scope, int(code))


def family_drilldown(scope: q.Scope, code: int) -> None:
    catalog = q.family_catalogue(code)
    filtered = q.family_variants(scope, code)
    variants = catalog.merge(filtered, how="left", on="article_id")
    variants[["items", "buyers"]] = variants[["items", "buyers"]].fillna(0).astype(int)
    variants["share"] = variants["items"] / variants["items"].sum()
    variants["article_id"] = variants["article_id"].map(article_code)
    variants["history"] = variants["training_items"].map(lambda value: "有训练期购买记录" if value else "训练期无购买记录")
    covered = int((variants["training_items"] > 0).sum())
    left, right = st.columns(2)
    left.metric("训练期目录变体购买覆盖", percent(ratio(covered, len(variants))))
    right.metric("当前范围族内购买件数", number(variants["items"].sum()))
    st.caption(f"目录覆盖分子 {covered:,} 个变体，目录分母 {len(variants):,} 个变体。变体购买占比使用当前筛选。")
    groups = catalog["product_group_name"].nunique()
    types = catalog["product_type_name"].nunique()
    if groups > 1 or types > 1:
        st.warning(f"候选款式族有分类冲突，目录包含 {groups} 个商品组和 {types} 种商品类型。")
    display_table(
        variants.sort_values(["items", "article_id"], ascending=[False, True]),
        {
            "article_id": "具体变体", "prod_name": "商品名", "product_group_name": "商品组",
            "product_type_name": "商品类型", "colour_group_name": "颜色组",
            "graphical_appearance_name": "图案", **PURCHASE_COLUMNS, "history": "训练期状态",
        },
        ("share",),
    )
    if catalog["colour_group_name"].nunique() > 1 and variants["items"].sum():
        colors = variants.groupby("colour_group_name", as_index=False)["items"].sum().sort_values("items", ascending=False)
        colors["share"] = colors["items"] / colors["items"].sum()
        display_table(colors, {"colour_group_name": "族内颜色组", "items": "购买件数", "share": "族内购买占比"}, ("share",))
    if st.toggle("查看同款跨日再次购买"):
        with st.spinner("正在比较款式族与具体变体的不同购买日…"):
            repeated = q.family_cross_day(scope, code).copy()
        repeated["code"] = repeated["code"].map(lambda value: article_code(value) if value.isdecimal() else value)
        repeated["repeat_rate"] = repeated["repeat_customers"] / repeated["buyers"]
        display_table(
            repeated,
            {"code": "变体或款式族", "buyers": "买家数", "repeat_customers": "跨日再次购买客户数", "repeat_rate": "跨日再次购买客户率"},
            ("repeat_rate",),
        )
        st.caption("使用当前筛选内的不同购买日。同日多件合计为一个购买日。")
    if st.toggle("比较首次观察购买此款式族后的限期回购"):
        with st.spinner("正在查找完整窗口内的同款族和任意商品购买日…"):
            repeated = q.family_repurchase_windows(scope, code).copy()
        if not no_results(repeated):
            repeated["repeat_rate"] = repeated["repeat_customers"] / repeated["eligible_customers"]
            repeated["any_repeat_rate"] = repeated["any_repeat_customers"] / repeated["eligible_customers"]
            display_table(
                repeated,
                {
                    "horizon_days": "窗口天数", "eligible_customers": "合格客户数",
                    "repeat_customers": "同款式族回购客户数", "repeat_rate": "同款式族再次购买日率",
                    "any_repeat_customers": "任意商品回购客户数", "any_repeat_rate": "任意商品再次购买日率",
                },
                ("repeat_rate", "any_repeat_rate"),
            )
            st.caption(f"日期筛选限定族内首次观察购买日。后续购买使用各渠道的完整记录，观察截止日为 {q.CUTOFF:%Y-%m-%d}。")


def product_hierarchy(scope: q.Scope) -> None:
    field = st.selectbox("商品经营层级", tuple(q.PRODUCT_LEVELS), format_func=q.PRODUCT_LEVELS.get)
    products = q.group_products(scope, field)
    if no_results(products):
        return
    bars(products, "category", q.PRODUCT_LEVELS[field])
    display_table(products, {"category": q.PRODUCT_LEVELS[field], **PURCHASE_COLUMNS}, ("share",))
    if st.toggle("查看分类月度构成"):
        monthly = q.monthly(scope, field)
        monthly["share"] = monthly["items"] / monthly.groupby("month")["items"].transform("sum")
        monthly["coverage"] = monthly["month"].map(lambda value: coverage_label(value, scope))
        display_table(
            monthly,
            {"month": "月份", "coverage": "月份覆盖", "category": q.PRODUCT_LEVELS[field], **PURCHASE_COLUMNS},
            ("share",),
        )


def appearance_page(scope: q.Scope) -> None:
    left, right = st.columns(2)
    appearance = left.selectbox("商品外观", tuple(q.APPEARANCES), format_func=q.APPEARANCES.get)
    level = right.selectbox(
        "分类轴", ("product_type_name", "section_name", "product_group_name"), format_func=q.PRODUCT_LEVELS.get,
    )
    matrix = q.appearance_matrix(scope, appearance, level).copy()
    if no_results(matrix):
        return
    category_order = matrix.groupby("category")["items"].sum().nlargest(12).index.tolist()
    appearance_order = matrix.groupby("appearance")["items"].sum().nlargest(12).index.tolist()
    shown = matrix.loc[matrix["category"].isin(category_order) & matrix["appearance"].isin(appearance_order)]
    chart = alt.Chart(shown).mark_rect().encode(
        x=alt.X("category:N", title=q.PRODUCT_LEVELS[level], sort=category_order),
        y=alt.Y("appearance:N", title=q.APPEARANCES[appearance], sort=appearance_order),
        color=alt.Color("items:Q", title="购买件数", scale=alt.Scale(scheme="redpurple")),
        tooltip=[
            alt.Tooltip("category:N", title=q.PRODUCT_LEVELS[level]),
            alt.Tooltip("appearance:N", title=q.APPEARANCES[appearance]),
            alt.Tooltip("items:Q", title="购买件数", format=","),
            alt.Tooltip("buyers:Q", title="买家数", format=","),
        ],
    )
    show_chart(chart)
    st.caption("每个图轴显示购买件数最多的 12 项。下方明细保留全部类别，买家数可能跨组重叠。")
    matrix["share"] = matrix["items"] / matrix["items"].sum()
    display_table(
        matrix, {"category": q.PRODUCT_LEVELS[level], "appearance": q.APPEARANCES[appearance], **PURCHASE_COLUMNS},
        ("share",),
    )


def product(scope: q.Scope) -> None:
    st.header("商品组合")
    show_scope(scope)
    tab = st.segmented_control("商品分析方式", ("分类层级", "款式族与变体", "颜色与图案"), default="分类层级")
    if tab == "分类层级":
        product_hierarchy(scope)
    elif tab == "款式族与变体":
        families(scope)
    elif tab == "颜色与图案":
        appearance_page(scope)


def stages(scope: q.Scope) -> None:
    choice = st.selectbox("客户分组", [None, *q.CUSTOMER_GROUPS], format_func=lambda field: q.CUSTOMER_GROUPS[field] if field else "不分组")
    with st.spinner("正在按客户不同购买日计算完整窗口…"):
        data = q.stage(scope, group=choice).copy()
    if no_results(data):
        return
    data["repeat_rate"] = data["repeat_rate"].astype(float)
    data["no_repeat_rate"] = data["no_repeat_customers"] / data["eligible_customers"]
    data["stage"] = data["purchase_day_no"].map(lambda value: f"第 {value} 个购买日")
    if choice:
        group_values = data["cohort"].drop_duplicates().tolist()
        plot_group = st.selectbox("图表分组值", group_values)
        chart_data = data.loc[data["cohort"] == plot_group]
        st.caption("合格客户数为各阶段和窗口的样本分母。未知属性单列。")
    else:
        chart_data = data
    chart = alt.Chart(chart_data).mark_line(point=True, strokeWidth=2).encode(
        x=alt.X("horizon_days:O", title="观察窗口（天）"),
        y=alt.Y("repeat_rate:Q", title="再次购买日率", axis=alt.Axis(format=".0%")),
        color=alt.Color("stage:N", title=None, scale=alt.Scale(range=[ACCENT, "#4e7082", "#97712b"])),
        tooltip=[
            alt.Tooltip("stage:N", title="阶段"),
            alt.Tooltip("horizon_days:O", title="窗口天数"),
            alt.Tooltip("eligible_customers:Q", title="合格客户数", format=","),
            alt.Tooltip("repeat_customers:Q", title="再次购买客户数", format=","),
            alt.Tooltip("repeat_rate:Q", title="再次购买日率", format=".2%"),
        ],
    )
    show_chart(chart, 270)
    columns = {"cohort": q.CUSTOMER_GROUPS[choice]} if choice else {}
    display_table(data, columns | STAGE_COLUMNS, ("repeat_rate", "no_repeat_rate"))
    if choice:
        window_coverage = data.groupby(["purchase_day_no", "horizon_days"], as_index=False).agg(
            eligible_customers=("eligible_customers", "sum"),
        )
        unknown = data.loc[data["cohort"] == "未知", ["purchase_day_no", "horizon_days", "eligible_customers"]]
        coverage = window_coverage.merge(unknown, how="left", on=["purchase_day_no", "horizon_days"], suffixes=("", "_unknown")).fillna(0)
        coverage["attribute_coverage"] = 1 - coverage["eligible_customers_unknown"] / coverage["eligible_customers"]
        with st.expander("客户属性覆盖率"):
            display_table(
                coverage, {"purchase_day_no": "观察购买日序号", "horizon_days": "窗口天数", "attribute_coverage": "属性覆盖率"},
                ("attribute_coverage",),
            )


def cohorts(scope: q.Scope) -> None:
    with st.spinner("正在计算首现月份组回购…"):
        data = q.stage(scope, by_month=True).copy()
    if no_results(data):
        return
    data["repeat_rate"] = data["repeat_rate"].astype(float)
    data["no_repeat_rate"] = data["no_repeat_customers"] / data["eligible_customers"]
    chart = alt.Chart(data).mark_line(point=True, strokeWidth=2).encode(
        x=alt.X("cohort:T", title="观察期首现月份", axis=alt.Axis(format="%Y-%m", labelOverlap=True)),
        y=alt.Y("repeat_rate:Q", title="再次购买日率", axis=alt.Axis(format=".0%")),
        color=alt.Color("horizon_days:N", title="窗口天数", scale=alt.Scale(range=[ACCENT, "#4e7082", "#97712b"])),
        tooltip=[
            alt.Tooltip("cohort:T", title="首现月份", format="%Y-%m"),
            alt.Tooltip("horizon_days:O", title="窗口天数"),
            alt.Tooltip("eligible_customers:Q", title="合格客户数", format=","),
            alt.Tooltip("repeat_customers:Q", title="再次购买客户数", format=","),
            alt.Tooltip("repeat_rate:Q", title="再次购买日率", format=".2%"),
        ],
    )
    show_chart(chart, 275)
    columns = {"cohort": "观察期首现月份"} | {key: STAGE_COLUMNS[key] for key in (
        "horizon_days", "eligible_customers", "repeat_customers", "no_repeat_customers", "repeat_rate", "no_repeat_rate",
    )}
    display_table(data, columns, ("repeat_rate", "no_repeat_rate"))


def customer_history(scope: q.Scope) -> None:
    with st.spinner("正在汇总客户历史购买频次…"):
        data = q.recency(scope)
    if no_results(data):
        return
    chart = alt.Chart(data).mark_rect().encode(
        x=alt.X("frequency_band:O", title="观察购买日数", sort=["1", "2", "3", "4+"]),
        y=alt.Y("recency_band:O", title="距数据截止日的天数"),
        color=alt.Color("customer_count:Q", title="客户数", scale=alt.Scale(scheme="redpurple")),
        tooltip=[
            alt.Tooltip("frequency_band:O", title="观察购买日数"),
            alt.Tooltip("recency_band:O", title="距截止日天数"),
            alt.Tooltip("customer_count:Q", title="客户数", format=","),
            alt.Tooltip("item_count:Q", title="购买件数", format=","),
        ],
    )
    show_chart(chart, 220)
    st.caption(f"频次和最近一次购买日使用当前日期范围；近期性以历史数据截止日 {q.CUTOFF:%Y-%m-%d} 计算。")
    frequency = data.groupby("frequency_band", as_index=False)["customer_count"].sum()
    frequency["share"] = frequency["customer_count"] / frequency["customer_count"].sum()
    display_table(
        frequency, {"frequency_band": "观察购买日数", "customer_count": "客户数", "share": "客户占比"},
        ("share",),
    )
    if st.toggle("查看相邻购买日相隔至少 90 天的回购"):
        result = q.long_gap(scope)
        columns = st.columns(3)
        columns[0].metric("再次购买客户", number(result["customers"]))
        columns[1].metric("再次购买日", number(result["purchase_days"]))
        median = result["median_days"]
        columns[2].metric("间隔中位数（天）", "—" if median is None else f"{median:g}")
        st.caption("日期筛选限定相邻购买日中的后一天。")


def breadth(scope: q.Scope) -> None:
    show_scope(scope)
    left, right = st.columns(2)
    dimensions = {"product_group_name": "商品组", "product_type_name": "商品类型", "colour_group_name": "颜色组"}
    dimension = left.selectbox("购买广度维度", list(dimensions), format_func=dimensions.get)
    group = right.selectbox("广度客户分组", [None, *q.CUSTOMER_GROUPS], format_func=lambda field: q.CUSTOMER_GROUPS[field] if field else "不分组")
    with st.spinner("正在统计每位客户的购买类别…"):
        data = q.customer_breadth(scope, dimension, group)
    if no_results(data):
        return
    display_table(
        data.round({"average_breadth": 2}),
        {
            "segment": q.CUSTOMER_GROUPS[group] if group else "分组",
            "customer_count": "样本客户数",
            "average_breadth": "平均购买类别数",
            "average_main_share": "平均主类购买件数占比",
        },
        ("average_main_share",),
    )
    st.caption("先按客户计算类别数和件数最多的单一类别占比，再对分组内客户取均值。")


def customers(scope: q.Scope) -> None:
    st.header("客户回购")
    tab = st.segmented_control("客户分析方式", ("回购阶段", "首现月份组", "近期性与频次", "品类广度"), default="回购阶段")
    if tab in ("回购阶段", "首现月份组"):
        show_scope(scope, purchases=False)
        st.caption(f"回购使用完整客户购买日序列。日期筛选限定起始购买日，观察窗口以 {q.CUTOFF:%Y-%m-%d} 为截止日。")
    if tab == "回购阶段":
        stages(scope)
    elif tab == "首现月份组":
        cohorts(scope)
    elif tab == "近期性与频次":
        show_scope(scope, purchases=False)
        customer_history(scope)
    elif tab == "品类广度":
        breadth(scope)


def channel_mix(scope: q.Scope) -> None:
    active = replace(scope, channel=None)
    show_scope(active)
    monthly = q.channel_months(active)
    if no_results(monthly):
        return
    monthly["coverage"] = monthly["month"].map(lambda value: coverage_label(value, active))
    chart = alt.Chart(monthly).mark_line(point=True, strokeWidth=2).encode(
        x=alt.X("month:T", title="月份", axis=alt.Axis(format="%Y-%m", labelOverlap=True)),
        y=alt.Y("items:Q", title="购买件数"),
        color=alt.Color("channel:N", title="渠道编码", scale=alt.Scale(domain=["1", "2"], range=CHANNEL_COLORS)),
        tooltip=[
            alt.Tooltip("month:T", title="月份", format="%Y-%m"),
            alt.Tooltip("coverage:N", title="覆盖"),
            alt.Tooltip("channel:N", title="渠道编码"),
            alt.Tooltip("items:Q", title="购买件数", format=","),
            alt.Tooltip("buyers:Q", title="买家数", format=","),
        ],
    )
    show_chart(chart, 245)
    if (monthly["coverage"] == "部分月").any():
        partial = monthly.loc[monthly["coverage"] == "部分月", "month"].drop_duplicates()
        st.caption(f"部分月：{'、'.join(f'{value:%Y-%m}' for value in partial)}。")
    show_overlap(q.overlap(active))
    options = q.PRODUCT_LEVELS | q.APPEARANCES | q.RANK_KEYS
    level = st.selectbox("渠道商品构成", list(options), format_func=options.get)
    data = q.channel_composition(active, level)
    shown = data.copy()
    if level == "article_id":
        shown["category"] = shown["category"].map(article_code)
    display_table(shown, {"channel": "渠道编码", "category": options[level], **PURCHASE_COLUMNS}, ("share",))
    st.caption("每个编码显示购买件数最多的 20 项。占比分母为同编码在当前筛选范围的购买件数。")


def channel_switch(scope: q.Scope) -> None:
    show_scope(scope, purchases=False)
    with st.spinner("正在比较客户相邻购买日的渠道编码…"):
        data = q.switch(scope)
        mixed = q.mixed(scope)
    sample = int(data["pair_count"].sum())
    changed = int(data.loc[data["from_channel"] != data["to_channel"], "pair_count"].sum())
    cols = st.columns(3)
    cols[0].metric("单编码购买日切换率", percent(ratio(changed, sample)))
    cols[1].metric("切换配对 / 合格配对", f"{number(changed)} / {number(sample)}")
    cols[2].metric("混合编码购买日", number(mixed["mixed_days"]) if mixed["total_days"] else "0")
    st.caption("仅计算日期范围内相邻、且两日都只出现一个渠道编码的客户购买日配对。混合编码购买日单独计数。")
    if not no_results(data):
        display_table(data, {"from_channel": "前一购买日编码", "to_channel": "后一购买日编码", "pair_count": "配对数"})


def journeys() -> None:
    st.caption(f"完整训练期 {q.START:%Y-%m-%d} 至 {q.CUTOFF:%Y-%m-%d} · 商品组")
    with st.spinner("正在汇总相邻购买日的品类旅程…"):
        data = q.read("""
            SELECT /*+ SET_VAR(enable_spill=true, enable_page_cache=false, exec_mem_limit=2684354560) */
                   from_group, to_group, pair_count
            FROM v_category_journey ORDER BY pair_count DESC, from_group, to_group
        """)
    if no_results(data):
        return
    chart = alt.Chart(data).mark_rect().encode(
        x=alt.X("to_group:N", title="后一购买日商品组"),
        y=alt.Y("from_group:N", title="前一购买日商品组"),
        color=alt.Color("pair_count:Q", title="配对数", scale=alt.Scale(scheme="redpurple")),
        tooltip=[
            alt.Tooltip("from_group:N", title="前一购买日商品组"),
            alt.Tooltip("to_group:N", title="后一购买日商品组"),
            alt.Tooltip("pair_count:Q", title="配对数", format=","),
        ],
    )
    show_chart(chart, 320)
    shown = data.head(20)
    display_table(shown, {"from_group": "前一购买日商品组", "to_group": "后一购买日商品组", "pair_count": "配对数"})
    st.caption("列出配对数最多的 20 个组合。按客户相邻不同购买日的商品组集合计算，一组购买日可计入多个类别组合。")


def channels(scope: q.Scope) -> None:
    st.header("渠道旅程")
    st.caption("渠道以源编码 1 和 2 显示。")
    tab = st.segmented_control("渠道分析方式", ("渠道构成", "跨编码切换", "跨日品类旅程"), default="渠道构成")
    if tab == "渠道构成":
        channel_mix(scope)
    elif tab == "跨编码切换":
        channel_switch(scope)
    elif tab == "跨日品类旅程":
        journeys()


def intake() -> None:
    st.header("原始文件核验")
    st.markdown(f"下载入口为 [Kaggle 官方竞赛数据页]({SOURCE_PAGE})。请在下载前登录并接受竞赛规则。")
    results = verify_files(digest=False)
    present = sum(item["status"] in {"size_matches", "manifest_missing"} for item in results)
    st.metric("本地存在文件", f"{present} / {len(FILES)}")
    st.dataframe(
        [
            {
                "文件": item["file_name"],
                "状态": item["status"],
                "清单字节": item["expected_bytes"],
                "实际字节": item["actual_bytes"],
                "路径": item["relative_path"],
            }
            for item in results
        ],
        width="stretch",
        hide_index=True,
    )
    if st.button("核验三张表的字节数与 SHA-256"):
        with st.spinner("正在读取原始文件并计算 SHA-256…"):
            results = verify_files()
        st.dataframe(
            [{"文件": item["file_name"], "校验状态": item["status"], "实际 SHA-256": item["actual_sha256"] or "—"}
             for item in results],
            width="stretch",
            hide_index=True,
        )
        if all(item["status"] == "verified" for item in results):
            st.success("三张核心 CSV 的字节数与 SHA-256 均和本地清单一致。")
        else:
            st.error("至少一个文件缺失，或与本地清单的摘要不一致。")
    elif present != len(FILES):
        st.info("接受 Kaggle 竞赛规则并配置身份验证后，运行 `pixi run data-fetch` 下载文件。")


st.title("H&M 购买分析")
page = st.segmented_control("分析主题", PAGES, default="经营概览", label_visibility="collapsed")
if page == "文件核验":
    intake()
elif page:
    try:
        if not q.warehouse_ready():
            st.warning("Doris 中的 H&M 数据尚未完整发布。请按部署文档完成数据加载。")
            if st.button("重新检查数据"):
                q.read.clear()
                st.rerun()
            st.stop()
        scope = sidebar_scope()
        {"经营概览": overview, "商品组合": product, "客户回购": customers, "渠道旅程": channels}[page](scope)
    except pymysql.MySQLError as exc:
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        logging.getLogger(__name__).warning("Doris dashboard query failed, code=%s", code)
        if code in {1049, 1146}:
            st.warning("分析库或视图尚未就绪。请按部署文档完成 H&M 数据加载和分析视图初始化。")
        elif "MEM_LIMIT_EXCEEDED" in str(exc):
            st.warning("Doris 当前可用内存不足。请等待其他查询结束后重试。")
        else:
            st.error("暂时无法取得 Doris 查询结果。请检查项目服务状态后重试。")
        if st.button("重试 Doris 查询"):
            q.read.clear()
            st.rerun()
