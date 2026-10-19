# Keystone

从 Ether 整理的模块化单体平台，提供身份与审计、文件、字典、模型接入和 Flow。API、Worker 与 CLI 共用一个 Python 工程，管理后台使用 Vue。

## 本机启动（Linux/macOS）

需要 Python 3.12、uv、Node 24（至少 24.15）、Corepack 和 Docker Compose。以下各个终端均从仓库根目录开始。

### 1. 启动第三方依赖

```bash
docker compose -f deploy/compose.yaml up -d --wait
```

当前依赖只有 PostgreSQL 17，默认地址 `127.0.0.1:55432`，数据库和用户均为 `keystone`，本地开发密码为 `keystone_local_only`。默认值与后端示例一致，可直接使用。

### 2. 启动 API

```bash
cd backend
[ -f .env ] || cp .env.example .env
uv sync --locked
uv run --locked keystone init
uv run --locked keystone-api
```

已有 `.env` 直接使用。`init` 首次交互设置管理员密码（用户名 `admin`），完成数据库迁移、Flow 迁移和文件存储初始化；重复执行保留已有数据及密码。它不配置模型，不要求模型 Key。API 默认监听 `127.0.0.1:8000`。

### 3. 另开终端启动 Worker

```bash
cd backend
uv run --locked keystone-worker
```

### 4. 另开终端启动前端

```bash
cd frontend/admin
corepack pnpm install --frozen-lockfile
corepack pnpm dev
```

访问 <http://127.0.0.1:5173/admin/>，以 `admin` 和初始化时设置的密码登录。前端通过 Vite 代理请求本机 API。API、Worker、前端各自在终端运行，用 Ctrl+C 停止。

## 模型配置（手动）

平台可以先启动，模型在需要时单独配置。手动在 `backend/.env` 的 `KEYSTONE_MODEL_SECRETS` 中注入真实 Key，例如：

```dotenv
KEYSTONE_MODEL_SECRETS='{"inference":"你的 OpenRouter API Key"}'
```

重启 API 和 Worker 后，在后台“模型接入”手动新建 OpenRouter 渠道，推理秘密引用选择 `inference`；再新建模型 key（如 `chat`），上游模型填写实际可用的模型 ID。账务引用可留空，需要查余额时再配置 Management Key。密钥仅由服务端环境注入，后台只保存引用名。

所有模型调用使用真实服务，启动、初始化和保存配置都不会自动发起调用。Flow 默认启用；配置好模型后，可在 Flow 页面发起文本审查。

## 服务打包

后端的 [Dockerfile](backend/Dockerfile) 和管理后台的 [Dockerfile](frontend/admin/Dockerfile) 各自随工程维护，用于构建发布镜像。API、Worker 共用后端镜像，以 `keystone-api`、`keystone-worker` 命令分别运行。构建命令见[打包与发布](docs/deployment.md)。

## 文档

| 任务 | 文档 |
|---|---|
| 开发依赖、测试和 Worker 排障 | [开发说明](docs/development.md) |
| Dockerfile 打包与发布 | [打包与发布](docs/deployment.md) |
| 数据库、端口、密钥和存储配置 | [配置说明](docs/configuration.md) |
| 保存重要数据或升级前备份（可选） | [备份与恢复](docs/backup-restore.md) |
| 模块边界、代码规范与业务接入 | [架构说明](docs/architecture.md) |
| 身份、文件、模型和 Flow 规则 | [领域索引](docs/README.md#按领域阅读) |

后端位于 `backend/`，管理后台位于 `frontend/admin/`，各自维护 Dockerfile 与构建上下文；`deploy/compose.yaml` 只编排第三方依赖。
