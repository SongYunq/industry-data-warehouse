"""Streamlit application for the isolated Chongqing warehouse."""

from __future__ import annotations

import math
import re
from datetime import date

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

from experiments.chongqing.warehouse import connect


BUSINESS = {"delivery": "派件", "pickup": "揽收"}
COLORS = {"派件": "#3157d5", "揽收": "#128575"}
INK = "#182943"
MUTED = "#536581"
TOPICS = ("运营总览", "履约与人员", "区域热度", "轨迹样本", "道路环境")
ROAD_LABELS = {
    "residential": "居住区道路", "unclassified": "未分级道路", "service": "服务道路",
    "tertiary": "三级道路", "secondary": "二级道路", "primary": "一级道路",
    "footway": "步行道", "motorway": "高速公路", "trunk": "干线", "path": "小径",
}


st.set_page_config(page_title="重庆末端揽派分析", page_icon="📦", layout="wide")
st.markdown("""
<style>
:root { color-scheme: light; }
[data-testid="stAppViewContainer"], [data-testid="stHeader"] { background: #f4f7fb; }
[data-testid="stSidebar"] { background: #fff; border-right: 1px solid #dfe7f3; }
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
[data-testid="stSidebar"] label { color: #233653; }
[data-testid="stMainBlockContainer"] { max-width: 1420px; padding: 2.2rem 3rem 3rem; }
.cq-heading {
    background: #142c4b;
    background-image: radial-gradient(ellipse at 92% 90%, #1e5969 0%, transparent 54%),
                      linear-gradient(115deg, #142745, #203c6d);
    border-radius: 20px; padding: 27px 32px 29px; color: #fff;
    display: flex; align-items: end; justify-content: space-between; gap: 24px;
}
.cq-heading h1 { color: #fff; padding: 0; margin: 5px 0 0; line-height: 1.2;
    font-size: clamp(29px, 4vw, 46px); letter-spacing: -.035em; font-weight: 770; }
.cq-heading p { margin: 0; font-size: 13px; color: #d6e5fc; }
.cq-heading .cq-place { font-size: 13px; font-weight: 650; color: #9ae6d5; }
[data-testid="stMetric"] { min-height: 105px; }
[data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; letter-spacing: -.035em; }
[data-testid="stVerticalBlockBorderWrapper"] { background: #fff; border-color: #e0e8f3; }
[data-testid="stSegmentedControl"] button { font-weight: 620; min-height: 38px; }
@media(max-width: 760px) {
  [data-testid="stMainBlockContainer"] { padding: 1.35rem .9rem 2rem; }
  .cq-heading { display: block; padding: 20px; }
  .cq-heading p { margin-top: 8px; }
}
</style>
<div class="cq-heading">
  <div><div class="cq-place">重庆 · 末端物流</div><h1>揽派与道路分析</h1></div>
  <p>LaDe 历史样本</p>
</div>
""", unsafe_allow_html=True)


@st.cache_data(ttl=90, show_spinner=False)
def query(sql: str, args: tuple = ()) -> pd.DataFrame:
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, args or None)
            rows = cursor.fetchall()
            columns = [item[0] for item in cursor.description]
        return pd.DataFrame.from_records(rows, columns=columns)
    finally:
        connection.close()


def presentation(chart: alt.TopLevelMixin, height: int = 270) -> None:
    styled = (
        chart.properties(height=height)
        .configure(background="transparent", font="Inter")
        .configure_view(strokeOpacity=0)
        .configure_axis(labelColor=MUTED, titleColor=INK, gridColor="#e5ebf3", labelFontSize=11)
        .configure_legend(labelColor=MUTED, titleColor=INK, orient="top")
    )
    st.altair_chart(styled, width="stretch", theme=None)


def business_color() -> alt.Color:
    return alt.Color("业务:N", title=None, scale=alt.Scale(domain=list(COLORS), range=list(COLORS.values())))


