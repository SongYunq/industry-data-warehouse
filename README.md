# H&M 快时尚零售数据仓库

最后更新：2026-11-17（Asia/Shanghai）

本项目围绕 Kaggle H&M Personalized Fashion Recommendations 竞赛数据建设课程数据仓库。本阶段使用 Pixi、Python、Apache Doris、Streamlit 和 Docker Compose。Metabase 的正式接入与交付列为下一阶段任务，状态为待实现，届时配套启用 PostgreSQL。

## 当前进度

H&M 已确定为当前数据源，首批三张 CSV 已完成 SHA-256、结构和首轮数据质量盘点：交易 31,788,324 行、客户 1,371,980 行、商品 105,542 行；交易日期覆盖 2018-09-20 至 2020-09-22。当前看板以商品分类、款式排行、颜色与图案和商品描述词为主，另提供客户复购、线上与线下对比及文件核验。项目规定渠道 1=线下、2=线上，客户字段 FN 不纳入业务分析。W05 概念、W06 逻辑设计已扩展；Doris 已完成 25 个月分区的全量装载与核对，单月重载验证通过。Streamlit 已接入分析库，是本阶段的看板入口。详细依据见 [数据质量盘点](docs/附件-数据质量盘点.csv) 和[工作进度](docs/00-工作内容与进度.md)。原始数据位于 Git 忽略目录，不纳入版本库。

此前的 Airbnb 原始数据、数据工具和香港试点设计已归档保留。根据历史试点记录，香港数据曾部分装载到 Doris 的 `hk_airbnb_pilot`；本次迁移没有删除或重建该库。本机复核历史库仍有 10 张表，共享服务的 5 个具名数据卷仍存在。归档位置见 [Airbnb 历史数据与证据](docs/archive/inside_airbnb/README.md)。

W13 已形成汇报 PPT 初稿，材料入口、报告章节和录屏彩排路径见 [答辩与交付](docs/11-答辩与交付.md)。

## 常用命令

- `pixi run data-fetch`：从 Kaggle 获取三张核心 CSV。
- `pixi run data-check`：核对文件字节数和 SHA-256。
- `pixi run data-inspect`：盘点字段、记录数、日期范围和异常行。
- `pixi run data-load`：建表并全量装载 H&M。
- `pixi run analytics-install`：安装 Doris 分析视图。
- `pixi run dashboard`：启动 Streamlit 分析看板。
- `docker compose --env-file .env -f deploy/compose.yaml up -d doris-fe doris-be`：启动本阶段所需的 Doris 容器。
- `pixi run services-status`：查看容器和 Doris 节点状态。

数据清单与结构盘点见 [H&M 数据源选取](docs/12-数据源选取.md) 和 [部署与运行](docs/10-部署与运行.md)。原始数据和凭据不纳入 Git；课程原始材料保留在 `docs/课程原始文档/`。协作规则见 [AGENTS.md](AGENTS.md)。
