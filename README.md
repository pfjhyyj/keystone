# Keystone

从 Ether 整理的模块化单体平台，提供身份与审计、文件、字典、模型接入和 Flow。API、Worker 与 CLI 共用一个 Python 工程，控制台使用 Vue。

## 本机启动（Linux/macOS）

需要 Python 3.12、uv、Node 24（至少 24.15）、pnpm 11.19.0 和 Docker Compose。pnpm 版本与 `frontend/console/package.json` 的 `packageManager` 一致。以下各个终端均从仓库根目录开始。

### 已有旧环境：重新测试（可选）

首次使用可直接跳到第 1 步。已有旧配置、希望恢复默认本机配置时，先用 Ctrl+C 停止 API、Worker 和前端，再从仓库根目录执行：

```bash
cp backend/keystone.toml.example backend/keystone.toml
cp frontend/console/.env.example frontend/console/.env
cp deploy/.env.example deploy/.env
```

这些命令覆盖旧配置，补齐本地文件存储并移除旧的模型、应用容器参数。默认启动不需要传入 `deploy/.env`。若当前终端设置过 `KEYSTONE_*`、`POSTGRES_*` 或 `COMPOSE_*` 环境变量，先取消相应覆盖，避免它们优先于配置文件生效。后端只读取 `keystone.toml`，旧 `backend/.env` 不再生效；保留原数据时，应将原数据库连接、存储源名称和路径转写到 TOML 中。

要连测试数据一起清空，再执行下面两条命令；仅用于默认本机开发环境，会删除数据库中的用户、模型配置、Flow 记录及 `.data/files` 中的文件和模型加密密钥：

```bash
docker compose -f deploy/compose.yaml down --volumes --remove-orphans
rm -rf -- .data/files
```

数据库和文件需一起清空；保留原数据时跳过这两条命令。随后按第 1～4 步启动，空数据库会重新提示设置管理员密码。

### 1. 启动第三方依赖

```bash
docker compose -f deploy/compose.yaml up -d --wait
```

当前依赖只有 PostgreSQL 17，默认地址 `127.0.0.1:55432`，数据库和用户均为 `keystone`，本地开发密码为 `keystone_local_only`。默认值与后端示例一致，可直接使用。

### 2. 启动 API

```bash
cd backend
[ -f keystone.toml ] || cp keystone.toml.example keystone.toml
uv sync --locked
uv run --locked keystone init
uv run --locked keystone-api
```

已有 `keystone.toml` 会保留，旧文件缺少存储配置时见[初始化排障](docs/configuration.md#5-初始化时文件存储未配置)。`init` 首次交互设置管理员密码（用户名 `admin`），一次创建完整业务结构、Flow 执行结构和文件存储；重复执行保留已有数据及密码。无需单独运行迁移命令。它不配置模型，不要求模型 Key。API 默认监听 `127.0.0.1:8000`。

API、Worker 和 CLI 共用 `keystone.toml`，`KEYSTONE_*` 环境变量可覆盖对应字段；文件存储的相对路径以 TOML 所在目录为基准。配置示例和覆盖规则见[配置说明](docs/configuration.md)。

### 3. 另开终端启动 Worker

```bash
cd backend
uv run --locked keystone-worker
```

Worker 默认持续向 PostgreSQL 上报心跳，无需额外配置；控制台“平台状态”显示就绪状态与最近心跳时间。命令行探针见[Worker 健康检查](docs/development.md#worker-健康检查排障)。

### 4. 另开终端启动前端

```bash
cd frontend/console
pnpm install --frozen-lockfile
pnpm dev
```

访问 <http://127.0.0.1:5173/console/>，以 `admin` 和初始化时设置的密码登录。前端通过 Vite 代理请求本机 API。API、Worker、前端各自在终端运行，用 Ctrl+C 停止。

## 模型配置（手动）

平台可以先启动，模型在需要时单独配置。在控制台“模型接入”手动新建 OpenRouter 渠道，填写普通 API Key；再新建模型 key（如 `chat`），上游模型填写实际可用的模型 ID。需要查询账户余额时，可另外填写同一账户的 Management Key。

密钥保存后不回显；编辑时留空保留原值，填写新值即可更新。保存立即生效，API 与 Worker 无需重启，也不需要模型凭据环境变量。

所有模型调用使用真实服务，启动、初始化和保存配置都不会自动发起调用。Flow 默认启用；配置好模型后，可在 Flow 页面发起文本审查。

## 服务打包

后端的 [Dockerfile](backend/Dockerfile) 和控制台的 [Dockerfile](frontend/console/Dockerfile) 各自随工程维护，用于构建发布镜像。API、Worker 共用后端镜像，以 `keystone-api`、`keystone-worker` 命令分别运行。构建命令见[打包与发布](docs/deployment.md)。

## 文档

| 任务 | 文档 |
|---|---|
| 开发依赖、测试和 Worker 排障 | [开发说明](docs/development.md) |
| Dockerfile 打包与发布 | [打包与发布](docs/deployment.md) |
| 数据库、端口、密钥和存储配置 | [配置说明](docs/configuration.md) |
| 保存重要数据或升级前备份（可选） | [备份与恢复](docs/backup-restore.md) |
| 模块边界、代码规范与业务接入 | [架构说明](docs/architecture.md) |
| 身份、文件、模型和 Flow 规则 | [领域索引](docs/README.md#按领域阅读) |

后端位于 `backend/`，控制台位于 `frontend/console/`，各自维护 Dockerfile 与构建上下文；`deploy/compose.yaml` 只编排第三方依赖。