def display_business(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(业务=frame["business_type"].map(BUSINESS))


def ratio(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:.1%}" if denominator else "—"


def metric_row(metrics: list[tuple[str, str]]) -> None:
    for column, (label, value) in zip(st.columns(len(metrics)), metrics):
        column.metric(label, value, border=True)


def apply_filters() -> tuple[str, tuple]:
    bounds = query("SELECT MIN(completed_date) AS first_day, MAX(completed_date) AS last_day FROM dws_daily")
    first, last = (pd.Timestamp(bounds.iloc[0][name]).date() for name in ("first_day", "last_day"))
    with st.sidebar:
        st.header("订单筛选")
        selection = st.date_input("实际完成日期", (first, last), min_value=first, max_value=last, format="YYYY-MM-DD")
        kind = st.radio("业务类型", ("全部", "派件", "揽收"), horizontal=True)
        st.caption("日期与业务筛选适用于运营总览、履约与人员、区域热度。")
        st.divider()
        st.markdown("**数据范围**")
        st.caption("揽派主表以 2022 年 5–10 月业务分片为主，保留源文件中窗口外的完成日期。")
        st.caption("轨迹采用独立的 3–4 月配套数据。路网为静态数据。")
    if len(selection) != 2:
        selection = (selection[0], selection[0]) if selection else (first, last)
    begin, end = selection
    predicate = "completed_date BETWEEN %s AND %s"
    args: tuple[date | str, ...] = (begin, end)
    if kind != "全部":
        business = next(key for key, value in BUSINESS.items() if value == kind)
        predicate += " AND business_type = %s"
        args += (business,)
    return predicate, args


def order_day(predicate: str, args: tuple) -> pd.DataFrame:
    return query(f"SELECT * FROM dws_daily WHERE {predicate} ORDER BY completed_date, business_type", args)


def overview(predicate: str, args: tuple) -> None:
    data = order_day(predicate, args)
    if data.empty:
        st.info("所选时间和业务暂无完成记录。")
        return
    totals = data.groupby("business_type")["orders"].sum()
    appointment = int(data[["appointment_early", "appointment_in_window", "appointment_late"]].sum().sum())
    inside = int(data["appointment_in_window"].sum())
    metric_row([
        ("完成总量", f"{int(data['orders'].sum()):,}"),
        ("派件完成", f"{totals['delivery']:,}" if "delivery" in totals else "—"),
        ("揽收完成", f"{totals['pickup']:,}" if "pickup" in totals else "—"),
        ("揽收预约窗内率", ratio(inside, appointment)),
    ])
    trend = display_business(data).assign(完成日=pd.to_datetime(data.completed_date), 完成量=data.orders)
    peak = trend.groupby("完成日")["完成量"].sum().sort_values(ascending=False).head(1)
    with st.container(border=True):
        st.subheader("每日完成量")
        st.caption(f"所选范围峰值为 {peak.index[0]:%Y-%m-%d}，当天完成 {int(peak.iloc[0]):,} 单。")
        presentation(alt.Chart(trend).mark_bar().encode(
            x=alt.X("yearmonthdate(完成日):T", title=None, axis=alt.Axis(format="%m-%d", tickCount=8)),
            y=alt.Y("sum(完成量):Q", title="单"), color=business_color(),
            tooltip=[alt.Tooltip("yearmonthdate(完成日):T", title="完成日", format="%Y-%m-%d"), "业务:N",
                     alt.Tooltip("sum(完成量):Q", title="完成量", format=",")],
        ), height=295)
    area = query(f"""
        SELECT business_type, region_id, COUNT(*) AS orders,
               COUNT(DISTINCT courier_id) AS couriers, AVG(duration_seconds) / 60.0 AS minutes
        FROM dwd_orders WHERE {predicate}
        GROUP BY business_type, region_id ORDER BY orders DESC, business_type, region_id LIMIT 12
    """, args)
    area = display_business(area).assign(片区=lambda f: f.业务 + " · " + f.region_id.astype(str))
    with st.container(border=True):
        st.subheader("高单量作业片区")
        st.caption("鼠标移到条形上查看活跃快递员和平均耗时。揽收和派件的片区编号分别统计。")
        presentation(alt.Chart(area).mark_bar(cornerRadiusEnd=4).encode(
            x=alt.X("orders:Q", title="完成量（单）"),
            y=alt.Y("片区:N", sort="-x", title=None), color=business_color(),
            tooltip=["片区:N", alt.Tooltip("orders:Q", title="完成量", format=","),
                     alt.Tooltip("couriers:Q", title="活跃快递员", format=","),
                     alt.Tooltip("minutes:Q", title="平均耗时（分钟）", format=".1f")],
        ), height=300)


def fulfillment(predicate: str, args: tuple) -> None:
    data = order_day(predicate, args)
    if data.empty:
        st.info("所选时间和业务暂无完成记录。")
        return
    col1, col2 = st.columns(2)
    with col1.container(border=True):
        st.subheader("接单至完成耗时")
        delays = query(f"""
            SELECT business_type, CASE WHEN duration_seconds < 1800 THEN 1
                   WHEN duration_seconds < 3600 THEN 2 WHEN duration_seconds < 7200 THEN 3
                   WHEN duration_seconds < 14400 THEN 4 WHEN duration_seconds < 28800 THEN 5
                   WHEN duration_seconds < 86400 THEN 6 ELSE 7 END AS bucket,
                   COUNT(*) AS orders
            FROM dwd_orders WHERE {predicate} AND duration_seconds IS NOT NULL
            GROUP BY business_type, bucket
        """, args)
        names = ["<30 分", "30–60 分", "1–2 小时", "2–4 小时", "4–8 小时", "8–24 小时", "≥24 小时"]
        delays = display_business(delays).assign(耗时=lambda f: f.bucket.map(dict(enumerate(names, 1))))
        if not delays.empty:
            presentation(alt.Chart(delays).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
                x=alt.X("耗时:N", sort=names, title=None, axis=alt.Axis(labelAngle=-28)),
                y=alt.Y("orders:Q", title="完成量（单）"), color=business_color(),
                tooltip=["耗时:N", "业务:N", alt.Tooltip("orders:Q", title="订单数", format=",")],
            ), height=220)
        st.caption("只统计接单、完成时间有效且先后顺序正常的记录。")
    with col2.container(border=True):
        st.subheader("揽收预约结果")
        summary = pd.DataFrame({"结果": ["提前", "窗内", "晚于预约"], "单量": [
            int(data[name].sum()) for name in ("appointment_early", "appointment_in_window", "appointment_late")
        ]})
        if summary.单量.sum():
            summary["占比"] = summary.单量 / summary.单量.sum()
            presentation(alt.Chart(summary).mark_bar(cornerRadiusEnd=4, size=33).encode(
                y=alt.Y("结果:N", sort=["提前", "窗内", "晚于预约"], title=None),
                x=alt.X("占比:Q", title="有效预约中的占比", axis=alt.Axis(format="%")),
                color=alt.Color("结果:N", legend=None, scale=alt.Scale(
                    domain=["提前", "窗内", "晚于预约"], range=["#7093e8", "#128575", "#c35340"])),
                tooltip=["结果:N", alt.Tooltip("单量:Q", format=","), alt.Tooltip("占比:Q", format=".2%")],
            ), height=220)
            st.caption("恰好在预约起点或终点完成均计入窗内。")
        else:
            st.info("所选范围无有效揽收预约记录。")
    with st.container(border=True):
        st.subheader("快递员作业概览")
        focus = st.selectbox("查看业务", list(display_business(data).业务.unique()), key="courier_business")
        business = next(code for code, label in BUSINESS.items() if label == focus)
        staff = query(f"""
            SELECT courier_id, COUNT(*) AS orders, COUNT(DISTINCT completed_date) AS active_days,
                   COUNT(DISTINCT region_id) AS regions, AVG(duration_seconds) / 60.0 AS minutes
            FROM dwd_orders WHERE {predicate} AND business_type = %s
            GROUP BY courier_id ORDER BY orders DESC, courier_id LIMIT 25
        """, args + (business,))
        staff["每活跃日单量"] = (staff.orders / staff.active_days).round(1)
        staff["minutes"] = pd.to_numeric(staff.minutes).round(1)
        staff["courier_id"] = staff.courier_id.astype(str)
        st.dataframe(staff.rename(columns={"courier_id": "人员编号", "orders": "完成单量", "active_days": "活跃天数",
                                            "regions": "作业片区数", "minutes": "平均耗时（分钟）"}),
                     hide_index=True, width="stretch", height=360)
        st.caption("按完成量展示前 25 人。活跃天数为有完成记录的天数，数据未记录排班或工作时长。")


