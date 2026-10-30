# 数据库结构与迁移

迁移平铺在 `versions/`，共用一条 Alembic 线性链和 `keystone.alembic_version`。每份迁移显式声明字段、约束和索引，不导入运行时 ORM，也不调用 `create_all`。

## 当前基线

| 文件 / Revision | 模块 | 表 |
|---|---|---|
| `0001_identity_initial.py` / `0001` | 身份与权限 | users、roles、user_roles、role_permissions、login_sessions、personal_access_tokens |
| `0002_audit_initial.py` / `0002` | 审计 | audit_logs |
| `0003_dictionary_initial.py` / `0003` | 字典 | dictionaries、dictionary_items |
| `0004_files_initial.py` / `0004` | 文件 | files、file_grants、file_references |
| `0005_model_access_initial.py` / `0005` | 模型接入与费用 | model_channels、model_entries、model_invocations、model_dispatch_scopes |
| `0006_flow_initial.py` / `0006` | Flow | flow_runs、flow_run_budgets、flow_budget_claims |
| `0007_worker_initial.py` / `0007` | Worker | worker_registrations、worker_heartbeats |

新增 `0008_model_access_volcengine.py` / `0008` 放开 `model_channels` 的火山方舟 provider 约束，不改动已有渠道与调用记录。回退前须先处理火山方舟渠道及其引用；存在该 provider 时回退会由约束检查拒绝。

`0009_model_access_workspace.py` / `0009` 为渠道增加可空的百炼业务空间 ID；历史渠道保持空值，使用既有共享域名查询模型列表。回退仅移除列表查询配置，不改动推理、凭据和费用事实。

路径相对 `versions/`。`0001.down_revision = None`，之后依次连接，唯一 head 为 `0009`；共七个模块基线和一份后续迁移、21 张领域表，另有版本表。

模型接入基线包含渠道凭据密文、操作声明（含 `text.embed`）、调用归属、费用与回执、受限结果证据、发送间隔/冷却表、NewAPI 渠道类型、可修改的模型显示名称，以及 Run 关联调用表达式索引。显示名称为空时按固定 key 展示；改名不改变 key、选择或历史调用。条目 `operations` 默认仅 `text.generate`，调用 `operation` 默认 `text.generate`、`result_context` 可空，不按模型名推断媒体能力。Flow 基线包含快照、协调字段、累计预算和稳定预留；没有预算的 Run 不自动回填。Worker 基线包含固定身份登记、心跳及其约束和索引。

## 初始化与已有数据库

新库执行 `keystone init`，一次建立完整业务结构、DBOS 的 `dbos` schema、文件存储与初始管理员。相同迁移链上重复执行保留已有数据和密码。API/Worker 启动只检查结构，不执行 DDL。

当前基线从 `0001` 重新编号。合并前的数据库不能仅凭相同 revision 认定结构兼容，须核对完整结构后按下述方式处理；本次整理不提供旧链的自动转换。仍带操作枚举白名单的同号旧库也不兼容；当前操作校验边界见[模型操作](../../docs/domains/model-access.md#61-操作与固定选择)。旧链的 `0010`～`0012` 及旧语义 revision（`identity`、`audit`、`dictionary`、`files`、`model_access`、`flow`、`core`、`model_costs`、`multi_workers`、`worker_registration`）均不能由当前链解析，须按完整结构核对。旧 embedding revision `0008` 与当前火山方舟迁移、旧链 `0009` 与当前模型列表业务空间迁移均同号但不同义，不能只凭版本号判断兼容；旧库仍按停写备份、新库初始化及数据校验流程处理。

这些旧库需要在停写窗口备份并核对结构：无保留需求时可另建新库初始化；需保留数据时，先在新库初始化当前结构，再按实际结构迁移所需数据并校验，不能直接导入旧版本表或用 `stamp` 跳过差异。用户/PAT、密文及其加密密钥、文件源、调用原状态、金额、回执和 Flow 快照须保持原义；旧模型能力不按名称推断，没有预算的历史 Run 不补造预算。在途执行先由原制品收尾，DBOS 与业务数据按[备份恢复](../../docs/backup-restore.md)联合保留，不通过删除执行记录绕过恢复。

Alembic 连接固定 `search_path=public`，只管理 `keystone` schema。DBOS 表及数据由执行引擎管理，业务迁移与回滚不修改它们。

## 后续变更与脚手架

- 文件使用 `NNNN_<module>_<change>.py` 和小写 snake_case，直接放在 `versions/`，Worker 使用 `worker` 前缀；不增加模块目录、迁移分支或版本表。
- `revision` 与文件序号使用相同四位字符串。序号从 `0001` 连续递增，`down_revision` 指向前一个 revision，实际执行由 Alembic 原生依赖决定。已发布迁移不重写、不复用 revision，后续只追加。
- 下一份新业务迁移示例为 `0010_notes_initial.py`，`revision = "0010"`、`down_revision = "0009"`。未发布的并行变更在合并前解决序号冲突；达到 `9999` 时先调整规范。

`scripts/new_business.py` 校验从 `0001` 开始的连续链，并按下一序号生成迁移；空目录从 `0001` 开始。它只读取静态字面量元数据，支持普通及带类型注解的赋值；在写业务文件前检查平铺路径、命名、重复序号/revision、序号连续性和线性依赖。子目录、重复声明、动态元数据、分支和额外依赖均拒绝，不导入或执行历史迁移。

`--migrations` 必须指向 `versions` 目录；实际接入须包含当前完整链，空目录仅用于独立评审或测试。已有 `<namespace>_initial` 时不允许重复建表，仅补回代码可用 `--no-migration`。生成表名 `<namespace>_<entity>` 最多 63 字符，接入步骤见[业务扩展](../../docs/extending.md#2-接入实体与迁移)。

平台与业务模型由 `keystone.model_registry.load_models()` 显式加载，bootstrap 与 `env.py` 共用该入口；业务实体共享 Base/keystone schema，模型注册不导入 API、workflow 或初始化数据。

验证覆盖迁移发现、安装后定位、脚手架、逐模块建表与回滚、完整结构和 ORM 一致、重复初始化、每个基线到 head 的升级、调用/费用/恢复证据与 Flow 数据保留，以及不可解析的旧 revision 拒绝且不改数据。数据库测试及回滚仅在随机隔离 PostgreSQL 库执行。
