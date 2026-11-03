# Keystone

面向单企业内部多个业务模块的模块化单体平台，提供身份与审计、文件、字典、模型接入和 Flow。业务直接以模块接入，API、Worker 与 CLI 共用一个 Python 工程和发布制品，控制台使用 Vue。

优先保持开发、部署和维护简单，默认一个 Worker，业务页面可集成到现有控制台。扩展通过明确的模块接口和显式装配进行，按实际业务与容量需求演进。平台定位、扩展边界与真实业务验证目标见[整体架构](docs/architecture.md)。

## Quickstart

需要 Python 3.12、uv、Node 24（至少 24.15）、pnpm 11.19.0 和可连接的 PostgreSQL。可以使用自己的数据库；只有通过 Docker 启动本地数据库时才需要 Docker Compose。以下使用标准 shell，每个终端从仓库根目录开始。

### 1. 配置数据库并启动 API

先准备后端配置，已有配置会保留：

```sh
cd backend
[ -f keystone.toml ] || cp keystone.toml.example keystone.toml
```

数据库选择以下一种方式：

- **使用自己的 PostgreSQL**：预先创建供 Keystone 使用的数据库和用户，确保该用户有创建 schema、执行迁移和读写数据的权限。在 `backend/keystone.toml` 中修改顶层 `database_url`，例如：

  ```toml
  database_url = "postgresql+psycopg://keystone:your_password@db.example.com:5432/keystone"
  ```

  也可通过 `KEYSTONE_DATABASE_URL` 环境变量覆盖；API、Worker 和 CLI 需使用同一数据库配置，URL 中的特殊字符需编码。详见[配置说明](docs/operations/configuration.md)。

- **通过 Docker 启动本地 PostgreSQL（可选）**：在仓库根目录执行以下命令，`keystone.toml.example` 中的数据库连接与其默认值一致：

  ```sh
  docker compose -f deploy/compose.yaml up -d --wait
  ```

数据库就绪后，在 `backend/` 执行初始化并启动 API：

```sh
uv sync --locked
uv run --locked keystone init
uv run --locked keystone-api
```

首次初始化会应用数据库迁移，并提示设置 `admin` 密码。示例配置已包含本机文件存储，不需要模型 Key；已有环境升级按[部署说明](docs/operations/deployment.md#3-更新与恢复)操作。

### 2. 另开终端启动 Worker

```sh
cd backend
uv run --locked keystone-worker
```

默认一个 Worker，文件清理、Flow 协调和自动查费自动运行。扩容、退役与故障恢复按需查阅[部署说明](docs/operations/deployment.md#4-多-worker-部署与发布)。

### 3. 另开终端启动控制台

```sh
cd frontend/console
pnpm install --frozen-lockfile
pnpm dev
```

访问 <http://127.0.0.1:5173/console/>，以 `admin` 登录。“管理总览”汇总平台异常和当前权限可见的近 30 天用量，“平台状态”查看 Worker 与依赖的完整状态。API 默认地址为 `http://127.0.0.1:8000`。

## 接入业务

按[业务接入](docs/development/business.md#快速接入)完成第一个模块、页面与测试。后台任务按[执行入口](docs/execution.md#1-选择执行入口)选择 Task 或 Flow，示例见[Flow 开发](docs/development/flow.md)。

## 模型配置（手动）

在控制台创建渠道和模型条目，按已支持协议填写凭据及操作能力。保存配置不发送模型请求；步骤与能力范围见[模型配置](docs/domains/model-access.md#51-配置)，供应商细节见[实现参考](docs/reference/model-access.md#渠道与模型列表)。

## 文档

[文档索引](docs/README.md)按设计、开发、运维和实现参考提供入口。
