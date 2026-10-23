# 数据库结构与迁移

迁移文件直接平铺在 `versions/`，文件名包含全局递增序号、模块和变更含义，共用一条 Alembic 迁移链和一张版本表。每份迁移显式声明字段、约束和索引，不导入运行时 ORM 或调用 `create_all`，避免后续模型修改影响历史结构。

## 目录与命名

```text
versions/
|-- 0001_identity_initial.py
|-- 0002_audit_initial.py
|-- 0003_dictionary_initial.py
|-- 0004_files_initial.py
|-- 0005_model_access_initial.py
|-- 0006_flow_initial.py
|-- 0007_worker_heartbeats.py
|-- 0008_model_access_cost_reconciliation.py
|-- 0009_worker_ownership.py
|-- 0010_worker_registration.py
`-- 0011_<namespace>_initial.py  # 新业务示例，当前尚无业务迁移
```

- 所有迁移文件直接位于 `versions/`，不建立分类或模块子目录。模块归属体现在文件名中，Worker 运行观测与归属迁移使用 `worker` 前缀；不为每个模块另建 Alembic 环境、迁移分支或版本表。
- 文件名统一为 `NNNN_<module>_<change>.py`，使用小写 snake_case。四位序号在全部模块间从 `0001` 连续递增，不按模块重新编号；同一模块可包含不连续的全局序号。
- 实际执行顺序由 `revision` / `down_revision` 决定，目录遍历顺序不参与执行。文件序号必须与同一条线性链一致，由测试与脚手架检查。
- 新迁移的 `revision` 使用四位序号字符串，例如 `"0011"`，`down_revision` 指向当前 head。模块和变更含义留在文件名中，避免长模块名超出 Alembic 版本字段长度。历史语义 revision 保留原值，不改成数字。
- 已使用的迁移内容、revision 和顺序保持不变；后续只追加。并行开发遇到序号冲突时，在合并前重排尚未发布的迁移序号和依赖。达到 `9999` 时先调整规范，不能直接扩位导致排序变化。

全局顺序可从仓库根目录查看：

```powershell
Get-ChildItem backend/migrations/versions -File -Filter *.py | Sort-Object Name | Select-Object Name
```

## 当前迁移链

| 文件 / Revision | 模型归属 | 表 |
|---|---|---|
| `0001_identity_initial.py` / `identity` | 身份与权限 | users、roles、user_roles、role_permissions、login_sessions、personal_access_tokens |
| `0002_audit_initial.py` / `audit` | 审计 | audit_logs |
| `0003_dictionary_initial.py` / `dictionary` | 字典 | dictionaries、dictionary_items |
| `0004_files_initial.py` / `files` | 文件 | files、file_grants、file_references |
| `0005_model_access_initial.py` / `model_access` | 模型接入 | model_channels、model_entries、model_invocations |
| `0006_flow_initial.py` / `flow` | Flow | flow_runs |
| `0007_worker_heartbeats.py` / `core` | Worker 运行观测 | worker_heartbeats |
| `0008_model_access_cost_reconciliation.py` / `model_costs` | 自动费用核对 | 扩展 model_channels、model_invocations；保留既有金额与结果 |
| `0009_worker_ownership.py` / `multi_workers` | Worker 归属与运行观测 | 扩展 model_invocations、flow_runs、worker_heartbeats；不推断历史发送归属 |
| `0010_worker_registration.py` / `worker_registration` | 固定身份登记 | 新增 worker_registrations，保留原身份与恢复依据 |

上表路径相对 `versions/`，通过 `down_revision` 按表中顺序连接，只有一个 head：`worker_registration`。Alembic 使用原生平铺目录加载脚本，源码与安装后部署使用相同规则。初始基线到历史 revision `core`，之后追加增量迁移。文件多源字段合入 files，渠道凭据密文字段合入 model_access。18 张表均位于 `keystone` schema，Alembic 另维护一张版本表。

`0009` 是历史上的跨模块原子变更，统一建立 Worker 发送归属与观测，使用 worker 模块前缀。为保留原有事务与升级行为，不将该历史迁移拆成多个模块文件。后续普通领域变更使用所属模块前缀，跨模块原子变更按主要职责命名并说明所影响的模块。

model_costs 保留旧费用与调用状态；已知费用初始化为 confirmed，原范围不明的未知费用为 unavailable/legacy_scope_unknown，不使用当前 Key 回填。回滚删除新增字段前会检查 Yunwu/Openlux 数据，存在新 provider 时拒绝降级，避免丢失或篡改业务归属。

multi_workers 增加可信发送归属、Flow 协调尝试时间以及固定 Worker/版本/共享维护观测。旧模型调用归属保持空值，不从 FlowRun 或当前 Worker 推断；无归属的遗留 Flow running 记录会阻止 Worker 启动，须在停机迁移后执行[专项离线收尾](../../docs/deployment.md#43-首次从单-worker-升级)。增量迁移不改原费用、调用结果或 DBOS 检查点；Worker 上报和恢复都不自动迁移。

worker_registration 导入每个身份的最新心跳、仍 running 模型调用的已知 Worker 归属，以及初始化时提供的旧 worker_ids。正常停止、离线和从未启动的旧成员均保留为未退役登记；迁移不猜测退役，不改变调用状态。当前实例指针与正常停止时间独立保存，心跳清理不会删除身份或恢复依据。旧清单仅在首次执行该迁移时导入，重复 init 不覆盖登记或重新激活退役身份。切换后新增 Worker 自动登记，API 动态读取成员；永久缩容走显式退役命令，不删除心跳或 DBOS 历史。

## 初始化

正常启动只需执行 `keystone init`，一次完成业务结构、DBOS 的 `dbos` schema、文件存储与初始管理员。重复执行保留已有数据和密码，无需分别执行迁移命令。API/Worker 启动仍只检查依赖，不自动执行 DDL。

本次目录与文件名调整不修改这十份历史迁移的内容、revision 或依赖。已使用上述任一 revision 的数据库可以直接继续升级，无需重建或 stamp；已经位于 head 的数据库无需执行新的业务 DDL。更早且不属于此链的历史数据库仍须先核对实际结构，不能仅修改版本号掩盖结构差异。

## 后续结构变更

新增结构按上述目录和命名规则继续追加同一条链。例如当前新增 notes 业务时，脚手架生成 `versions/0011_notes_initial.py`，`revision = "0011"`、`down_revision = "worker_registration"`；之后任意模块使用下一全局序号 `0012`。部署仍通过 `keystone init` 更新，不增加启动步骤。

`scripts/new_business.py` 在写入业务代码前读取全部迁移的静态元数据，支持普通赋值和带类型注解的字面量赋值；校验平铺路径、命名、重复序号/revision、连续序号和线性依赖，不执行历史 DDL。子目录中的迁移、重复声明、动态计算的元数据、分支和额外依赖均拒绝。未通过检查时不生成业务文件。

指定 `--migrations` 时必须传迁移的 `versions` 目录；实际接入需包含完整历史，空目录仅适用于独立评审或测试。若已有 `<namespace>_initial` 迁移，不能再次生成同模块的初始建表迁移；仅补回业务代码时使用 `--no-migration`，后续结构变更手工追加。生成的 `<namespace>_<entity>` 表名最多 63 个字符。平台和已有业务的增量迁移也须通过相同命名/顺序测试。接入步骤见[业务扩展](../../docs/extending.md#2-接入实体与迁移)。

平台与业务模型由 `keystone.model_registry.load_models()` 显式加载，bootstrap 和 `env.py` 共用该入口。业务实体共享 Base/keystone schema，表名带业务前缀；模型注册不导入 API、workflow 或初始化数据。接入检查见 `tests/unit/test_architecture.py`。

Alembic 连接固定 `search_path=public`，只管理 `keystone` schema。DBOS 的表及数据由执行引擎管理，业务迁移和回滚不修改它们。

单元测试验证原生发现、安装后的目录定位、平铺文件、模块前缀、全局序号与依赖一致，并用违规历史验证脚手架在写文件前失败。集成测试使用随机隔离 PostgreSQL 库，验证逐领域建表与回滚、完整结构与 ORM 一致、重复初始化保留数据、所有旧 revision 直接升级、降级后使用旧平铺目录继续升级，以及新业务迁移的发现与回滚。拒绝降级时验证整条降级事务回滚，版本和领域数据不发生部分变化；DBOS 数据不受影响。回滚仅在测试库执行。
