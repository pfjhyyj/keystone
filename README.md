# Keystone

模块化单体平台，提供身份与审计、文件、字典、模型接入和 Flow。API、Worker 与 CLI 共用一个 Python 工程，控制台使用 Vue。

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

  也可通过 `KEYSTONE_DATABASE_URL` 环境变量覆盖；API、Worker 和 CLI 需使用同一数据库配置，URL 中的特殊字符需编码。详见[配置说明](docs/configuration.md)。

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

首次初始化会应用数据库迁移，并提示设置 `admin` 密码。示例配置已包含本机文件存储，不需要模型 Key；已有环境升级按[部署说明](docs/deployment.md#3-更新与恢复)操作。

### 2. 另开终端启动 Worker

```sh
cd backend
uv run --locked keystone-worker
```

默认一个 Worker，文件清理、Flow 协调和自动查费自动运行。双 Worker、热添加、恢复和退役见下节。

### 3. 另开终端启动控制台

```sh
cd frontend/console
pnpm install --frozen-lockfile
pnpm dev
```

访问 <http://127.0.0.1:5173/console/>，以 `admin` 登录，在“平台状态”查看 Worker。API 默认地址为 `http://127.0.0.1:8000`。

## Worker 操作示例

以下每个终端都先进入 `backend/`，使用同一宿主、同一版本、同一份配置和协调卷。示例从两个 Worker 热加到三个，再退回两个；已有环境沿用原固定身份。

**1. 首次升级。** 旧版本先停写、处理在途业务，停止全部 API/Worker 并等待退出，联合备份后切换新版本；保留原配置和协调卷。新环境直接使用上面的 Quickstart。

```sh
uv sync --locked
uv run --locked keystone init
uv run --locked keystone-worker --inspect
```

旧活动执行或检查不完整时，先按[升级核对](docs/deployment.md#4-多-worker-部署与发布)处理后再启动。

**2. 启动 API 和双 Worker。** 三个终端分别运行；Quickstart 已启动的 API/默认 Worker 可直接复用。

```sh
# 终端 1：API
uv run --locked keystone-api
# 终端 2：Worker A
KEYSTONE_WORKER_ID=keystone-worker uv run --locked keystone-worker
# 终端 3：Worker B
KEYSTONE_WORKER_ID=keystone-worker-02 uv run --locked keystone-worker
```

**3. 热添加第三个。** 保持 API、A、B 运行，在新终端启动 C：

```sh
KEYSTONE_WORKER_ID=keystone-worker-03 uv run --locked keystone-worker
```

自动登记，无需改身份清单、执行 init 或重启已有进程；共享预算不变，最多 4 个未退役身份。

**4. C 故障后按原身份恢复。** 确认旧进程退出，用相同版本、配置和协调卷重新运行上一条命令。API/A/B 继续运行；查费续查，模型请求不自动重发，无需 init。

**5. 缩容退役 C。** 停用 C 的自动重启，在 C 的终端按 Ctrl-C 并等待进程退出，再执行：

```sh
uv run --locked keystone retire-worker --worker-id keystone-worker-03
```

仍在运行或有待恢复执行/模型调用时拒绝退役，先按第 4 步恢复收尾。普通停止仍保留登记，成功退役后才减少预期数量；API/A/B 无需重启。

观察状态：`uv run --locked keystone-worker --inspect`。本例的 `workers.ready_workers` / `workers.expected_workers` 为：双 Worker `2/2`，热加后 `3/3`，C 离线后 `2/3`，恢复后 `3/3`，退役后 `2/2`；如已有其他登记，数量相应增加。状态更新需等待心跳和维护检查。

已退役身份需先执行 `uv run --locked keystone reactivate-worker --worker-id keystone-worker-03` 才能再次启动；细节见[恢复与退役](docs/deployment.md#44-固定槽位调整与故障恢复)。

## 模型配置（手动）

在控制台“模型接入”创建渠道，按页面提示填写服务商、服务地址、推理 API Key 和上游模型 ID，再创建业务使用的模型 key。服务地址由代码声明并受发送前校验，页面只列出当前可用的服务商；需要查询账户余额的渠道，可按提示另填同账户的查询凭据。

密钥加密保存，不回显；编辑时留空保留原值。模型配置保存后直接生效，无需重启 API 或 Worker。费用按渠道声明的账务能力核对；更换凭据或站点时新建渠道，未确认费用保留待查原因，不视为零。完整规则见[模型接入](docs/domains/model-access.md)。

## 服务打包

API 和 Worker 共用[后端镜像](backend/Dockerfile)，分别执行 `keystone-api` 和 `keystone-worker`；[控制台镜像](frontend/console/Dockerfile) 单独构建。`deploy/compose.yaml` 只编排 PostgreSQL，构建与运行配置见[部署说明](docs/deployment.md)。

## 文档

| 任务 | 文档 |
|---|---|
| 参数、环境变量和存储配置 | [配置说明](docs/configuration.md) |
| 测试和 Worker 排障 | [开发说明](docs/development.md) |
| 发布、扩容及故障恢复 | [部署说明](docs/deployment.md) |
| 数据与文件联合备份 | [备份与恢复](docs/backup-restore.md) |
| 架构与平台边界 | [架构说明](docs/architecture.md) |
| 新增业务、权限、迁移和 Flow | [新增业务](docs/extending.md) |
| 身份、文件、模型和 Flow | [领域索引](docs/README.md#按领域阅读) |
