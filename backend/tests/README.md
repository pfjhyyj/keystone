# 后端测试

测试按依赖边界分三层，避免在单元测试里启动数据库或后台执行器：

| 目录 | 用途 | 运行条件 |
|---|---|---|
| `unit/` | 配置、HTTP 契约、权限/审计、Flow 声明、迁移、架构、磁盘与进程边界 | 无需数据库 |
| `integration/` | 身份、文件、模型、Flow、Worker、计费、媒体和迁移的真实 PostgreSQL 场景 | 需要专用 `keystone_test` 数据库 |
| `support/` | 共享夹具：模型桩、S3 协议桩、本地媒体桩、测试 Flow 业务、AST 导入图 | 不作为测试用例收集 |

`support/flow_fixture/` 是测试专用 Flow 装配，复用 `keystone.examples.document_review` 的示例代码；`support/local_media_stub.py` 提供本地媒体环境。它们不属于生产包，也不进入迁移链。

`unit/test_architecture.py` 通过 `support/module_checks.py` 递归检查平台和业务模块：根目录只允许包说明与权限/审计声明，一级职责目录固定，MVC + Service、协议适配和 DBOS 执行边界不可反向依赖。负例覆盖相对导入和嵌套执行文件；轻量 Flow 定义及模块声明另用独立进程验证不会加载 ORM/执行器。脚手架生成结果接受同一结构检查，`unit/test_catalog.py` 检查声明合并时拒绝覆盖权限或审计动作。

跨模块归属检查拒绝其他模块 Model/Adapter 导入、平台反向导入具体业务和 core 反向导入领域/组装代码；负例覆盖包别名、相对导入和嵌套文件。DBOSClient 仅在 platform/flow/adapters/dbos.py 放行；同名业务 Adapter 和其他 Flow Adapter 均拒绝。

导入例外逐条判断，负例还覆盖任务包入口、近似 definitions 名称、同文件混入合法契约、core ORM/HTTP 包装及模块反向依赖装配层。Flow Service 用独立进程验证无需导入应用装配或执行器，版本查询注入另验证应用隔离及每次授权的实时复查。

`unit/test_file_settlement.py` 验证重复取消必须等待已知的上传失败事务；`integration/test_disk_capacity_api.py` 通过延迟落库稳定触发磁盘监控取消，检查 failed 状态、原文件 ID 重试及容量释放。

integration/test_file_boundaries.py 验证空权限角色的文件授权、PAT 范围、角色撤销、停用主体及缺失主体语义和授权审计回滚，并通过公开传输接口验证独立于 Flow 的落盘、引用上传幂等及作用域资源释放。现有媒体、取消与恢复测试继续使用相同的 host 接口。

`unit/test_migrations.py` 使用 Alembic 原生发现检查文件平铺、模块前缀、全局四位序号与单一依赖链一致，并验证不同工作目录和安装后部署。`unit/test_new_business.py` 验证跨业务连续编号、类型注解元数据、表名长度，以及生成前拒绝重复初始建表、重复编号/revision、断链、合并及错误路径；`integration/test_migration_layout.py` 验证从每个基线 revision 升级、降级后由独立脚本目录继续升级、数据及 DBOS 表保留，以及生成迁移的重复升级和回滚。

## 运行

```bash
cd backend

# 日常快速检查：不连接数据库
.venv/bin/python -m pytest tests/unit

# 集成测试：使用专用测试库，不能指向开发库
export KEYSTONE_TEST_DATABASE_URL='postgresql+psycopg://keystone:keystone_local_only@127.0.0.1:55432/keystone_test'
.venv/bin/python -m pytest tests/integration --require-postgres

# 全部测试
.venv/bin/python -m pytest --require-postgres
```

仓库统一入口仍为根目录的 `backend/.venv/bin/python scripts/check.py`；只跑非数据库检查时使用 `--unit`。

数据库不可用时按入口区分：`pytest --require-postgres` 和 `scripts/check.py` 全量会在启动阶段直接报错；默认 `pytest` 会统一跳过 `postgres` 用例，unit 照常运行。`scripts/check.py --unit` 和 `pytest tests/unit` 不探测数据库。

## 组织约定

- 新测试先判断是否需要数据库：不需要放 `unit/`，需要放 `integration/` 并使用现有 `client`、`empty_database_url` 夹具。
- 跨测试的桩和辅助函数放 `support/`；不要让 `unit/` 依赖 `integration/` 的模块。
- 同一校验函数的不同输入用一次测试循环覆盖，不重复创建应用、数据库或子进程。
- Flow/Worker 的子进程测试通过 `support.*` 导入夹具；E2E 和部署验证也使用同一份 support 目录。
- 迁移测试使用随机隔离库；回滚测试只发生在测试库。