def geography(predicate: str, args: tuple) -> None:
    data = order_day(predicate, args)
    if data.empty:
        st.info("所选时间和业务暂无完成记录。")
        return
    coverage = data.groupby("business_type", as_index=False)[["orders", "gps_accept_n", "gps_finish_n"]].sum()
    coverage = display_business(coverage)
    coverage["接单定位有效率"] = coverage.apply(lambda r: ratio(r.gps_accept_n, r.orders), axis=1)
    coverage["完成定位有效率"] = coverage.apply(lambda r: ratio(r.gps_finish_n, r.orders), axis=1)
    with st.container(border=True):
        st.subheader("目标位置热度")
        grid = query(f"""
            SELECT FLOOR(target_lng * 200) / 200.0 + 0.0025 AS lng,
                   FLOOR(target_lat * 200) / 200.0 + 0.0025 AS lat, COUNT(*) AS orders
            FROM dwd_orders WHERE {predicate} AND target_lng BETWEEN 105 AND 111
                AND target_lat BETWEEN 27 AND 33
            GROUP BY lng, lat ORDER BY orders DESC, lng, lat LIMIT 1600
        """, args)
        if grid.empty:
            st.info("所选范围暂无可展示的目标坐标。")
        else:
            for name in ("lng", "lat", "orders"):
                grid[name] = pd.to_numeric(grid[name])
            center = grid.head(200)
            layer = pdk.Layer("ScatterplotLayer", grid, get_position="[lng, lat]", get_radius="min(450, 35 + sqrt(orders) * 10)",
                              radius_min_pixels=2, radius_max_pixels=24, get_fill_color=[43, 95, 215, 145], pickable=True)
            deck = pdk.Deck(layers=[layer], map_style=None,
                            initial_view_state=pdk.ViewState(latitude=float(center.lat.median()),
                                                            longitude=float(center.lng.median()), zoom=10),
                            tooltip={"text": "网格完成量：{orders} 单"})
            st.pydeck_chart(deck, width="stretch", height=370)
            st.caption("按 0.005° 网格汇总，显示单量最高的最多 1,600 格。拖动、缩放可查看分布。")
        st.dataframe(coverage[["业务", "接单定位有效率", "完成定位有效率"]], hide_index=True, width="stretch")

    left, right = st.columns(2)
    with left.container(border=True):
        st.subheader("末端区域单量")
        areas = query(f"""
            SELECT business_type, aoi_id, aoi_type, COUNT(*) AS orders,
                   COUNT(DISTINCT completed_date) AS days
            FROM dwd_orders WHERE {predicate} GROUP BY business_type, aoi_id, aoi_type
            ORDER BY orders DESC, business_type, aoi_id LIMIT 18
        """, args)
        areas = display_business(areas)
        st.dataframe(areas[["业务", "aoi_id", "aoi_type", "orders", "days"]].rename(
            columns={"aoi_id": "区域编号", "aoi_type": "类型代码", "orders": "完成量", "days": "有记录天数"}),
            hide_index=True, width="stretch", height=335)
        st.caption("区域编号来自脱敏样本。相同编号在不同业务中分别统计。")
    with right.container(border=True):
        st.subheader("完成高峰时段")
        hourly = query(f"""
            SELECT business_type, completed_hour AS hour, COUNT(*) AS orders
            FROM dwd_orders WHERE {predicate} GROUP BY business_type, completed_hour ORDER BY completed_hour
        """, args)
        presentation(alt.Chart(display_business(hourly)).mark_bar().encode(
            x=alt.X("hour:O", title="完成时刻（时）", axis=alt.Axis(labelAngle=0, labelOverlap=True)),
            y=alt.Y("orders:Q", title="完成量（单）"), color=business_color(),
            tooltip=["业务:N", alt.Tooltip("hour:O", title="小时"), alt.Tooltip("orders:Q", title="完成量", format=",")],
        ), height=295)


