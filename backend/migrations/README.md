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
| `model_costs.py` / `model_costs` | 自动费用核对 | 扩展 model_channels、model_invocations；保留既有金额与结果 |
| `multi_workers.py` / `multi_workers` | Worker 归属与运行观测 | 扩展 model_invocations、flow_runs、worker_heartbeats；不推断历史发送归属 |
| `worker_registration.py` / `worker_registration` | 固定身份登记 | 新增 worker_registrations，保留原身份与恢复依据 |

文件均位于 `versions/`，通过 `down_revision` 按上表顺序连接，只有一个 head：`worker_registration`。初始基线到 core，之后追加语义迁移。文件多源字段合入 files，渠道凭据密文字段合入 model_access。18 张表均位于 `keystone` schema，Alembic 另维护一张版本表。

model_costs 保留旧费用与调用状态；已知费用初始化为 confirmed，原范围不明的未知费用为 unavailable/legacy_scope_unknown，不使用当前 Key 回填。回滚删除新增字段前会检查 Yunwu/Openlux 数据，存在新 provider 时拒绝降级，避免丢失或篡改业务归属。

multi_workers 增加可信发送归属、Flow 协调尝试时间以及固定 Worker/版本/共享维护观测。旧模型调用归属保持空值，不从 FlowRun 或当前 Worker 推断；无归属的遗留 Flow running 记录会阻止 Worker 启动，须在停机迁移后执行[专项离线收尾](../../docs/deployment.md#43-首次从单-worker-升级)。增量迁移不改原费用、调用结果或 DBOS 检查点；Worker 上报和恢复都不自动迁移。

worker_registration 导入每个身份的最新心跳、仍 running 模型调用的已知 Worker 归属，以及初始化时提供的旧 worker_ids。正常停止、离线和从未启动的旧成员均保留为未退役登记；迁移不猜测退役，不改变调用状态。当前实例指针与正常停止时间独立保存，心跳清理不会删除身份或恢复依据。旧清单仅在首次执行该迁移时导入，重复 init 不覆盖登记或重新激活退役身份。切换后新增 Worker 自动登记，API 动态读取成员；永久缩容走显式退役命令，不删除心跳或 DBOS 历史。

## 初始化

正常启动只需执行 `keystone init`，一次完成业务结构、DBOS 的 `dbos` schema、文件存储与初始管理员。重复执行保留已有数据和密码，无需分别执行迁移命令。API/Worker 启动仍只检查依赖，不自动执行 DDL。

本次合并针对尚未发布的开发版本；旧链数据库须先补齐并核对实际结构，再对齐新基线，不能仅修改版本号掩盖结构差异。正式发布后不再重写已使用的迁移。

## 后续结构变更

新增结构继续追加同一条 Alembic 迁移链，按领域和变更含义命名，revision 与文件名一致，`down_revision` 指向当前 head；不为领域另建分支或版本表。部署仍通过 `keystone init` 更新，不增加启动步骤。

平台与业务模型由 `keystone.model_registry.load_models()` 显式加载，bootstrap 和 `env.py` 共用该入口。业务实体共享 Base/keystone schema，表名带业务前缀；模型注册不导入 API、workflow 或初始化数据。接入检查见 `tests/test_business_models.py`。

Alembic 连接固定 `search_path=public`，只管理 `keystone` schema。DBOS 的表及数据由执行引擎管理，业务迁移和回滚不修改它们。

测试使用随机隔离 PostgreSQL 库，验证逐领域建表与回滚、完整结构与 ORM 一致、重复初始化保留数据，以及 DBOS 数据不受影响。回滚仅在测试库执行。
