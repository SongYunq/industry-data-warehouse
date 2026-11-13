"""Publish the H&M aggregate questions and dashboard in the local Metabase."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from time import sleep

import httpx

from warehouse.config import load_settings

COLLECTION = "H&M 零售分析"
DASHBOARD = "H&M 历史购买分析"
DATABASE = "H&M 零售数据仓库"

MONTH_PARAMETERS = {
    "from_month": ("起始月", "date", "date/single"),
    "to_month": ("截止月", "date", "date/single"),
    "channel": ("渠道编码", "number", "number/="),
    "horizon": ("观察窗天数", "number", "number/="),
}
MONTH_FILTER = """
WHERE 1 = 1
[[ AND purchase_month >= DATE_TRUNC(CAST({{from_month}} AS DATE), 'month') ]]
[[ AND purchase_month <= DATE_TRUNC(CAST({{to_month}} AS DATE), 'month') ]]
"""
CHANNEL_FILTER = "[[ AND sales_channel_id = {{channel}} ]]"
HORIZON_FILTER = "[[ AND horizon_days = {{horizon}} ]]"
QUERY_HINT = "/*+ SET_VAR(parallel_pipeline_task_num=1, exec_mem_limit=2147483648) */"
JOURNEY_QUERY_HINT = (
    "/*+ SET_VAR(parallel_pipeline_task_num=1, exec_mem_limit=2684354560, "
    "enable_spill=true, enable_page_cache=false) */"
)


def translated_label(source: str, translation: str) -> str:
    return (
        f"CASE WHEN NULLIF({translation}, '') IS NULL OR {translation} = {source} "
        f"THEN {source} ELSE CONCAT({source}, '（', {translation}, '）') END"
    )


@dataclass(frozen=True)
class Question:
    name: str
    view: str
    sql: str
    display: str = "table"
    dimensions: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    date_columns: tuple[str, ...] = ()

    @property
    def variables(self) -> list[str]:
        return [key for key in MONTH_PARAMETERS if "{{" + key + "}}" in self.sql]

    @property
    def native_sql(self) -> str:
        hint = JOURNEY_QUERY_HINT if self.view == "v_category_journey" else QUERY_HINT
        return self.sql.replace("SELECT ", f"SELECT {hint} ", 1)


QUESTIONS = (
    Question("月度购买件数", "v_monthly_overview",
             "SELECT purchase_month AS 月份, item_count AS 购买件数 "
             "FROM v_monthly_overview " + MONTH_FILTER +
             "AND month_coverage = '完整月' ORDER BY purchase_month",
             "line", ("月份",), ("购买件数",), date_columns=("月份",)),
    Question("商品组购买构成", "v_monthly_product_group",
             f"SELECT {translated_label('ranked.product_group', 'labels.zh_label')} AS 商品组, "
             "ranked.items AS 购买件数 "
             "FROM (SELECT product_group, SUM(item_count) AS items "
             "FROM v_monthly_product_group " + MONTH_FILTER + CHANNEL_FILTER +
             " GROUP BY product_group ORDER BY items DESC, product_group LIMIT 20) ranked "
             "LEFT JOIN v_display_labels labels ON labels.field_name = 'product_group_name' "
             "AND labels.source_label = ranked.product_group "
             "ORDER BY ranked.items DESC, ranked.product_group",
             "bar", ("商品组",), ("购买件数",)),
    Question("颜色组购买构成", "v_monthly_colour_group",
             f"SELECT {translated_label('ranked.colour_group', 'labels.zh_label')} AS 颜色组, "
             "ranked.items AS 购买件数 "
             "FROM (SELECT colour_group, SUM(item_count) AS items "
             "FROM v_monthly_colour_group " + MONTH_FILTER + CHANNEL_FILTER +
             " GROUP BY colour_group ORDER BY items DESC, colour_group LIMIT 20) ranked "
             "LEFT JOIN v_display_labels labels ON labels.field_name = 'colour_group_name' "
             "AND labels.source_label = ranked.colour_group "
             "ORDER BY ranked.items DESC, ranked.colour_group",
             "bar", ("颜色组",), ("购买件数",)),
    Question("月度商品和客户概况", "v_monthly_overview",
             "SELECT purchase_month AS 月份, month_coverage AS 月份覆盖, item_count AS 购买件数, "
             "customer_count AS 购买客户数, style_count AS 候选款式族数, variant_count AS 变体数 "
             "FROM v_monthly_overview " + MONTH_FILTER + " ORDER BY purchase_month",
             date_columns=("月份",)),
    Question("客户第 N 个购买日后回购", "v_repurchase_stage",
             "SELECT purchase_day_no AS 购买日序号, horizon_days AS 观察窗天数, "
             "eligible_customers AS 合格客户数, repeat_customers AS 再次购买客户数, "
             "no_repeat_customers AS 窗口内未观察到回购客户数, "
             "ROUND(100 * repeat_rate, 2) AS 再次购买日百分比 "
             "FROM v_repurchase_stage WHERE 1 = 1 " + HORIZON_FILTER +
             " ORDER BY purchase_day_no, horizon_days"),
    Question("首现月份组回购", "v_first_month_repurchase",
             "SELECT first_month AS 首现月份, horizon_days AS 观察窗天数, "
             "eligible_customers AS 合格客户数, repeat_customers AS 再次购买客户数, "
             "ROUND(100 * repeat_rate, 2) AS 再次购买日百分比 "
             "FROM v_first_month_repurchase WHERE 1 = 1 " + HORIZON_FILTER +
             " ORDER BY first_month, horizon_days",
             date_columns=("首现月份",)),
    Question("历史近期性与购买频次", "v_customer_frequency_recency",
             "SELECT recency_band AS 距截止日天数, frequency_band AS 不同购买日数, "
             "customer_count AS 客户数, item_count AS 购买件数 "
             "FROM v_customer_frequency_recency ORDER BY recency_band, frequency_band"),
    Question("渠道编码月度对比", "v_monthly_channel",
             "SELECT purchase_month AS 月份, sales_channel_id AS 渠道编码, "
             "item_count AS 购买件数, customer_count AS 购买客户数 "
             "FROM v_monthly_channel " + MONTH_FILTER + CHANNEL_FILTER +
             " ORDER BY purchase_month, sales_channel_id",
             date_columns=("月份",)),
    Question("单编码与多编码购买客户", "v_channel_overlap",
             "SELECT channel_group AS 客户渠道组, customer_count AS 客户数, "
             "item_count AS 购买件数 FROM v_channel_overlap ORDER BY channel_group"),
    Question("相邻单编码购买日切换", "v_channel_switch",
             "SELECT from_channel AS 前日编码, to_channel AS 后日编码, pair_count AS 日期对数 "
             "FROM v_channel_switch ORDER BY from_channel, to_channel"),
    Question("混合编码购买日", "v_channel_day_profile",
             "SELECT channel_group AS 购买日渠道组, day_count AS 购买日数 "
             "FROM v_channel_day_profile ORDER BY channel_group"),
    Question("跨购买日商品组变化", "v_category_journey",
             f"SELECT {translated_label('ranked.from_group', 'previous_label.zh_label')} AS 前日商品组, "
             f"{translated_label('ranked.to_group', 'next_label.zh_label')} AS 后日商品组, "
             "ranked.pair_count AS 日期对数 "
             "FROM (SELECT from_group, to_group, pair_count FROM v_category_journey "
             "ORDER BY pair_count DESC, from_group, to_group LIMIT 30) ranked "
             "LEFT JOIN v_display_labels previous_label ON previous_label.field_name = 'product_group_name' "
             "AND previous_label.source_label = ranked.from_group "
             "LEFT JOIN v_display_labels next_label ON next_label.field_name = 'product_group_name' "
             "AND next_label.source_label = ranked.to_group "
             "ORDER BY ranked.pair_count DESC, ranked.from_group, ranked.to_group"),
)


def request(client: httpx.Client, method: str, path: str, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.is_error:
        raise RuntimeError(f"Metabase {method} {path} 返回 HTTP {response.status_code}")
    return response.json()


def named_id(items: list[dict], name: str) -> int | None:
    matches = [item["id"] for item in items if item["name"] == name and not item.get("archived")]
    if len(matches) > 1:
        raise ValueError(f"Metabase 中有重名对象：{name}")
    return matches[0] if matches else None


def save_question(client: httpx.Client, question: Question, database_id: int,
                  collection_id: int, card_id: int | None) -> int:
    tags = {
        key: {"id": key, "name": key, "display-name": MONTH_PARAMETERS[key][0],
              "type": MONTH_PARAMETERS[key][1], "required": False}
        for key in question.variables
    }
    visualization: dict = {}
    if question.dimensions:
        visualization = {"graph.dimensions": list(question.dimensions),
                         "graph.metrics": list(question.metrics)}
    if question.date_columns:
        visualization["column_settings"] = {
            json.dumps(["name", column], ensure_ascii=False, separators=(",", ":")): {
                "date_style": "YYYY/M/D",
                "date_separator": "-",
            }
            for column in question.date_columns
        }
    payload = {
        "name": question.name,
        "type": "question",
        "collection_id": collection_id,
        "description": "H&M 课程分析 · 数据截至 2020-09-22。指标口径见项目需求分析。",
        "dataset_query": {"database": database_id, "type": "native",
                          "native": {"query": question.native_sql, "template-tags": tags}},
        "display": question.display,
        "visualization_settings": visualization,
    }
    if card_id is None:
        return request(client, "POST", "/api/card", json=payload)["id"]
    return request(client, "PUT", f"/api/card/{card_id}", json=payload)["id"]


def publish(client: httpx.Client, database_id: int) -> tuple[int, list[int]]:
    collections = request(client, "GET", "/api/collection")
    collection_id = named_id(collections, COLLECTION)
    if collection_id is None:
        collection_id = request(client, "POST", "/api/collection", json={
            "name": COLLECTION,
            "description": "H&M 竞赛数据课程分析。请在授权的本地环境中使用。",
        })["id"]

    path = f"/api/collection/{collection_id}/items"
    objects = request(client, "GET", path, params={"limit": 200})["data"]
    dashboard_id = named_id([item for item in objects if item["model"] == "dashboard"], DASHBOARD)
    if dashboard_id is None:
        dashboard_id = request(client, "POST", "/api/dashboard", json={
            "name": DASHBOARD, "collection_id": collection_id,
            "description": "2018-09-20 至 2020-09-22。完整月趋势排除首尾部分月。"
                           "回购仅使用完整观察窗，渠道保留源编码。",
        })["id"]
    dashboard_path = f"/api/dashboard/{dashboard_id}"
    dashboard = request(client, "GET", dashboard_path)
    parameters = [item for item in dashboard.get("parameters", [])
                  if item["id"] not in MONTH_PARAMETERS]
    parameters.extend(
        {"id": key, "name": label, "slug": key, "type": filter_type}
        for key, (label, _, filter_type) in MONTH_PARAMETERS.items()
    )
    existing_tabs = [
        {key: tab[key] for key in ("id", "name", "position") if key in tab}
        for tab in dashboard.get("tabs", [])
    ]
    tabs = list(existing_tabs)
    tab_ids = []
    for name in ("商品概况", "客户回购", "渠道对比", "品类旅程"):
        tab_id = next((tab["id"] for tab in existing_tabs if tab["name"] == name), None)
        if tab_id is None:
            tab_id = -(len(tabs) + 1)
            tabs.append({"id": tab_id, "name": name, "position": len(tabs)})
        tab_ids.append(tab_id)

    existing_cards = [item for item in objects if item["model"] == "card"]
    card_ids = [
        save_question(client, question, database_id, collection_id, named_id(existing_cards, question.name))
        for question in QUESTIONS
    ]
    existing_dashcards = dashboard.get("dashcards", [])
    managed = {item["card_id"]: item for item in existing_dashcards if item["card_id"] in card_ids}
    dashcard_fields = ("id", "card_id", "dashboard_tab_id", "row", "col", "size_x", "size_y",
                       "parameter_mappings", "visualization_settings", "inline_parameters", "action_id")
    dashcards = [
        {**{key: item[key] for key in dashcard_fields if key in item},
         "series": [{"id": card["id"]} for card in (item.get("series") or [])]}
        for item in existing_dashcards if item["card_id"] not in card_ids
    ]
    for tab_id, questions, ids in zip(
        tab_ids, (QUESTIONS[:4], QUESTIONS[4:7], QUESTIONS[7:11], QUESTIONS[11:]),
        (card_ids[:4], card_ids[4:7], card_ids[7:11], card_ids[11:]), strict=True,
    ):
        for index, (question, card_id) in enumerate(zip(questions, ids, strict=True)):
            dashcards.append({
                "id": managed.get(card_id, {}).get("id", -(len(dashcards) + 1)),
                "card_id": card_id,
                "dashboard_tab_id": tab_id,
                "row": index // 2 * 6, "col": index % 2 * 12, "size_x": 12, "size_y": 6,
                "parameter_mappings": [
                    {"parameter_id": key, "card_id": card_id, "target": ["variable", ["template-tag", key]]}
                    for key in question.variables
                ],
            })
    published = request(client, "PUT", dashboard_path, json={
        "parameters": parameters, "tabs": tabs, "dashcards": dashcards,
    })
    if not set(card_ids) <= {card["card_id"] for card in published["dashcards"]}:
        raise RuntimeError("Metabase 保存后未返回全部问答卡片")
    return dashboard_id, card_ids


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-id", type=int, help="Metabase H&M 数据源编号")
    parser.add_argument("--verify", action="store_true", help="逐一执行保存的问答卡片")
    args = parser.parse_args()
    load_settings()
    api_key = os.environ.get("METABASE_API_KEY")
    if not api_key:
        parser.error("请通过本地 .env 或环境变量设置 METABASE_API_KEY")
    url = f"http://127.0.0.1:{int(os.environ.get('METABASE_PORT', '3000'))}"
    with httpx.Client(base_url=url, headers={"X-API-Key": api_key}, timeout=180, trust_env=False) as client:
        database_id = args.database_id
        if database_id is None:
            databases = request(client, "GET", "/api/database")["data"]
            database_id = named_id(databases, DATABASE)
        if database_id is None:
            password = os.environ.get("HM_METABASE_DORIS_PASSWORD")
            if not password:
                parser.error("请先在 Doris 准备 hm_metabase 只读账户及其本地密码配置")
            database_id = request(client, "POST", "/api/database", json={
                "name": DATABASE,
                "engine": "doris",
                "is_full_sync": False,
                "details": {"host": "doris-fe", "port": 9030, "dbname": "hm_retail_dw",
                            "user": "hm_metabase", "password": password, "ssl": False},
            })["id"]
        dashboard_id, card_ids = publish(client, database_id)
        print(f"{url}/dashboard/{dashboard_id} · {len(card_ids)} 张问答卡片")
        if args.verify:
            for question, card_id in zip(QUESTIONS, card_ids, strict=True):
                path = f"/api/card/{card_id}/query"
                result = request(client, "POST", path, json={})
                if "MEM_LIMIT_EXCEEDED" in str(result.get("error", "")):
                    sleep(5)
                    result = request(client, "POST", path, json={})
                if result.get("status") != "completed" or not result["data"]["rows"]:
                    raise RuntimeError(f"Metabase 查询未返回数据：{question.name}")
                print(f"{question.name} · {len(result['data']['rows'])} 行")


if __name__ == "__main__":
    main()
