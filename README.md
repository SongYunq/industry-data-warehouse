# H&M 快时尚零售数据仓库

最后更新：2026-11-13（Asia/Shanghai）

本项目围绕 Kaggle H&M Personalized Fashion Recommendations 竞赛数据建设课程数据仓库。技术路线为 Pixi + Python、Apache Doris、Streamlit、Metabase、PostgreSQL 与 Docker Compose。

## 当前进度

H&M 已确定为当前数据源，首批三张 CSV 已完成 SHA-256、结构和首轮数据质量盘点：交易 31,788,324 行、客户 1,371,980 行、商品 105,542 行；交易日期覆盖 2018-09-20 至 2020-09-22。W04 商业分析覆盖商品层级、款式候选、颜色与图案、头部款式集中度、客户第 N 个不同购买日后的回购与首现月份组、渠道编码和跨购买日类别变化；用户确认商品／品类优先，客户与渠道为辅。W05 概念、W06 逻辑设计已扩展；Doris 已完成 25 个月分区的全量装载与核对，单月重载验证通过。Streamlit 已接入分析库，Metabase 本机服务已就绪，应用内数据源与看板发布等待管理员 API 密钥。详细依据见 [数据质量盘点](docs/附件-数据质量盘点.csv) 和[工作进度](docs/00-工作内容与进度.md)。原始数据位于 Git 忽略目录，不纳入版本库。

此前的 Airbnb 原始数据、数据工具和香港试点设计已归档保留。根据历史试点记录，香港数据曾部分装载到 Doris 的 `hk_airbnb_pilot`；本次迁移没有删除或重建该库。本机复核历史库仍有 10 张表，共享服务的 5 个具名数据卷仍存在。归档位置见 [Airbnb 历史数据与证据](docs/archive/inside_airbnb/README.md)。

## 常用命令

- `pixi run data-fetch`：从 Kaggle 获取三张核心 CSV。
- `pixi run data-check`：核对文件字节数和 SHA-256。
- `pixi run data-inspect`：盘点字段、记录数、日期范围和异常行。
- `pixi run data-load`：建表并全量装载 H&M。
- `pixi run analytics-install`：安装 Doris 分析视图。
- `pixi run dashboard`：启动 Streamlit 分析看板。
- `pixi run metabase-publish --verify`：登记 Metabase 数据源、发布看板并验证卡片，凭据配置见部署文档。
- `pixi run services-up`、`services-down`、`services-status`：管理共享 Doris 与 Metabase 服务。

数据清单与结构盘点见 [H&M 数据源选取](docs/12-数据源选取.md) 和 [部署与运行](docs/10-部署与运行.md)。原始数据和凭据不纳入 Git；课程原始材料保留在 `docs/课程原始文档/`。协作规则见 [AGENTS.md](AGENTS.md)。