def trajectories() -> None:
    coverage = query("SELECT * FROM b_coverage ORDER BY business_type")
    total_points = query("SELECT SUM(trajectory_point_count) AS total FROM b_trajectory_days").iloc[0].total
    metrics = [("重庆可归属轨迹点", f"{int(total_points):,}")]
    for row in coverage.itertuples():
        metrics.append((f"{BUSINESS[row.business_type]}人员日匹配率", ratio(row.matched_courier_day_count, row.courier_day_count)))
    metric_row(metrics)
    with st.container(border=True):
        st.subheader("轨迹覆盖")
        st.caption("同一快递员在同一天计作一个人员日。覆盖率以配套订单中的人员日为分母。日期为源文件月日，年份待核实。")
        view = display_business(coverage)[["业务", "city_order_count", "courier_day_count", "matched_courier_day_count"]]
        st.dataframe(view.rename(columns={"city_order_count": "重庆配套订单", "courier_day_count": "人员日",
                                          "matched_courier_day_count": "匹配人员日"}), hide_index=True, width="stretch")
    with st.container(border=True):
        st.subheader("按时间查看样本轨迹")
        samples = query("""
            SELECT sample_id, MIN(month_day) AS day_label, MIN(business_type) AS kind, COUNT(*) AS points
            FROM b_sample_polylines GROUP BY sample_id ORDER BY day_label, sample_id
        """)
        if samples.empty:
            st.info("暂无可展示的轨迹样本。")
            return
        labels = {
            row.sample_id: f"{row.day_label} · {BUSINESS.get(row.kind, '揽派均有记录')} · {row.points} 个点 · {i}"
            for i, row in enumerate(samples.itertuples(), 1)
        }
        choice = st.selectbox("轨迹样本", list(labels), format_func=labels.get)
        points = query("""
            SELECT point_index, gps_time, relative_x, relative_y
            FROM b_sample_polylines WHERE sample_id = %s ORDER BY point_index
        """, (choice,))
        xy = alt.Chart(points).encode(
            x=alt.X("relative_x:Q", title="相对 X", scale=alt.Scale(zero=False)),
            y=alt.Y("relative_y:Q", title="相对 Y", scale=alt.Scale(zero=False)),
        )
        line = xy.mark_line(color="#9db2e0", strokeWidth=1.35).encode(order="point_index:Q")
        dots = xy.mark_circle(size=25).encode(
            color=alt.Color("point_index:Q", title="采样顺序", scale=alt.Scale(scheme="blues")),
            tooltip=[alt.Tooltip("point_index:Q", title="采样顺序"), alt.Tooltip("gps_time:N", title="采样时间")],
        )
        presentation(line + dots, height=325)
        st.caption("以样本首点为相对原点。原始坐标单位和地理转换待核实，图中线条连接相邻采样点。")


