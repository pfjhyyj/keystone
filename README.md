# Keystone

从 Ether 整理的模块化单体平台，提供身份与审计、文件、字典、模型接入和 Flow，共十六张领域表。API、单 Worker 与 CLI 共用一个 Python 工程和镜像，管理后台使用 Vue。

Flow 默认启用；模型发送默认关闭。启动和保存配置不会自动请求真实模型。文本审查示例串联文件、Agent、步骤、费用与报告。

## 文档入口

| 任务 | 文档 |
|---|---|
| 了解模块边界、接入业务 | [架构与业务接入](docs/architecture.md) |
| 查阅领域模型和规则 | [领域索引](docs/README.md#按领域阅读) |
| 修改配置和文件存储源 | [三层配置](docs/configuration.md) |
| 部署、发布和备份恢复 | [部署手册](docs/deployment.md) |

## 仓库结构

- `backend/`：API、Worker、CLI、业务模块与单条迁移链。
- `frontend/admin/`：管理后台，其他业务前端按需建立。
- `deploy/`：镜像、Compose 和 Nginx 配置。
- `scripts/check.py`：本地与 CI 共用的检查入口。
- `docs/`：架构、领域规则、配置和部署说明。

平台领域放在 `backend/src/keystone/platform/`，业务放在 `businesses/`。权限、审计和流程在 `composition.py` 显式装配，ORM 在 `model_registry.py` 加载。示例的装配与停用步骤见[示例流程](docs/architecture.md#56-示例流程装配与停用)。

## 本地开发

需要 Python 3.12、uv、Node 24（至少 24.15）、Corepack、PostgreSQL 17 和 Docker Compose。pnpm 固定在前端 `packageManager`，后端与前端各自维护锁文件。以下为仓库根目录起始的 PowerShell 命令；已有 `.env` 直接编辑，避免覆盖。

启动开发数据库：

```powershell
Copy-Item deploy/.env.example deploy/.env
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d postgres
```

数据库默认只映射 `127.0.0.1:55432`。示例密码仅用于本地；修改后同步两个环境文件的 URL，连接串特殊字符需编码。

启动 API：

```powershell
Set-Location backend
Copy-Item .env.example .env
uv sync --locked
uv run --locked keystone migrate
uv run --locked keystone migrate-flow
uv run --locked keystone init-storage
uv run --locked keystone init-admin --username admin
uv run --locked keystone check
uv run --locked keystone-api
```

`init-admin` 仅首次执行，交互输入密码；已有用户时拒绝，不重置密码。API 和 Worker 启动不执行建表或迁移。

另开终端启动 Worker：

```powershell
Set-Location backend
uv run --locked keystone-worker
```

另开终端启动管理后台：

```powershell
Set-Location frontend/admin
Copy-Item .env.example .env
pnpm install --frozen-lockfile
pnpm dev
```

访问 `http://127.0.0.1:5173/admin/`。Vite 将同源 `/api/` 代理到 API；代理覆盖、生产 HTTPS Origin 与秘密配置见[配置说明](docs/configuration.md)。API、Worker 均用 Ctrl+C 停止，首版只运行一个 Worker。

## 模拟模型接入

在独立开发或测试环境的 `backend/.env` 设置：

```dotenv
KEYSTONE_MODEL_MODE=mock
KEYSTONE_MODEL_SECRETS={"inference":"fake-local-only","billing":"fake-local-only"}
```

同时重启 API/Worker。在后台创建 OpenRouter 模拟渠道，地址 `https://mock.invalid/v1`，推理/账务引用分别为 `inference`、`billing`；创建模型 key=`chat`、上游模型=`mock-chat`。mock 响应和费用只用于验证。

## Flow 示例

Flow 默认启用，无需额外打开开关；旧环境若显式配置了 `KEYSTONE_FLOW_ENABLED=false`，需改为 `true`。完成上述 mock 配置后，上传不超过 16 KiB 的 UTF-8 文本，在 Flow 页面选择文本审查、源文件和模型。

Worker 执行后可查看步骤、费用与报告。重复提交相同请求返回原运行，主动重做使用“准备新的运行”；取消不撤销已发生的外部调用。执行期间保持 API/Worker 的代码、锁文件、数据库和文件卷一致。

## 容器运行

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yaml config -q
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build -d --wait --wait-timeout 90
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm api keystone init-admin --username admin
```

访问 `http://127.0.0.1:8080/admin/`。Compose 依次完成数据库、两项迁移和存储初始化后启动 API/Worker。发布须先停旧进程，备份须联合保留数据库、文件源及卷标识；完整操作见[部署与恢复](docs/deployment.md)。

| 健康入口 | 含义 |
|---|---|
| `/api/v1/health` | API 进程存活 |
| `/api/v1/ready` | 数据库、业务迁移和存储依赖就绪，不代表 Worker 在线 |
| `keystone-worker --check` | 读取最近 30 秒内更新的本地 Worker 标记；本机需显式配置健康文件路径 |

## 开发检查

安装开发依赖和 Chromium 后，在仓库根目录执行：

```powershell
# 专用入口库，只需建立一次；不能指向开发或业务数据库。
docker compose --env-file deploy/.env -f deploy/compose.yaml exec postgres createdb -U keystone keystone_test
$env:KEYSTONE_TEST_DATABASE_URL='postgresql+psycopg://keystone:keystone_local_only@127.0.0.1:55432/keystone_test'
backend/.venv/Scripts/python.exe scripts/check.py
# 包含容器构建和备份恢复：
backend/.venv/Scripts/python.exe scripts/check.py --deploy
```

完整检查缺少专用数据库会失败；只做非数据库检查时显式使用 `--unit`。Linux Python 路径为 `backend/.venv/bin/python`。

## OpenRouter live 接入

live 当前仅支持 OpenRouter。先按[部署准备](docs/deployment.md#11-本地-live-联调准备环境)建立独立空库和文件卷，设置 `KEYSTONE_MODEL_MODE=live`，通过 `KEYSTONE_MODEL_SECRETS` 注入推理 Key 与可选 Management Key；后台仅填写引用名。接口与费用口径见[OpenRouter 接入](docs/domains/model-access.md#77-openrouter-live-接入)。

模型模式和秘密必须在 API/Worker 保持一致。真实调用由用户明确提交，未知结果不自动重发；费用来自上游报告，未知不等于零。普通 API 崩溃后的遗留调用按[离线收尾](docs/domains/model-access.md#75-普通-api-调用离线收尾)处理。
