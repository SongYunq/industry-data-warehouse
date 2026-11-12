# H&M 快时尚零售数据仓库

最后更新：2026-11-12（Asia/Shanghai）

本项目围绕 Kaggle H&M Personalized Fashion Recommendations 竞赛数据建设课程数据仓库。技术路线为 Pixi + Python、Apache Doris、Streamlit、Metabase、PostgreSQL 与 Docker Compose。

## 当前进度

H&M 已确定为当前数据源，首批三张 CSV 已完成 SHA-256、结构和首轮数据质量盘点：交易 31,788,324 行、客户 1,371,980 行、商品 105,542 行；交易日期覆盖 2018-09-20 至 2020-09-22。W04 首版商业经营分析基线已完成，用户确认商品／品类优先、客户再次购买日与渠道构成为辅；W05 概念、W06 逻辑设计已形成；W07 Doris 建表 SQL 已编写但尚未执行或验证。详细依据见 [数据质量盘点](docs/附件-数据质量盘点.csv) 和[工作进度](docs/00-工作内容与进度.md)。原始数据位于 Git 忽略目录，不纳入版本库。

此前的 Airbnb 原始数据、数据工具和香港试点设计已归档保留。根据历史试点记录，香港数据曾部分装载到 Doris 的 `hk_airbnb_pilot`；本次迁移没有删除或重建该库。只读容器清点确认 Doris FE/BE 正在运行、5 个数据卷仍存在，库表行数未查询。归档位置见 [Airbnb 历史数据与证据](docs/archive/inside_airbnb/README.md)。

## 常用命令

- `pixi run data-fetch`：从 Kaggle 获取三张核心 CSV。
- `pixi run data-check`：核对文件字节数和 SHA-256。
- `pixi run data-inspect`：盘点字段、记录数、日期范围和异常行。
- `pixi run dashboard`：打开 W03 原始文件检查页。
- `pixi run services-up`、`services-down`、`services-status`：管理共享 Doris 与 Metabase 服务。

数据清单与结构盘点见 [H&M 数据源选取](docs/12-数据源选取.md) 和 [部署与运行](docs/10-部署与运行.md)。原始数据和凭据不纳入 Git；课程原始材料保留在 `docs/课程原始文档/`。协作规则见 [AGENTS.md](AGENTS.md)。