def road_paths(wkt: str) -> list[list[list[float]]]:
    if not wkt.startswith(("LINESTRING", "MULTILINESTRING")):
        return []
    paths = []
    for component in re.findall(r"\(([^()]+)\)", wkt):
        path = []
        for pair in component.split(","):
            numbers = pair.split()
            try:
                x, y = float(numbers[0]), float(numbers[1])
            except (IndexError, ValueError):
                continue
            if not (11_000_000 < x < 13_000_000 and 2_900_000 < y < 4_000_000):
                continue
            path.append([math.degrees(x / 6_378_137), math.degrees(math.atan(math.sinh(y / 6_378_137)))])
        if len(path) >= 2:
            paths.append(path)
    return paths


def roads() -> None:
    classes = query("SELECT * FROM c_road_classes ORDER BY road_count DESC, fclass")
    metric_row([
        ("道路记录", f"{int(classes.road_count.sum()):,}"),
        ("单行记录", f"{int((classes.oneway_forward + classes.oneway_reverse).sum()):,}"),
        ("桥梁标记", f"{int(classes.bridge_count.sum()):,}"),
        ("隧道标记", f"{int(classes.tunnel_count.sum()):,}"),
    ])
    classes["道路类别"] = classes.fclass.map(lambda name: ROAD_LABELS.get(name, name))
    with st.container(border=True):
        st.subheader("道路类别构成")
        presentation(alt.Chart(classes.head(12)).mark_bar(color="#3157d5", cornerRadiusEnd=4).encode(
            x=alt.X("road_count:Q", title="路段记录数"), y=alt.Y("道路类别:N", sort="-x", title=None),
            tooltip=["道路类别:N", alt.Tooltip("road_count:Q", title="记录数", format=","),
                     alt.Tooltip("bridge_count:Q", title="桥梁标记", format=","),
                     alt.Tooltip("tunnel_count:Q", title="隧道标记", format=",")],
        ), height=280)
    with st.container(border=True):
        st.subheader("路段位置预览")
        options = {row.fclass: row.道路类别 for row in classes.itertuples(index=False)}
        focus = st.selectbox("道路类别", list(options), format_func=options.get)
        lines = query("""
            SELECT fclass, geometry_wkt FROM c_roads WHERE fclass = %s
            AND MOD(source_line, 19) = 0 ORDER BY source_line LIMIT 800
        """, (focus,))
        paths = [{"path": path, "category": options[focus]} for row in lines.itertuples()
                 for path in road_paths(row.geometry_wkt)]
        if paths:
            layer = pdk.Layer("PathLayer", paths, get_path="path", get_color=[25, 118, 111, 180],
                              width_min_pixels=2, pickable=True)
            st.pydeck_chart(pdk.Deck(layers=[layer], map_style=None, tooltip={"text": "{category}"},
                                     initial_view_state=pdk.ViewState(latitude=29.58, longitude=106.56, zoom=8.4)),
                            width="stretch", height=355)
        else:
            st.info("该类别没有可预览的抽样路段。")
        st.caption("按源行抽取部分路段，按 Web Mercator 假设转换作近似预览。轨迹与道路的实际通行关系尚未验证。")


try:
    selected = st.segmented_control("分析主题", TOPICS, default=TOPICS[0], label_visibility="collapsed")
    filters = apply_filters()
    pages = {TOPICS[0]: overview, TOPICS[1]: fulfillment, TOPICS[2]: geography}
    if selected in pages:
        pages[selected](*filters)
    elif selected == TOPICS[3]:
        trajectories()
    elif selected == TOPICS[4]:
        roads()
except OSError:
    st.error("暂时无法连接重庆实验库。请检查 Doris 是否正在运行。")
