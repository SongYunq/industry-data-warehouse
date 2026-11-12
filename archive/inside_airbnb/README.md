# Inside Airbnb 历史工作区

最后更新：2026-11-12（Asia/Shanghai）

本目录保留已停止作为当前数据源的 Airbnb 专属实现和 Doris SQL。香港试点代码曾写入 Doris 专属数据库 `hk_airbnb_pilot`；根据归档进度记录，Doris 已建 10 张表，首期存在部分数据写入，因 BE 可用内存不足未完成全量装载。该数据库和共享服务卷在本次迁移中没有清理或重建。本轮只读 Docker 清点确认 Doris FE/BE 正在运行、5 个数据卷仍存在；当前库表和行数未查询。

Airbnb 原始文件保存在 `data/archive/inside_airbnb/`，相关下载、完整性和结构盘点证据保存在 `docs/archive/inside_airbnb/`。归档代码仅供追溯，不再由 Pixi 默认任务调用。
