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
|-- 0007_worker_initial.py
`-- 0008_<namespace>_initial.py  # 新业务示例，当前尚无业务迁移
```

- 所有迁移文件直接位于 `versions/`，不建立分类或模块子目录。模块归属体现在文件名中，Worker 相关迁移使用 `worker` 前缀；不为每个模块另建 Alembic 环境、迁移分支或版本表。
- 文件名统一为 `NNNN_<module>_<change>.py`，使用小写 snake_case。四位序号在全部模块间从 `0001` 连续递增，不按模块重新编号；同一模块可包含不连续的全局序号。
- 实际执行顺序由 `revision` / `down_revision` 决定，目录遍历顺序不参与执行。文件序号必须与同一条线性链一致，由测试与脚手架检查。
- `revision` 与文件序号使用同一组四位序号字符串，例如 `"0008"`；`down_revision` 指向当前 head。模块和变更含义留在文件名中，避免长模块名超出 Alembic 版本字段长度。
- 已使用的迁移内容、revision 和顺序保持不变；后续只追加。并行开发遇到序号冲突时，在合并前重排尚未发布的迁移序号和依赖。达到 `9999` 时先调整规范，不能直接扩位导致排序变化。

全局顺序可从仓库根目录查看：

```powershell
Get-ChildItem backend/migrations/versions -File -Filter *.py | Sort-Object Name | Select-Object Name
```

## 当前迁移链

| 文件 / Revision | 模型归属 | 表 |
|---|---|---|
| `0001_identity_initial.py` / `0001` | 身份与权限 | users、roles、user_roles、role_permissions、login_sessions、personal_access_tokens |
| `0002_audit_initial.py` / `0002` | 审计 | audit_logs |
| `0003_dictionary_initial.py` / `0003` | 字典 | dictionaries、dictionary_items |
| `0004_files_initial.py` / `0004` | 文件 | files、file_grants、file_references |
| `0005_model_access_initial.py` / `0005` | 模型接入与费用核对 | model_channels、model_entries、model_invocations；含发送归属、费用核对字段与索引 |
| `0006_flow_initial.py` / `0006` | Flow | flow_runs；含协调尝试时间 |
| `0007_worker_initial.py` / `0007` | Worker 登记与运行观测 | worker_registrations、worker_heartbeats |

上表路径相对 `versions/`，通过 `down_revision` 按表中顺序连接，只有一个 head：`0007`。Alembic 使用原生平铺目录加载脚本，源码与安装后部署使用相同规则。文件多源字段合入 files，渠道凭据密文字段合入 model_access。18 张表均位于 `keystone` schema，Alembic 另维护一张版本表。

这是 0.1.0 的模块基线：每个文件直接包含该模块的完整当前结构，不再保留按版本叠加的中间步骤。model_access 初始迁移直接包含费用核对字段、发送归属字段以及包含 Openlux/Yunwu 的 provider 白名单，不需要对空库执行历史回填；flow_runs 直接包含 `reconcile_attempted_at`；worker 初始迁移直接建立固定身份登记、心跳观测及其约束和索引。新库为空，不导入旧 Worker 名单、历史心跳或旧调用归属，首次登记由 Worker 启动准入完成。

已经执行过旧语义 revision（`identity`、`audit`、`dictionary`、`files`、`model_access`、`flow`、`core`、`model_costs`、`multi_workers`、`worker_registration`）的数据库不能沿本链继续升级。这类数据库必须先核对实际结构，在停机窗口重建或迁移数据，再执行 `keystone init`；不能只修改版本号掩盖结构差异。

## 初始化

正常启动只需执行 `keystone init`，一次完成业务结构、DBOS 的 `dbos` schema、文件存储与初始管理员。重复执行保留已有数据和密码，无需分别执行迁移命令。API/Worker 启动仍只检查依赖，不自动执行 DDL。

七个基线迁移的 DDL、revision 与依赖固定，后续只追加。新环境从 `0001` 一次建到 head；旧 revision 的数据库按上节处理，不通过 stamp 跳过结构差异。

## 后续结构变更

新增结构按上述目录和命名规则继续追加同一条链。例如当前新增 notes 业务时，脚手架生成 `versions/0008_notes_initial.py`，`revision = "0008"`、`down_revision = "0007"`；之后任意模块使用下一全局序号 `0009`。部署仍通过 `keystone init` 更新，不增加启动步骤。

`scripts/new_business.py` 在写入业务代码前读取全部迁移的静态元数据，支持普通赋值和带类型注解的字面量赋值；校验平铺路径、命名、重复序号/revision、连续序号和线性依赖，不执行历史 DDL。子目录中的迁移、重复声明、动态计算的元数据、分支和额外依赖均拒绝。未通过检查时不生成业务文件。

指定 `--migrations` 时必须传迁移的 `versions` 目录；实际接入需包含完整历史，空目录仅适用于独立评审或测试。若已有 `<namespace>_initial` 迁移，不能再次生成同模块的初始建表迁移；仅补回业务代码时使用 `--no-migration`，后续结构变更手工追加。生成的 `<namespace>_<entity>` 表名最多 63 个字符。平台和已有业务的增量迁移也须通过相同命名/顺序测试。接入步骤见[业务扩展](../../docs/extending.md#2-接入实体与迁移)。

平台与业务模型由 `keystone.model_registry.load_models()` 显式加载，bootstrap 和 `env.py` 共用该入口。业务实体共享 Base/keystone schema，表名带业务前缀；模型注册不导入 API、workflow 或初始化数据。接入检查见 `tests/unit/test_architecture.py`。

Alembic 连接固定 `search_path=public`，只管理 `keystone` schema。DBOS 的表及数据由执行引擎管理，业务迁移和回滚不修改它们。

单元测试验证原生发现、安装后的目录定位、平铺文件、模块前缀、全局序号与依赖一致，并用违规历史验证脚手架在写文件前失败。集成测试使用随机隔离 PostgreSQL 库，验证逐模块建表与回滚、完整结构与 ORM 一致、重复初始化保留数据、从每个基线 revision 升级、独立脚本目录继续升级，以及新业务迁移的发现与回滚。回滚仅在测试库执行。
