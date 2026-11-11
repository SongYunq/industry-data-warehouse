# Industry Data Warehouse

最后更新：2026-11-12（Asia/Shanghai）

本项目当前工作转入 V2：使用 Inside Airbnb 的香港、东京、新加坡城市公开数据，技术路线沿用已有的 Pixi + Python、Apache Doris、Streamlit + Metabase 与 Docker Compose。V2 默认数据命令已切换到新的下载、校验和结构盘点模块；业务需求仍停在 W04 之前，尚未设计 Doris 表或业务指标。

## 当前状态

已确认 V2 数据源，并下载官网近一年归档中可取得的 4 个季度快照：每城 4 期、每期 7 类文件，共 84 个文件、1,174,273,252 字节（约 1.09 GiB）。已完成压缩 CSV 完整性核查、逐期行数统计和 GeoJSON 边界核对。原始数据位于 data_v2/raw/inside_airbnb/；证据见 [下载校验清单](docs_v2/附件-数据下载校验清单.csv) 与 [数据概况](docs_v2/附件-数据概况.csv)。

V2 尚未开始需求与指标口径（W04），也未导入 Doris、设计模型、开发 ETL 或业务看板。当前 Streamlit 页面只展示原始文件和快照规模，不呈现业务指标。Pixi 中的 `data-fetch`、`data-check`、`data-inspect`、`dashboard` 指向 V2；服务编排沿用现有入口，尚未在本轮启动或验收。

## 项目入口

- [V2 文档索引](docs_v2/README.md)
- [V2 工作进度](docs_v2/00-工作内容与进度.md)
- [V2 技术栈与版本](docs_v2/01-技术栈与版本.md)
- [V2 数据来源与数据字典](docs_v2/03-数据来源与数据字典.md)
- [V2 数据源选取](docs_v2/12-数据源选取.md)

## V1 基线

原 docs/ 与菜鸟 LaDe 项目阶段文档严格保留，不随本次切换而改写。V1 原始数据、数据流水线、初始化 SQL 和技术接入页仍保留在原位置供回溯；V2 数据源模块位于 `src/inside_airbnb/`，只读数据盘点页位于 `apps/streamlit_v2/`，原始文件另存于 `data_v2/`。项目协作与目录隔离规则见 [AGENTS.md](AGENTS.md)。
