# Inside Airbnb 城市短租数据仓库

最后更新：2026-11-12（Asia/Shanghai）

本项目围绕 Inside Airbnb 香港、东京和新加坡公开数据开展课程数据仓库设计。技术路线采用 Pixi + Python、Apache Doris、Streamlit、Metabase、PostgreSQL 与 Docker Compose。

## 当前进度

数据源选取、三城近一年可取得的季度快照下载、SHA-256 校验和文件结构盘点已完成。共取得 12 个快照、84 个文件，原始数据约 1.09 GiB，位于 `data/raw/inside_airbnb/`。当前项目停在 W04 之前：尚未完成业务需求与指标口径、尚未设计 Doris 模型、未导入数据，业务看板也未实现。现有 Streamlit 页面仅用于检查原始文件，不计算业务指标。

详细状态见 [项目文档索引](docs/README.md) 与 [工作进度](docs/00-工作内容与进度.md)。数据范围与局限见 [数据来源与数据字典](docs/03-数据来源与数据字典.md)，逐文件证据见 [下载校验清单](docs/附件-数据下载校验清单.csv)、[数据概况](docs/附件-数据概况.csv) 和 [字段清单](docs/附件-字段清单.csv)。课程要求与报告模板保留在 `docs/课程原始文档/`。

## 常用命令

- `pixi run data-fetch`：下载清单中缺失或校验不匹配的文件。
- `pixi run data-check`：按文件大小及 SHA-256 校验本地文件。
- `pixi run data-inspect`：盘点 CSV、GZIP CSV 与 GeoJSON 结构。
- `pixi run dashboard`：启动 Streamlit 原始文件盘点页。
- `pixi run services-up`、`services-down`、`services-status`：管理 Doris 与 Metabase 服务。

原始数据与本地凭据不纳入 Git。使用前参照 `.env.example` 配置本机 `.env`。协作规则见 [AGENTS.md](AGENTS.md)。
