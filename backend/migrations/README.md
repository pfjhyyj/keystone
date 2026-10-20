# 数据库结构与迁移

初始结构按领域模型拆分，不使用数字编号。每份迁移完整声明对应领域的字段、约束和索引，不导入运行时 ORM 或调用 `create_all`，避免后续模型修改影响历史结构。

| 文件 / Revision | 模型归属 | 表 |
|---|---|---|
| `identity.py` / `identity` | 身份与权限 | users、roles、user_roles、role_permissions、login_sessions、personal_access_tokens |
| `audit.py` / `audit` | 审计 | audit_logs |
| `dictionary.py` / `dictionary` | 字典 | dictionaries、dictionary_items |
| `files.py` / `files` | 文件 | files、file_grants、file_references |
| `model_access.py` / `model_access` | 模型接入 | model_channels、model_entries、model_invocations |
| `flow.py` / `flow` | Flow | flow_runs |
| `core.py` / `core` | Core 运行观测 | worker_heartbeats |

文件均位于 `versions/`，通过 `down_revision` 按上表顺序连接，只有一个 head：`core`。文件多源字段合入 files，渠道凭据密文字段合入 model_access，不再经过历史版本转换。17 张表均位于 `keystone` schema，Alembic 另维护一张版本表。

## 初始化

正常启动只需执行 `keystone init`，一次完成业务结构、DBOS 的 `dbos` schema、文件存储与初始管理员。重复执行保留已有数据和密码，无需分别执行迁移命令。API/Worker 启动仍只检查依赖，不自动执行 DDL。

本次合并针对尚未发布的开发版本；旧链数据库须先补齐并核对实际结构，再对齐新基线，不能仅修改版本号掩盖结构差异。正式发布后不再重写已使用的迁移。

## 后续结构变更

新增结构继续追加同一条 Alembic 迁移链，按领域和变更含义命名，revision 与文件名一致，`down_revision` 指向当前 head；不为领域另建分支或版本表。部署仍通过 `keystone init` 更新，不增加启动步骤。

平台与业务模型由 `keystone.model_registry.load_models()` 显式加载，bootstrap 和 `env.py` 共用该入口。业务实体共享 Base/keystone schema，表名带业务前缀；模型注册不导入 API、workflow 或初始化数据。接入检查见 `tests/test_business_models.py`。

Alembic 连接固定 `search_path=public`，只管理 `keystone` schema。DBOS 的表及数据由执行引擎管理，业务迁移和回滚不修改它们。

测试使用随机隔离 PostgreSQL 库，验证逐领域建表与回滚、完整结构与 ORM 一致、重复初始化保留数据，以及 DBOS 数据不受影响。回滚仅在测试库执行。
