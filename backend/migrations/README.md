# 数据库迁移

Keystone 保留一条 Alembic 迁移链，初始迁移按领域划分。编号表示执行顺序，领域名说明归属。

| Revision / 文件 | 领域 | 负责的表 |
|---|---|---|
| `0001_identity` | 身份与权限 | users、roles、user_roles、role_permissions、login_sessions、personal_access_tokens |
| `0002_audit` | 审计 | audit_logs |
| `0003_dictionary` | 字典 | dictionaries、dictionary_items |
| `0004_files` | 文件 | files、file_grants、file_references |
| `0005_model_access` | 模型接入 | model_channels、model_entries、model_invocations |
| `0006_flow` | Flow | flow_runs |

文件均在 versions/ 下，名称为 `<revision>.py`，从 identity 连到 flow，只有一个 head。每份初始迁移完整列出本领域的字段、约束、索引及逆序回滚；不导入运行时 ORM 模型或调用 create_all，避免模型后续修改影响历史迁移。审计字段在各表中显式声明，不用公共字段函数或动态建表循环隐藏结构。

所有业务表位于 keystone schema。`keystone migrate` 显式创建命名空间并升级到 head；API/Worker 只检查版本，不自动迁移。DBOS 使用独立 dbos schema，仍由 `keystone migrate-flow` 管理，业务迁移及回滚不修改 DBOS 表。

## 后续变更

业务接入测试在干净子进程中先捕获 load_models 注册结果，再扫描业务模型文件，防止测试导入掩盖漏注册；同时检查 models/__init__.py 导出、单实体文件、schema、表名前缀及反向依赖。执行 `pytest tests/test_business_models.py` 即可检查，无需数据库或容器。扫描只存在于测试中，迁移仍显式导入；临时测试模型不进入业务目录或迁移链。

模型加载隔离测试检查平台表集合完整、共享 metadata/schema，并且不引入 HTTP 或执行器；不限制总表数。新增业务表可进入同一 metadata，仍由上述业务模型检查约束，不能通过替换或漏掉平台表来满足检查。

平台与业务 ORM 统一由 `keystone.model_registry.load_models()` 显式加载，bootstrap 和 migrations/env.py 共用此入口。新增业务时，在自己的 models/__init__.py 导出实体，并在 load_models 中增加明确导入；不导入 API、workflow 或初始化数据。业务实体共享 Base/keystone schema，表名带业务前缀，继续追加同一迁移链，不增加独立迁移目录或版本表。

Alembic 连接固定 search_path=public，所有领域表仍显式位于 keystone。这样即使登录角色也叫 keystone，元数据反射仍保留表和外键的 schema，避免自动比较把已有结构误判为变化；schema 过滤仍只包含 keystone，不管理 public 或 dbos 的表。接入测试用临时实体验证 Alembic 识别新增表、建表后差异归零；这不新增生产实体或迁移。

当前项目尚未正式使用，结构整理直接并入对应领域的初始迁移。正式发布后，已发布或共享环境已使用的迁移保持不变；模型调整应追加编号递增的迁移，revision 与文件名一致，down_revision 指向当前 head。不重新生成整套初始迁移，也不为每个领域维护分支 head。确需跨领域原子调整时，在同一份增量迁移中写明涉及领域与依赖，不强行拆开事务。

新增迁移需核对对应[领域模型](../../docs/README.md)、外键顺序和回滚行为。测试使用随机隔离 PostgreSQL 库，验证迁移结果与 ORM 一致、重复升级、逐领域回滚与重新升级，以及 DBOS 数据保持不变。回滚测试只在测试库执行。

## 文件多源初始结构

文件多源直接包含在 `0004_files`：创建 files 时同时定义非空的 storage_key，并保留 storage_fingerprint、object_key。storage_key 由上传服务根据显式的源配置填写，ORM 和数据库都没有默认源值。共六份初始迁移、单条链、head=0006_flow；新库运行 keystone migrate 即可建立完整初始结构。
