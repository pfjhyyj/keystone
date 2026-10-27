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

默认一个 Worker，文件清理、Flow 协调和自动查费自动运行。扩容、退役与故障恢复按需查阅[部署说明](docs/deployment.md#4-多-worker-部署与发布)。

### 3. 另开终端启动控制台

```sh
cd frontend/console
pnpm install --frozen-lockfile
pnpm dev
```

访问 <http://127.0.0.1:5173/console/>，以 `admin` 登录。“管理总览”汇总平台异常和当前权限可见的近 30 天用量，“平台状态”查看 Worker 与依赖的完整状态。API 默认地址为 `http://127.0.0.1:8000`。

## 接入业务

从[最小业务接入](docs/business-quickstart.md)开始：生成模块，显式装配权限、模型和路由，完成迁移，再接入控制台页面和测试。普通请求由业务 Service 负责对象授权、事务与审计，复用身份、文件和字典等公共能力。

需要持久后台处理时，再按[Flow 开发](docs/flow-development.md)装配流程；模块、权限与迁移的完整约定见[模块扩展](docs/extending.md)。

## 模型配置（手动）

在控制台“模型接入”创建渠道，按页面提示填写服务商、服务地址和推理 API Key。标准 NewAPI 选择 NewAPI，默认 `https://newapiai.com/v1`；使用 Key 查询参数和分页日志的兼容站点选择 Yunwu，包括 Openlux（`https://api.openlux.ai/v1`）和 Yunwu（`https://yunwu.ai/v1`）。API 地址可配置，支持 HTTPS 地址及 `/v1` 等路径前缀。新建模型时选择渠道，页面会自动获取上游模型列表，可搜索选择，并自动填入可修改的业务模型 key。列表查询失败时也可手动输入上游模型 ID。模型操作能力仍需按实际支持范围勾选，列表中的模型不保证支持所有操作。OpenRouter 地址固定，NewAPI 地址按配置使用并在发送前校验；系统追加 `/models` 和 `/chat/completions`，查询和调用按配置地址及系统 DNS 访问，不自动跳转或改变目标；需要查询账户余额的渠道，可按提示另填同账户的查询凭据。NewAPI 支持自动查费和额度查询；上游允许时直接读取账户剩余额度，并与当前 Key 的 Token 额度分开展示。Token 不限额仍受账户余额限制。Yunwu 类目前只接入费用核对，账户总余额可在服务商控制台查看。

“模型接入”仅管理渠道、模型和调用测试；“用量与费用”提供总览、维度分析与调用明细。可按时间、渠道、模型、用户、Flow/步骤/Agent、操作、结果和费用状态筛选，按指定单位分析真实消费，点击统计项钻取明细。筛选与分页保存在链接中；普通用户仅见本人，具备全量查看权限时可分析其他用户与系统调用。未知费用、用量缺失和各单位金额分别展示，账户余额仍在渠道配置中查看。

渠道和模型可在列表快捷改名；模型显示名称与固定 key 分开，改名不影响流程绑定。未被引用的配置可确认删除，有调用记录或流程引用时只能停用；渠道仍有模型配置时须先处理这些模型。

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
| 从脚手架接入第一个业务页面 | [最小业务接入](docs/business-quickstart.md) |
| 凭据、审计及维护任务历史保留 | [历史清理](docs/history-retention.md) |
| 身份、文件、模型和 Flow | [领域索引](docs/README.md#按领域阅读) |
