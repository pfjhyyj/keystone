# 后端测试

测试按依赖边界分三层，避免在单元测试里启动数据库或后台执行器：

| 目录 | 用途 | 运行条件 |
|---|---|---|
| `unit/` | 配置、HTTP 契约、权限/审计、Flow 声明、迁移、架构、磁盘与进程边界 | 无需数据库 |
| `integration/` | 身份、文件、模型、Flow、Worker、计费、媒体和迁移的真实 PostgreSQL 场景 | 需要专用 `keystone_test` 数据库 |
| `support/` | 共享夹具：模型桩、S3 协议桩、本地媒体桩、测试 Flow 业务、AST 导入图 | 不作为测试用例收集 |

`support/flow_fixture/` 是测试专用 Flow 装配，复用 `keystone.examples.document_review` 的示例代码；`support/local_media_stub.py` 提供本地媒体环境。它们不属于生产包，也不进入迁移链。

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

## 组织约定

- 新测试先判断是否需要数据库：不需要放 `unit/`，需要放 `integration/` 并使用现有 `client`、`empty_database_url` 夹具。
- 跨测试的桩和辅助函数放 `support/`；不要让 `unit/` 依赖 `integration/` 的模块。
- 同一校验函数的不同输入用一次测试循环覆盖，不重复创建应用、数据库或子进程。
- Flow/Worker 的子进程测试通过 `support.*` 导入夹具；E2E 和部署验证也使用同一份 support 目录。
- 迁移测试使用随机隔离库；回滚测试只发生在测试库。
