"""香港 Inside Airbnb 先锋市场观察看板。"""

from __future__ import annotations

import html

import pandas as pd
import streamlit as st

from inside_airbnb.hong_kong_pilot import load_dashboard_data

st.set_page_config(
    page_title="香港短租市场观察",
    page_icon="🏙️",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data(ttl=600, show_spinner=False)
def get_data():
    listings, geojson, availability_df, reviews, source_label, fallback_message = load_dashboard_data()
    listings = listings.copy()
    listings["snapshot_date"] = listings["snapshot_date"].astype("string")
    listings["neighbourhood"] = listings["neighbourhood"].astype("string").fillna("未归类")
    for column in (
        "accommodates", "bedrooms", "beds", "bathrooms", "price", "availability_30",
        "availability_60", "availability_90", "availability_365", "number_of_reviews",
        "number_of_reviews_ltm", "review_scores_rating", "latitude", "longitude",
        "host_city_listing_count",
    ):
        if column in listings.columns:
            listings[column] = pd.to_numeric(listings[column], errors="coerce")
    for column in ("host_is_superhost", "host_identity_verified"):
        if column in listings.columns:
            listings[column] = listings[column].map(
                lambda value: True if value is True or str(value).lower() in {"1", "t", "true"}
                else False if value is False or str(value).lower() in {"0", "f", "false"}
                else pd.NA
            ).astype("boolean")
    if not availability_df.empty:
        availability_df = availability_df.copy()
        availability_df["snapshot_date"] = availability_df["snapshot_date"].astype("string")
        availability_df["calendar_date"] = availability_df["calendar_date"].astype("string")
    if not reviews.empty:
        reviews = reviews.copy()
        reviews["review_month"] = pd.to_datetime(reviews["review_month"], errors="coerce")
    return listings, geojson, availability_df, reviews, source_label, fallback_message


def _feature_rings(geometry: dict) -> list[list[list[float]]]:
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if kind == "Polygon":
        return list(coordinates)
    if kind == "MultiPolygon":
        return [ring for polygon in coordinates for ring in polygon]
    return []


def neighbourhood_svg(geojson: dict, counts: dict[str, int]) -> str | None:
    features = geojson.get("features", [])
    points = [
        point
        for feature in features
        for ring in _feature_rings(feature.get("geometry") or {})
        for point in ring
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    if not points:
        return None

    min_lon = min(float(point[0]) for point in points)
    max_lon = max(float(point[0]) for point in points)
    min_lat = min(float(point[1]) for point in points)
    max_lat = max(float(point[1]) for point in points)
    width, height, padding = 1000.0, 650.0, 28.0
    span_lon = max(max_lon - min_lon, 1e-9)
    span_lat = max(max_lat - min_lat, 1e-9)
    scale = min((width - 2 * padding) / span_lon, (height - 2 * padding) / span_lat)
    map_width, map_height = span_lon * scale, span_lat * scale
    offset_x = (width - map_width) / 2
    offset_y = (height - map_height) / 2

    def project(point: list[float]) -> tuple[float, float]:
        x = offset_x + (float(point[0]) - min_lon) * scale
        y = height - offset_y - (float(point[1]) - min_lat) * scale
        return x, y

    maximum = max(counts.values(), default=0)
    palette = ("#e9e7fb", "#c9c4f3", "#a79dea", "#8274dc", "#5948c6")
    shapes: list[str] = []
    for feature in features:
        properties = feature.get("properties") or {}
        name = str(properties.get("neighbourhood") or "未命名街区")
        count = int(counts.get(name, 0))
        ratio = count / maximum if maximum else 0
        palette_index = min(len(palette) - 1, int(ratio * len(palette))) if count else 0
        paths = []
        for ring in _feature_rings(feature.get("geometry") or {}):
            if len(ring) < 4:
                continue
            projected = [project(point) for point in ring]
            commands = [f"M {projected[0][0]:.2f},{projected[0][1]:.2f}"]
            commands.extend(f"L {x:.2f},{y:.2f}" for x, y in projected[1:])
            commands.append("Z")
            paths.append(" ".join(commands))
        if not paths:
            continue
        safe_name = html.escape(name, quote=True)
        tooltip = html.escape(f"{name} · {count:,} 个房源", quote=False)
        shapes.append(
            f'<path d="{" ".join(paths)}" fill="{palette[palette_index]}" '
            f'class="district"><title>{tooltip}</title>'
            f'<desc>{safe_name}: {count} listings</desc></path>'
        )
    if not shapes:
        return None
    legend = "".join(
        f'<span><i style="background:{color}"></i>{label}</span>'
        for color, label in zip(palette, ("较少", "较低", "中等", "较高", "较多"))
    )
    return f"""<div class="map-card">
      <svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="香港街区房源分布图">
        {''.join(shapes)}
      </svg>
      <div class="legend">{legend}</div>
    </div>
    <style>
      .map-card {{ background:#fbfaff; border:1px solid #ece9f7; border-radius:16px; padding:12px; }}
      svg {{ width:100%; height:auto; max-height:540px; }}
      .district {{ stroke:#fff; stroke-width:2.1; stroke-linejoin:round; transition:filter .15s ease; }}
      .district:hover {{ filter:brightness(.9); stroke:#37288e; stroke-width:3.2; }}
      .legend {{ display:flex; flex-wrap:wrap; gap:14px; padding:8px 6px 2px; color:#625f72; font:12px sans-serif; }}
      .legend i {{ display:inline-block; width:13px; height:13px; border-radius:4px; margin-right:5px; vertical-align:-2px; }}
    </style>"""


@st.cache_data(ttl=600, show_spinner=False)
def cached_source_data():
    return get_data()


try:
    listings, geojson, availability, reviews, source_label, fallback_message = cached_source_data()
except Exception as exc:  # noqa: BLE001
    st.title("香港短租市场观察")
    st.error(f"看板读取失败：{exc}")
    st.info("检查 `data/raw/inside_airbnb/hong_kong/` 是否包含原始快照；Doris 模式还需先完成数据装载。")
    st.stop()

st.title("香港短租市场观察")
st.caption("Inside Airbnb 季度房源快照 · 供数据结构与挂牌市场研究使用")
if fallback_message:
    st.info(
        "当前通过本地数据展示。Doris 模式需要主题表完整且后端节点有足够可用内存；"
        "可用 `pixi run data-load-hong-kong` 重试装载。"
    )
else:
    st.success(f"数据连接：{source_label}", icon="✅")

snapshot_dates = sorted(listings["snapshot_date"].dropna().unique().tolist())
if not snapshot_dates:
    st.error("没有可展示的快照日期。")
    st.stop()

with st.sidebar:
    st.header("筛选条件")
    selected_snapshot = st.selectbox(
        "观察快照",
        snapshot_dates,
        index=len(snapshot_dates) - 1,
        format_func=lambda value: pd.Timestamp(value).strftime("%Y年%m月%d日"),
    )
    neighbourhood_options = sorted(listings["neighbourhood"].dropna().unique().tolist())
    selected_neighbourhood = st.selectbox(
        "街区", ["全部街区", *neighbourhood_options]
    )
    room_options = sorted(listings["room_type"].dropna().unique().tolist())
    selected_room = st.selectbox("房间类型", ["全部房间类型", *room_options])
    property_options = sorted(listings["property_type"].dropna().unique().tolist())
    selected_property = st.selectbox("房屋类型", ["全部房屋类型", *property_options])
    superhost_options = ["全部房东", "超赞房东", "普通房东", "状态未知"]
    selected_superhost = st.selectbox("房东类型", superhost_options)
    st.divider()
    st.caption("数据范围：香港 4 个季度快照，2025 年 12 月至 2026 年 9 月。")

selected_snapshot_rows = listings.loc[listings["snapshot_date"] == selected_snapshot].copy()
filter_mask = pd.Series(True, index=selected_snapshot_rows.index)
if selected_neighbourhood != "全部街区":
    filter_mask &= selected_snapshot_rows["neighbourhood"].eq(selected_neighbourhood)
if selected_room != "全部房间类型":
    filter_mask &= selected_snapshot_rows["room_type"].eq(selected_room)
if selected_property != "全部房屋类型":
    filter_mask &= selected_snapshot_rows["property_type"].eq(selected_property)
if selected_superhost == "超赞房东":
    filter_mask &= selected_snapshot_rows["host_is_superhost"].eq(True).fillna(False)
elif selected_superhost == "普通房东":
    filter_mask &= selected_snapshot_rows["host_is_superhost"].eq(False).fillna(False)
elif selected_superhost == "状态未知":
    filter_mask &= selected_snapshot_rows["host_is_superhost"].isna()
filtered = selected_snapshot_rows.loc[filter_mask].copy()

if filtered.empty:
    st.warning("当前筛选条件没有房源记录，请调整侧边栏筛选。")
    st.stop()

prices = filtered["price"].dropna() if "price" in filtered else pd.Series(dtype="float64")
ratings = filtered["review_scores_rating"].dropna()
availability_days = filtered["availability_365"].dropna()
listing_count = int(filtered["listing_id"].nunique())
price_median = prices.median() if not prices.empty else None
rating_mean = ratings.mean() if not ratings.empty else None
availability_median = availability_days.median() if not availability_days.empty else None

metric_columns = st.columns(4)
metric_columns[0].metric("挂牌房源", f"{listing_count:,}")
metric_columns[1].metric(
    "挂牌价中位数",
    f"HK$ {price_median:,.0f}" if price_median is not None else "暂无",
    help="源字段 price 的非空值；不代表最终成交价。",
)
metric_columns[2].metric(
    "平均评分",
    f"{rating_mean:.2f}" if rating_mean is not None else "暂无",
    help=f"有效评分房源 {len(ratings):,} 条；其余空值不计入均值。",
)
metric_columns[3].metric(
    "未来 365 天可订中位数",
    f"{availability_median:,.0f} 天" if availability_median is not None else "暂无",
    help="房态可订天数，不是入住天数或出租率。",
)
price_coverage = len(prices) / len(filtered) if len(filtered) else 0
st.caption(
    f"所选快照 {selected_snapshot} · 挂牌价非空率 {price_coverage:.1%} "
    f"({len(prices):,}/{len(filtered):,}) · 当前筛选后的房源数 {listing_count:,}"
)

trend_mask = pd.Series(True, index=listings.index)
if selected_neighbourhood != "全部街区":
    trend_mask &= listings["neighbourhood"].eq(selected_neighbourhood)
if selected_room != "全部房间类型":
    trend_mask &= listings["room_type"].eq(selected_room)
if selected_property != "全部房屋类型":
    trend_mask &= listings["property_type"].eq(selected_property)
trend_base = listings.loc[trend_mask].copy()
trend = (
    trend_base.groupby("snapshot_date", as_index=False)
    .agg(listing_count=("listing_id", "nunique"))
    .sort_values("snapshot_date")
)
price_trend = (
    trend_base.dropna(subset=["price"])
    .groupby("snapshot_date", as_index=False)
    .agg(median_price=("price", "median"), priced_listings=("listing_id", "nunique"))
    .sort_values("snapshot_date")
)

overview_tab, neighbourhood_tab, structure_tab, host_tab, availability_tab, reviews_tab, listings_tab, notes_tab = st.tabs(
    ["市场概览", "街区分布", "房源结构", "房东画像", "可订日历", "评论趋势", "房源明细", "数据口径"]
)

with overview_tab:
    st.subheader("季度快照观察")
    left, right = st.columns([1.2, 1])
    with left:
        st.markdown("**快照中的房源记录数**")
        st.bar_chart(trend.set_index("snapshot_date")["listing_count"], y_label="房源数", height=330)
        st.caption("每个点/柱代表一个抓取快照；记录量变化也可能来自来源覆盖变化。")
    with right:
        st.markdown("**挂牌价中位数（仅非空值）**")
        if price_trend.empty:
            st.info("所选条件下没有非空挂牌价。")
        else:
            st.bar_chart(price_trend.set_index("snapshot_date")["median_price"], y_label="HKD / 晚", height=330)
            st.caption("2025 年 12 月和 2026 年 3 月的源文件价格全部为空，图中不补值。")
    st.warning(
        "2026 年 3 月快照房源记录为 2,115 条，前后快照约 6,600 条。"
        "现有证据无法区分市场变化与抓取覆盖差异，因此时间趋势仅作来源快照观察。"
    )

with neighbourhood_tab:
    st.subheader("街区房源分布")
    by_neighbourhood = (
        filtered.groupby("neighbourhood", as_index=False)
        .agg(
            listing_count=("listing_id", "nunique"),
            median_price=("price", "median"),
            average_rating=("review_scores_rating", "mean"),
            latitude=("latitude", "mean"),
            longitude=("longitude", "mean"),
        )
        .sort_values("listing_count", ascending=False)
    )
    map_counts = dict(zip(by_neighbourhood["neighbourhood"], by_neighbourhood["listing_count"]))
    svg = neighbourhood_svg(geojson, map_counts)
    if svg:
        st.components.v1.html(svg, height=600, scrolling=False)
        st.caption("填色代表当前筛选下的房源数量；鼠标悬停可查看街区名称和数量。")
    else:
        st.info("缺少可用的街区边界文件。")
    left, right = st.columns([1, 1])
    with left:
        st.markdown("**房源数量排名**")
        st.bar_chart(
            by_neighbourhood.head(12).set_index("neighbourhood")["listing_count"],
            horizontal=True,
            x_label="房源数",
            height=420,
        )
    with right:
        st.markdown("**街区指标**")
        summary = by_neighbourhood.rename(
            columns={
                "neighbourhood": "街区",
                "listing_count": "房源数",
                "median_price": "挂牌价中位数 HKD",
                "average_rating": "平均评分",
            }
        )[["街区", "房源数", "挂牌价中位数 HKD", "平均评分"]]
        st.dataframe(summary, width="stretch", hide_index=True, height=420)

with structure_tab:
    st.subheader("房型与价格结构")
    by_room = (
        filtered.groupby("room_type", as_index=False)
        .agg(
            listing_count=("listing_id", "nunique"),
            median_price=("price", "median"),
            rated_listings=("review_scores_rating", "count"),
            average_rating=("review_scores_rating", "mean"),
        )
        .sort_values("listing_count", ascending=False)
    )
    left, right = st.columns(2)
    with left:
        st.markdown("**按房间类型的房源数**")
        st.bar_chart(by_room.set_index("room_type")["listing_count"], y_label="房源数", height=340)
    with right:
        st.markdown("**按房间类型的挂牌价中位数**")
        priced_rooms = by_room.dropna(subset=["median_price"])
        if priced_rooms.empty:
            st.info("所选条件下没有可比较的挂牌价。")
        else:
            st.bar_chart(
                priced_rooms.set_index("room_type")["median_price"],
                y_label="HKD / 晚",
                height=340,
            )
    st.markdown("**评分与评论摘要**")
    st.dataframe(
        by_room.rename(
            columns={
                "room_type": "房间类型",
                "listing_count": "房源数",
                "median_price": "挂牌价中位数 HKD",
                "rated_listings": "有评分房源数",
                "average_rating": "平均评分",
            }
        ),
        width="stretch",
        hide_index=True,
    )
    st.caption("评分是房源公开评分；评论数不等于订单数。")

with host_tab:
    st.subheader("房东结构")
    host_flags = filtered["host_is_superhost"] if "host_is_superhost" in filtered else pd.Series(dtype="boolean")
    verified_flags = filtered["host_identity_verified"] if "host_identity_verified" in filtered else pd.Series(dtype="boolean")
    host_count = int(filtered["host_id"].nunique()) if "host_id" in filtered else 0
    superhost_count = int(host_flags.eq(True).sum()) if len(host_flags) else 0
    verified_count = int(verified_flags.eq(True).sum()) if len(verified_flags) else 0
    host_metrics = st.columns(3)
    host_metrics[0].metric("房东数", f"{host_count:,}")
    host_metrics[1].metric("超赞房东房源占比", f"{superhost_count / len(filtered):.1%}" if len(filtered) else "暂无")
    host_metrics[2].metric("身份已验证房源占比", f"{verified_count / len(filtered):.1%}" if len(filtered) else "暂无")
    host_rows = filtered.loc[filtered["host_id"].notna()].groupby("host_id", as_index=False).agg(
        listing_count=("listing_id", "nunique"),
        is_superhost=("host_is_superhost", "first"),
    )
    host_rows["房源组合"] = pd.cut(
        host_rows["listing_count"], bins=[0, 1, 5, 20, float("inf")],
        labels=["1 套", "2–5 套", "6–20 套", "21 套及以上"],
    )
    host_distribution = host_rows.groupby("房源组合", observed=False, as_index=False).agg(房东数=("host_id", "nunique"))
    left, right = st.columns(2)
    with left:
        st.markdown("**按房东房源组合分布**")
        st.bar_chart(host_distribution.set_index("房源组合")["房东数"], y_label="房东数", height=340)
    with right:
        st.markdown("**超赞房东标记下的房源指标**")
        host_summary = filtered.assign(
            房东类型=filtered["host_is_superhost"].map({True: "超赞房东", False: "普通房东"}).fillna("未知")
        ).groupby("房东类型", as_index=False).agg(
            房源数=("listing_id", "nunique"),
            挂牌价中位数=("price", "median"),
            平均评分=("review_scores_rating", "mean"),
        )
        st.dataframe(host_summary, width="stretch", hide_index=True)
    st.caption("房东按来源公开 ID 去重；页面不展示 ID、姓名或个人资料。房东属性按快照保存。")

with availability_tab:
    st.subheader("未来日历可订情况")
    availability_view = availability.loc[availability["snapshot_date"].eq(selected_snapshot)].copy() if not availability.empty else availability
    if selected_neighbourhood != "全部街区" and not availability_view.empty:
        availability_view = availability_view.loc[availability_view["neighbourhood"].eq(selected_neighbourhood)]
    if selected_room != "全部房间类型" and not availability_view.empty:
        availability_view = availability_view.loc[availability_view["room_type"].eq(selected_room)]
    if selected_property != "全部房屋类型" and not availability_view.empty:
        availability_view = availability_view.loc[availability_view["property_type"].eq(selected_property)]
    if not availability_view.empty:
        availability_view["calendar_date"] = pd.to_datetime(availability_view["calendar_date"], errors="coerce")
        daily = availability_view.groupby("calendar_date", as_index=False).agg(
            listing_count=("listing_count", "sum"),
            available_count=("available_listing_count", "sum"),
        ).sort_values("calendar_date")
        daily["可订房源比例"] = (
            daily["available_count"].astype(float)
            / daily["listing_count"].replace(0, float("nan")).astype(float)
            * 100
        )
        st.line_chart(daily.set_index("calendar_date")["可订房源比例"], y_label="来源标记可订房源比例（%）", height=360)
        st.caption("按所选快照的日历日期统计；不可订可能表示已预订或房东封锁，不能解释为入住率。")
        district_calendar = availability_view.groupby("neighbourhood", as_index=False).agg(
            平均可订房源比例=("available_listing_count", "sum"),
            房态记录=("listing_count", "sum"),
        )
        district_calendar["平均可订房源比例"] = (
            district_calendar["平均可订房源比例"].astype(float)
            / district_calendar["房态记录"].replace(0, float("nan")).astype(float)
            * 100
        ).round(1)
        st.dataframe(
            district_calendar.sort_values("平均可订房源比例", ascending=False),
            width="stretch", hide_index=True,
        )
    else:
        st.info("当前快照和筛选条件没有日历汇总记录。")

with reviews_tab:
    st.subheader("公开评论活动")
    if not reviews.empty:
        total_reviews = int(reviews["review_count"].sum())
        months_with_reviews = int(reviews["review_month"].nunique())
        review_metrics = st.columns(3)
        review_metrics[0].metric("最新公开评论记录", f"{total_reviews:,}")
        review_metrics[1].metric("有评论月份", f"{months_with_reviews:,}")
        review_metrics[2].metric("月均评论记录", f"{total_reviews / months_with_reviews:,.0f}" if months_with_reviews else "暂无")
        monthly_reviews = reviews.sort_values("review_month").set_index("review_month")
        st.bar_chart(monthly_reviews["review_count"], y_label="评论记录数", height=380)
        st.dataframe(
            reviews.sort_values("review_count", ascending=False).head(12).rename(
                columns={"review_month": "评论月份", "review_count": "评论记录数", "reviewed_listing_count": "有评论房源数"}
            )[["评论月份", "评论记录数", "有评论房源数"]],
            width="stretch", hide_index=True,
        )
        st.caption("评论按最新（2026-09-20）累积导出文件中的评论日期分月；不把各期累积文件相加。评论数不是订单数。")
    else:
        st.info("没有可展示的月度评论汇总。")

with listings_tab:
    st.subheader("筛选后的房源明细")
    display_columns = [
        "neighbourhood", "property_type", "room_type", "accommodates", "bedrooms", "beds",
        "price", "availability_365", "number_of_reviews_ltm", "review_scores_rating",
    ]
    display = filtered[[column for column in display_columns if column in filtered.columns]].copy()
    display = display.rename(
        columns={
            "neighbourhood": "街区",
            "property_type": "房屋类型",
            "room_type": "房间类型",
            "accommodates": "可容纳人数",
            "bedrooms": "卧室数",
            "beds": "床位数",
            "price": "挂牌价 HKD",
            "availability_365": "未来365天可订天数",
            "number_of_reviews_ltm": "近12个月评论数",
            "review_scores_rating": "评分",
        }
    )
    st.caption(f"当前结果 {len(display):,} 条；表格显示最多前 500 条。房源 ID、房东 ID 和单房源坐标不在表中展示。")
    st.dataframe(display.head(500), width="stretch", hide_index=True, height=520)

with notes_tab:
    st.subheader("口径与使用边界")
    st.markdown(
        """
        - **数据来源**：Inside Airbnb 香港 `listings.csv.gz` 季度快照及 `neighbourhoods.geojson` 街区边界。
        - **房源数**：一个快照内按 `listing_id` 去重后的挂牌记录数。
        - **挂牌价**：`price` 源字段去除符号后的数值；只计算非空值。没有实际成交价格，不能代表订单价格。
        - **可订天数**：抓取时未来 365 天日历的可订汇总。不可订可能是预订或房东封锁，不能解释为入住。
        - **可订日历**：按快照、日历日期、街区、房型和房屋类型汇总日历记录。比例是来源标记的可订房态占比，不是入住率。
        - **评分与评论**：评分来自房源公开汇总字段；月度评论取最新一期累积评论文件，不跨快照相加，评论数不等于订单数。
        - **房东画像**：按房东公开 ID 去重；仅保留超赞房东、身份验证、加入日期及房源数等分析属性，不加载姓名、简介或图片。
        - **空间展示**：街区边界使用来源 GeoJSON；看板不显示单房源点位。来源坐标本身经过模糊处理。
        - **缺失与波动**：2025 年 12 月、2026 年 3 月 `price` 全空；2026 年 3 月房源数显著偏低，尚未确认原因。
        - **明确排除**：没有订单明细；不计算实际入住率、ADR、RevPAR 或实际收入。未核实定义的估算字段也不参与 MVP 指标。
        """
    )
    st.markdown(
        "数据致谢：[Inside Airbnb](https://insideairbnb.com/get-the-data/) · "
        "[数据假设说明](https://beta.insideairbnb.com/data-assumptions/) · "
        "[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)"
    )

st.divider()
st.caption(
    f"数据源：Inside Airbnb · 页面读取：{source_label} · 快照：{snapshot_dates[0]} — {snapshot_dates[-1]}"
)
