# 开发与测试

日常运行顺序见 [README](../README.md#本机启动linuxmacos)：Compose 启动 PostgreSQL，本机终端分别启动 API、Worker 和前端。数据库的默认值与 `backend/.env.example` 一致；平台初始化与服务启动均不要求模型凭据，渠道和模型由操作者之后手动配置。

## 开发配置

- `backend/.env`：本机 API、Worker 和 CLI 共用。默认使用本机 55432 端口与 `.data/files` 目录。
- `frontend/admin/.env`：仅在更改 API 地址时需要，可设置 `KEYSTONE_API_PROXY`；默认代理 `http://127.0.0.1:8000`。
- `deploy/.env`：可选，仅用于覆盖 PostgreSQL 密码、宿主端口或镜像。例如复制示例后使用 `docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait`；改密码或端口时同步修改后端数据库 URL。

`keystone init` 一次完成业务迁移、Flow 迁移、存储和管理员初始化。`KEYSTONE_INITIAL_ADMIN_PASSWORD` 仅供非交互初始化使用，本机默认交互输入；已有用户时不重置密码。模型表初始为空，`init` 不创建渠道、模型或调用记录。

## 开发检查

完成 README 的依赖安装后，从仓库根目录执行：

```bash
(cd frontend/admin && corepack pnpm exec playwright install chromium)
# 专用入口库只需建立一次，不能指向开发或业务数据库。
docker compose -f deploy/compose.yaml exec postgres createdb -U keystone keystone_test
export KEYSTONE_TEST_DATABASE_URL='postgresql+psycopg://keystone:keystone_local_only@127.0.0.1:55432/keystone_test'
backend/.venv/bin/python scripts/check.py
```

只运行非数据库检查时使用 `scripts/check.py --unit`。需要验证 Dockerfile、完整测试栈及备份恢复时使用 `scripts/check.py --deploy`；完整容器栈定义在 `tests/deploy/compose.yaml`，只供隔离测试，不参与默认开发启动。

## Worker 健康检查（排障）

Worker 和 API 在本机分别运行。API 的 `/api/v1/ready` 检查数据库和存储，Worker 日志中的 `flow_worker_ready` 表示 Flow 执行器已就绪。

需要独立探针时，可在 `backend/.env` 设置 `KEYSTONE_WORKER_READY_FILE=../.data/keystone-worker.ready`（父目录须存在），重启 Worker 后，在另一个终端检查：

```bash
cd backend
uv run --locked keystone-worker --check
```

探针只读取最近 30 秒的本地就绪标记，不连接数据库或模型渠道。不同 Worker 使用不同标记路径；本机默认未配置时探针返回失败，不影响 Worker 运行。该标记只证明维护循环近期完成且所需执行器已初始化，不保证每个流程成功。
