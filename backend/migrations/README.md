# 数据库结构与迁移

迁移平铺在 `versions/`，共用一条 Alembic 线性链和 `keystone.alembic_version`。每份迁移显式声明字段、约束和索引，不导入运行时 ORM，也不调用 `create_all`。

## 当前基线

| 文件 / Revision | 模块 | 表 |
|---|---|---|
| `0001_identity_initial.py` / `0001` | 身份与权限 | users、roles、user_roles、role_permissions、login_sessions、personal_access_tokens |
| `0002_audit_initial.py` / `0002` | 审计 | audit_logs |
| `0003_dictionary_initial.py` / `0003` | 字典 | dictionaries、dictionary_items |
| `0004_files_initial.py` / `0004` | 文件 | files、file_references |
| `0005_model_access_initial.py` / `0005` | 模型接入与费用 | model_channels、model_entries、model_invocations、model_dispatch_scopes |
| `0006_flow_initial.py` / `0006` | Flow | flow_runs |
| `0007_worker_initial.py` / `0007` | Worker | worker_registrations、worker_heartbeats |

七份迁移依序创建 20 张领域表，唯一 head 为 `0007`。Files 基线直接创建可空的 `completed_at` 和回收索引。模型接入基线直接包含火山方舟、百炼业务空间和费用预估字段及约束。

模型接入基线包含渠道凭据密文、操作声明（含 `text.embed`）、调用归属、费用与回执、受限结果证据、发送间隔/冷却表、NewAPI 渠道类型、可修改的模型显示名称，以及 Run 关联调用表达式索引。显示名称为空时按固定 key 展示；改名不改变 key、选择或历史调用。条目 `operations` 默认仅 `text.generate`，调用 `operation` 默认 `text.generate`、`result_context` 可空，不按模型名推断媒体能力。Flow 基线只保存运行快照、状态与协调字段，调用及用量统计使用实际模型记录。Worker 基线包含固定身份登记、心跳及其约束和索引。

## 初始化与已有数据库

新库执行 `keystone init`，一次建立完整业务结构、DBOS 的 `dbos` schema、文件存储与初始管理员。相同迁移链上重复执行保留已有数据和密码。API 和 Worker 的数据库就绪检查确认连通性和 Alembic head。

合并后的基线用于新库初始化。沿用旧库前需核对实际结构，同号 revision 只表示版本记录。

旧库先停写并备份，在途执行由原制品收尾；旧格式记录保留在原库和原制品中。新库不提供旧快照、摘要或无归属调用的兼容恢复。需要迁入的基础业务数据按当前结构转换和校验，不直接导入旧执行记录。DBOS 与业务数据的联合保留见[备份恢复](../../docs/operations/backup-restore.md)。

Alembic 连接固定 `search_path=public`，只管理 `keystone` schema。DBOS 表及数据由执行引擎管理，业务迁移与回滚不修改它们。

## 后续变更与脚手架

- 文件使用 `NNNN_<module>_<change>.py` 和小写 snake_case，直接放在 `versions/`，Worker 使用 `worker` 前缀；不增加模块目录、迁移分支或版本表。
- `revision` 与文件序号使用相同四位字符串。序号从 `0001` 连续递增，`down_revision` 指向前一个 revision，实际执行由 Alembic 原生依赖决定。已发布迁移不重写、不复用 revision，后续只追加。
- 下一份新业务迁移示例为 `0008_notes_initial.py`，`revision = "0008"`、`down_revision = "0007"`。未发布的并行变更在合并前解决序号冲突；达到 `9999` 时先调整规范。

`scripts/new_business.py` 校验从 `0001` 开始的连续链，并按下一序号生成迁移；空目录从 `0001` 开始。它只读取静态字面量元数据，支持普通及带类型注解的赋值；在写业务文件前检查平铺路径、命名、重复序号/revision、序号连续性和线性依赖。子目录、重复声明、动态元数据、分支和额外依赖均拒绝，不导入或执行历史迁移。

`--migrations` 必须指向 `versions` 目录；实际接入须包含当前完整链，空目录仅用于独立评审或测试。已有 `<namespace>_initial` 时不允许重复建表，仅补回代码可用 `--no-migration`。生成表名 `<namespace>_<entity>` 最多 63 字符，接入步骤见[业务扩展](../../docs/development/business.md#接入实体与迁移)。

平台与业务模型由 `keystone.model_registry.load_models()` 显式加载，bootstrap 与 `env.py` 共用该入口；业务实体共享 Base/keystone schema，模型注册不导入 API、workflow 或初始化数据。

验证覆盖迁移发现、安装后定位、脚手架、逐模块建表与回滚、完整结构和 ORM 一致、重复初始化、从最早基线到 head 的升级、调用/费用/恢复证据与 Flow 数据保留。数据库测试及回滚仅在随机隔离 PostgreSQL 库执行。
