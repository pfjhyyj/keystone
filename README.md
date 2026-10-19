# Keystone

从 Ether 整理的通用底座平台，采用模块化单体，支持独立业务模块以及普通步骤与 Agent 混合的 Flow。

当前已实现工程骨架、身份与审计、文件管理、字典、模型接入及 Flow：共十六张领域表。文件可同时使用多个本地/S3 存储源，每个 File 对应其中一份内容，备份关系由业务维护。管理后台已完成三批整理，覆盖账户、用户/角色、只读审计、文件授权/引用、字典、模型配置/调用费用和 Flow 发起/运行详情。真实 DBOS Worker、Pydantic AI/Harness 和文件审查示例已通过模拟渠道验证；模型发送和 Flow 默认关闭；OpenRouter live 已提供，真实账户仍待联调。

## 文档入口

- [设计索引与领域模型](docs/README.md)
- [整体架构与仓库结构](docs/architecture.md)
- [环境变量与三层配置](docs/configuration.md)
- [技术选型、Ether 复用范围与实现计划](docs/implementation-plan.md)

## 仓库结构

业务扩展的权限、审计动作与 ORM 接入方式见[业务接入规范](docs/architecture.md#55-业务权限审计与模型接入)：composition.py 显式组合目录，model_registry.py 统一加载实体，继续使用现有身份/审计服务和一条迁移链。当前只提供接入点，不新增具体业务。

- `backend/`：一个 Python 工程，API、Worker 与一次性 CLI 共用代码及依赖。
- `frontend/admin/`：独立管理后台；其他业务前端按需加入 `frontend/`。
- `deploy/`：共用后端镜像、管理后台镜像、Compose 与入口代理配置。
- `docs/`：当前设计的唯一维护位置。

具体业务放在 `backend/src/keystone/businesses/` 的对应模块，通用领域放在 `platform/`。目录随实际实现建立，不预建空模块。

后端六领域已按 [Controller / Service / Model / Schema 规范](docs/architecture.md#54-模块分层与代码规范)统一为领域内四层目录，每个持久化实体独立文件；接口及领域表保持不变，结构检查与本批验证见[实施记录](docs/implementation-plan.md#418-flow-安全错误与列表批量查询)。

## 本地开发

需要 Python 3.12、uv、Node 24（至少 24.15）、Corepack 和 PostgreSQL 17。前端通过 `packageManager` 固定 pnpm 11.19.0；依赖分别锁在 `backend/uv.lock` 和 `frontend/admin/pnpm-lock.yaml`。以下为 PowerShell 命令，起点是仓库根目录。

先启动独立开发数据库：

```powershell
Copy-Item deploy/.env.example deploy/.env
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d postgres
```

数据库仅映射到 `127.0.0.1:55432`。示例密码只用于本地开发；如果修改数据库密码，需同步修改两个环境文件中的连接串，URL 特殊字符需编码。已有 `.env` 时直接编辑，不重复覆盖。

启动 API：

```powershell
Set-Location backend
Copy-Item .env.example .env
uv sync --locked
uv run --locked keystone migrate
uv run --locked keystone migrate-flow
uv run --locked keystone init-storage
# 仅首次初始化：交互输入并确认密码，没有默认密码。
uv run --locked keystone init-admin --username admin
uv run --locked keystone check
uv run --locked keystone-api
```

另开终端启动 Worker：

```powershell
Set-Location backend
uv run --locked keystone-worker
```

Worker 执行有界文件清理；启用 Flow 后同时运行 DBOS，固定单 Worker、队列并发 2。API、Worker 均用 Ctrl+C 停止，二者不运行建表或迁移。Flow 默认关闭；开启步骤见下节。

另开终端启动管理后台：

```powershell
Set-Location frontend/admin
Copy-Item .env.example .env
pnpm install --frozen-lockfile
pnpm dev
```

打开 `http://127.0.0.1:5173/admin/`。Vite 将 `/api/` 代理到 `http://127.0.0.1:8000`；可在前端 `.env` 中设置 `KEYSTONE_API_PROXY` 改变目标，进程环境变量优先，修改后重启 Vite。浏览器始终使用同源 `/api`，生产由 Nginx 转发。使用初始化账户登录，页面提供本人改密、个人令牌、实际健康状态、用户、角色与权限、操作审计、文件库、业务字典、模型接入及 Flow。用户、角色、审计的独立路径分别为 `/admin/users`、`/admin/roles`、`/admin/audit`，导航和操作按权限显示。不预置业务数据；模型模拟模式的响应与账务数据明确标注为验证数据。

也可不配置数据库先启动 API：存活检查返回 200，就绪检查返回 503；不会自动创建数据库或回退到 SQLite。

## 模拟 Flow 验证

在 backend/.env 中设置以下配置，再启动同一份代码的 API 和 Worker：

```dotenv
KEYSTONE_FLOW_ENABLED=true
KEYSTONE_MODEL_MODE=mock
KEYSTONE_MODEL_SECRETS={"inference":"fake-local-only","billing":"fake-local-only"}
```

管理后台先新增模拟渠道：供应商选 OpenRouter，地址使用 `https://mock.invalid/v1`，推理/账务凭据引用分别填 inference、billing；新增模型 key=chat、provider_model=mock-chat。上传不超过 16 KiB 的 UTF-8 文本，在 Flow 页面选择文本文件审查、源文件和模型后提交。Worker 未启动时保持等待执行；启动后由 Agent 读取文件、进行两轮模拟请求并生成报告，可查看步骤、费用和下载结果。费用单位来自模拟响应，不代表真实账单。

同一请求重复提交返回原运行；主动重做使用“准备新的运行”。取消不撤销已经发生的外部调用。文件和模型费用分别检查权限。API/Worker 需共用数据库、文件卷、源码和 uv.lock；不要在未完成运行期间热改执行代码。详细恢复规则与预算见 [Flow 当前实现](docs/domains/flow.md#7-当前实现)。

## 健康检查与迁移

| 入口 | 作用 |
|---|---|
| `GET /api/v1/health` | 进程存活，返回服务名与版本，不代表数据库就绪 |
| `GET /api/v1/ready` | PostgreSQL、迁移版本与文件卷标识正常时返回 200，否则 503 |
| `keystone-worker --check` | 只读 Worker 本地就绪文件；最近 30 秒内更新返回 0，否则 1，不启动执行器或访问数据库 |
| `keystone migrate` | 一次性升级到最新业务迁移；重复执行安全 |
| `keystone migrate-flow` | 一次性初始化/升级 DBOS 的 dbos schema，重复执行安全 |
| `keystone revision` | 查看当前迁移版本 |
| `keystone check` | 检查连接、迁移和文件存储标识；未就绪时返回非零退出码 |
| `keystone init-storage` | 显式初始化协调卷及各本地存储源；重复执行保留卷标识，不创建 S3 bucket |
| `keystone init-admin --username admin` | 交互创建首位管理员；已有用户则拒绝执行，不重置任何密码 |

迁移链：`0001_identity` → `0002_audit` → `0003_dictionary` → `0004_files` → `0005_model_access` → `0006_flow`。六份按领域建立初始结构，文件多源字段直接包含在 0004_files 中；共十六张领域表，保留单链、单 head。全部业务表使用 `keystone` schema；DBOS 表使用独立 `dbos` schema，不纳入 Alembic 业务表迁移。迁移文件保存在 `backend/migrations/`，部署时必须随代码一起交付；源码运行按代码位置定位，非 editable 的后端镜像从 `/app` 工作目录读取随镜像交付的 `migrations/`。当前发布单位是仓库和镜像，不是独立 pip 组件。

领域归属与初始化方式见[迁移说明](backend/migrations/README.md)。项目尚未正式使用，结构整理直接并入对应初始定义，正式发布后采用增量变更。

配置分为后端、前端和部署编排三层，分别提供 `backend/.env.example`、`frontend/admin/.env.example` 和 `deploy/.env.example`，完整列出各层保留的配置及简短注释。后端健康文件默认关闭，示例路径按需取消注释；Compose 固定的内部监听和路径不作为部署环境文件的覆盖项。只提交示例，实际 `.env` 文件已被 Git 忽略；已有配置直接编辑，不重复覆盖。职责、优先级及参数说明见[环境变量与三层配置](docs/configuration.md)。

后端使用 `KEYSTONE_` 前缀，从进程环境或当前工作目录的 `.env` 读取；环境变量优先，API、Worker、CLI 均从 `backend/` 启动。Compose 部署只读取显式传入的部署环境文件，通过服务的 `environment` 声明注入后端配置。数据库 URL 使用 `postgresql+psycopg://…`。Windows 的 API、Worker 和 CLI 沿用 Ether 的 Selector 事件循环以兼容 psycopg。

## 容器运行

从仓库根目录执行，先按上文准备 `deploy/.env`：

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yaml config -q
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build -d --wait --wait-timeout 90
docker compose --env-file deploy/.env -f deploy/compose.yaml ps -a
```

访问 `http://127.0.0.1:8080/admin/`；`/api/` 由同一 Nginx 转发。启动时准备 PostgreSQL、migrate、flow-migrate 与 storage-init，全部成功后启动 API/Worker，等待 API 健康后再启动 admin。API 和 Worker 使用同一镜像、不同命令。首次部署还需交互执行管理员初始化，见[部署手册](docs/deployment.md#1-首次启动)。迁移失败时不会启动依赖它的进程；Worker 已有独立文件探针及 Compose healthcheck，检查范围和本机配置见[Worker 健康检查](docs/deployment.md#12-worker-最小健康检查)，不代表每个 Flow 均可成功。

部署新代码时先停止旧进程，再执行迁移并启动新版本，避免旧代码与新表结构同时运行：

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yaml stop admin api worker
docker compose --env-file deploy/.env -f deploy/compose.yaml build
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm migrate
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm flow-migrate
# 只有迁移成功后才执行下一条。
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
```

停止使用 `docker compose --env-file deploy/.env -f deploy/compose.yaml down`，保留数据库及文件卷；不要附加 `-v`，除非明确要清空开发数据。API/Worker 共享 file_data 卷和 /data/files 路径。备份/恢复应一起保留数据库、文件内容及 .keystone-storage 标识。

2026-10-18 已通过真实镜像构建、Nginx/Chromium、容器冷启动、联合备份恢复及保留卷重建验收，三个渠道均使用模拟传输。备份采用停写后的完整 PostgreSQL 导出与文件卷归档，并保留原镜像和运行配置；恢复到新的隔离项目，操作见[部署与恢复手册](docs/deployment.md)。Compose 可配置三个宿主端口和三个镜像引用，便于恢复到独立端口及原镜像 ID。

当前 Compose 为单宿主本地部署配置。生产 TLS、扩容时的统一限流、异地加密备份设施与真实渠道需要在目标环境另行验收。API ready 不代表 Worker 在线，也不逐个验证历史文件。部署验收可从仓库根目录重复运行：

```powershell
backend/.venv/Scripts/python.exe tests/deploy/verify.py
```

脚本构建真实镜像，通过随机项目、独立卷和 Chromium 检查九组场景；需要 backend 开发环境及 admin 的 Playwright 依赖。只清理本次创建的容器与卷，结果保存至 `.artifacts/deploy-verify/`，不访问开发库或真实渠道。

## 检查与测试

后端，在 `backend/` 执行：

```powershell
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -m "not postgres"
uv build
```

PostgreSQL 集成测试要求独立测试库和具备 CREATEDB 权限的测试角色。采用上述开发 Compose 时，第一次在仓库根目录建立测试入口库：

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yaml exec postgres createdb -U keystone keystone_test
```

然后在 `backend/` 执行：

```powershell
$env:KEYSTONE_TEST_DATABASE_URL='postgresql+psycopg://keystone:keystone_local_only@127.0.0.1:55432/keystone_test'
uv run --locked pytest
```

测试只接受名为 `keystone_test` 的入口库，每次创建随机命名的独立测试数据库并在结束时清理该库，不修改入口库中的表；未设置变量时跳过数据库测试。测试覆盖空库读取不建表、重复迁移、版本不匹配、事务回滚、错误脱敏和 Worker 资源释放。测试 URL 只用于专用测试环境。

前端，在 `frontend/admin/` 执行：

```powershell
pnpm test
pnpm build
```

分别运行前端测试和 TypeScript 检查/生产构建。首次运行真实浏览器测试前，在同一目录执行 `pnpm exec playwright install chromium`，并确保已安装前端依赖和 backend 的虚拟环境。

真实 API 浏览器测试在仓库根目录执行，沿用上述专用测试入口库变量：

```powershell
backend/.venv/Scripts/python.exe frontend/admin/e2e/run.py
```

Linux 对应 Python 路径为 `backend/.venv/bin/python`。脚本只接受数据库名为 keystone_test 的 KEYSTONE_TEST_DATABASE_URL，测试角色需要 CREATEDB 权限；它创建随机数据库与临时文件目录，执行领域及 DBOS 迁移，启动独立 API、真实 Worker 和 Vite，注入模拟渠道与假秘密，结束后停止进程并清理。Vite 使用 127.0.0.1:5175，该端口须空闲。不要直接对正在使用的后台执行 Playwright；脚本会注入隔离环境后运行 test:e2e 配置。截图和失败产物在 `.artifacts/`，不提交到仓库。

后端回归覆盖权限隔离、审计事务、六领域初始迁移与回滚、本地/S3 文件存储、模拟渠道，以及真实 DBOS Worker 的执行、取消和崩溃恢复。前端单元测试和真实 API 浏览器场景覆盖身份管理、文件授权、字典、模型配置/费用和 Flow 报告。live 测试使用本机 HTTP 夹具，不发起真实付费调用。清理与 Flow 修改完成后的统一验收见[最终代码联合回归](docs/implementation-plan.md#428-清理与-flow-最终代码联合回归)；各批范围见[实施记录](docs/implementation-plan.md#4-实现顺序与完成条件)，当前验证以本节命令为准。

## 模拟模型接入

默认 `KEYSTONE_MODEL_MODE=disabled`。本地验证时在 `backend/.env` 中设置以下值，再重启 API；Compose 则配置 `deploy/.env`：

```dotenv
KEYSTONE_MODEL_MODE=mock
KEYSTONE_MODEL_SECRETS={"mock-inference":"fake-inference","mock-billing":"fake-billing"}
```

登录后台的“模型接入”，新建渠道时选择 DeepSeek、OpenRouter 或阿里云百炼，地址填 `https://mock.invalid/v1`，推理秘密引用选 `mock-inference`，账务秘密引用选 `mock-billing`。随后新建模型（例如 key 为 `demo-chat`、上游模型为 `mock-chat`），即可发起普通/流式模拟调用、查看记录、刷新余额。实际秘密仅从服务端环境注入；后台只管理引用名。

模拟模式走真实 SDK 的发送与响应解析路径，但底层使用内存传输，不访问供应商，即使配置了真实地址也不会发出网络请求。生产环境拒绝开启 mock。模拟数据留在独立开发/测试库和文件卷；首次启用 live 使用空库、空卷并重新初始化，不能直接切换已有 mock 部署，也不能仅靠新建渠道隔离历史 Run 和费用。OpenRouter 模拟费用为 0.0012 credits，DeepSeek/阿里云单次费用保持未知；三个渠道的模拟余额均不代表真实账户。live 当前仅支持 OpenRouter。

调用、管理配置/余额、读取本人历史、读取全部历史分别受权限控制。接口、稳定调用 ID 防重发与故障规则见[模型实现约定](docs/domains/model-access.md#7-当前实现约定)。消息与输出不入库；SDK 不自动重试，发送后结果不明时保留 unknown，不据此认为未计费。

后台渠道和模型在独立弹窗编辑，渠道及可用模型选择均支持分页；配置保存后可刷新可用模型目录。余额展示最近尝试与成功采集时间，失败后的旧快照不代表当前余额。调用记录可按渠道 ID、Flow 运行 ID 和时间筛选，详情查看用量、上游编号和费用来源，补查不会重发模型。停止接收仅中断浏览器接收，不能保证上游未执行。

Flow 的“发起流程”打开独立表单，文本审查可选择源文件和模型；其他定义使用 JSON 对象输入。本页关闭重开保留相同请求键，修改输入或“准备新的运行”才创建新请求；离开页面不保存草稿。运行详情分别展示状态、步骤、费用和结果，未完成时每 3 秒刷新，关闭后停止；取消需确认并等待 Worker，终态后不再自动轮询。Flow 启用配置不证明 Worker 在线，报告下载仍检查文件权限。

## OpenRouter live 接入

live 基础能力与 OpenRouter 适配已实现，通过本机 HTTP 协议夹具和真实 DBOS Worker 验证，尚未进行真实账户联调。本次开发没有配置真实密钥或发起付费调用，示例仍默认 disabled。

真实账户的[准备清单与最小验收](docs/domains/model-access.md#79-真实账户联调准备与最小验收)已整理：先两次有界推理验收普通/流式、费用和余额，通过后再单独验收一次 Flow。该步骤表尚未执行，不表示已配置真实密钥或通过账户验收。

本地 live 联调使用独立 Compose 项目、空数据库和文件卷，从当前示例配置构建与初始化；步骤见[环境准备](docs/deployment.md#11-本地-live-联调准备环境)。

准备独立环境后，按以下顺序配置；配置本身不会发起推理：

1. 使用独立空数据库和文件卷，执行已有迁移、文件初始化和管理员初始化；mock 环境继续保留原模式。
2. 在运行环境设置 `KEYSTONE_MODEL_MODE=live`，通过 `KEYSTONE_MODEL_SECRETS` 注入推理 API Key 和可选的 Management Key，使用不同引用名，例如 `openrouter-inference`、`openrouter-billing`。真实密钥不写源码或示例文件。API/Worker 使用相同配置并一起重启。
3. 后台新建 OpenRouter 渠道，地址必须为 `https://openrouter.ai/api/v1`，分别填写上述引用名；两个 Key 必须来自同一账户。没有 Management Key 可以推理，余额显示不可用。
4. 新建模型，填写业务稳定 key 和自己账户可用的上游模型 ID；live 不预填 mock-chat，也不默认挑选收费模型。其他服务商和非官方地址不会进入 live 可用模型目录。
5. 需要实际验收时再手动发送有界请求。页面“发送真实请求”和 Flow 发起可能产生费用；查询余额/费用不重发推理。当前不自动探测渠道或重试失败请求。

账户余额来自 Management Key 的 `/credits`；响应 `usage.cost` 或补查 `/generation` 的 `total_cost` 作为单次账户费用，单位 credits。未知费用继续保持未知，停止接收不能保证停止计费。网络传输只允许官方地址及固定路径，不使用环境代理，不跟随跳转；部署出口需允许直接访问 OpenRouter HTTPS。接口、错误处理与验证边界见[OpenRouter 实现约定](docs/domains/model-access.md#78-openrouter-live-接入)。

无需真实渠道的专项测试（仓库根目录，先设置专用 `KEYSTONE_TEST_DATABASE_URL`）：

```powershell
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_model_live.py -q
```

该测试用独立协议响应和本机 HTTP socket 验证 live 路径，使用假密钥且不会连接公开供应商；生产代码不包含绕过目标校验的测试配置。DeepSeek/阿里云 live 接入后续按需求补齐，不引入 LiteLLM、价格表或新的领域模型。

## 身份与审计使用约定

普通模型调用在 API 硬退出后可能遗留 running。先停止连接同一数据库的全部 API（及自定义普通调用进程），执行以下命令完成离线收尾，再重启 API：

```shell
keystone recover-model-calls --confirm-api-stopped
```

Compose 部署对应 `docker compose --env-file deploy/.env -f deploy/compose.yaml stop api`，然后运行 `docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm --no-deps api keystone recover-model-calls --confirm-api-stopped`，成功后 `docker compose --env-file deploy/.env -f deploy/compose.yaml start api`。其他位置启动的 API 也必须停止；确认标志不会自动检测或停止进程。命令只将普通调用收尾为 unknown，保留费用与请求编号、写系统审计，不重发请求或修改 Flow 调用。细节见[离线收尾规则](docs/domains/model-access.md#76-普通-api-调用离线收尾)。

文件 API 为 /api/v1/files，字典 API 为 /api/v1/dictionaries。文件库通过 OpenDAL 同时支持多个本地/S3 源，配置示例见[多源配置](docs/configuration.md#4-同时配置本地与-s3)。上传可指定 storage_key；每个 File 对应一份内容，业务通过两个 file_id 组织备份。API/Worker 仍须共用本地协调卷；所有内容源通过 KEYSTONE_FILE_STORAGES 显式配置，本地协调目录单独设置 KEYSTONE_FILE_COORDINATION_ROOT，使用前显式执行 keystone init-storage。默认单文件上限 16 MiB，有界内存上传，不开放大文件分片或直传。文件删除返回 202，由 Worker 清理后转为 deleted。具体请求、功能权限与业务引用接入见[文件实现约定](docs/domains/files.md#7-当前实现约定)；字典只停用不删除，业务保存 DictionaryItem.id，见[字典实现约定](docs/domains/dictionary.md#7-当前实现约定)。

文件库的“文件授权”和“查看引用”要求 file:manage 及文件管理权；可按已知文件 ID 打开，不要求先取得 inspect。授权分别控制查看信息、读取内容和管理，无用户/角色目录权限时可填写已知对象 ID。内置管理员也不能自动读取其他人的文件内容；引用只读，解除需回到所属业务。字典页面把字典与选项分别编辑，保留说明和排序；停用父字典不会修改子选项自身的启用状态。

本地接口文档位于 `http://127.0.0.1:8000/docs`。登录与会话接口为 `/api/v1/auth/login`、`/auth/me`、`/auth/logout`、`/auth/password`，个人令牌接口为 `/api/v1/auth/tokens`；用户、角色、权限目录和审计分别为 `/api/v1/users`、`/roles`、`/permissions`、`/audit-logs`。完整权限与接口表见[身份文档](docs/domains/identity.md#71-当前-api)。浏览器管理请求使用 Cookie 和 CSRF 保护；脚本使用签发时只显示一次的 `Authorization: Bearer <PAT>`。PAT 不能继续签发令牌或用于本人改密。

创建用户和重置密码后必须首次改密；改密会撤销全部会话，PAT 独立管理。停用账户会同时撤销会话和 PAT，重新启用不恢复旧凭据。用户不删除，普通角色被使用时不能删除；最后一位启用管理员受并发保护。

默认会话 8 小时，PAT 最长 90 天，密码 8–128 字符，不要求字符组合复杂度。相关配置为 `KEYSTONE_SESSION_HOURS`、`KEYSTONE_PAT_MAX_DAYS`、`KEYSTONE_BROWSER_ORIGINS`。Origin 必须精确到协议、主机和端口且不含尾斜杠；开发默认支持 localhost/127.0.0.1 的 5173 与 8080。生产需 `KEYSTONE_ENV=production` 和明确的 HTTPS origins，例如 `KEYSTONE_BROWSER_ORIGINS=["https://keystone.example.com"]`；此时 Cookie 自动启用 Secure 与 __Host- 名称。

Compose 首次部署后执行 `docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm api keystone init-admin --username admin` 初始化账户。密码通过隐藏的交互输入提供，不写命令参数、源码或示例环境文件。`init-admin` 不是恢复工具；已有用户时不会覆盖身份数据。

审计日志无编辑/删除 API，成功变更与日志同事务；默认查询最近 30 天，单次最多 366 天、100 条。暂不自动清理会话、PAT 或审计历史；FileGrant 角色引用保护已实现，FlowRun 使用限制性外键保留原 PAT，不自动清理 Run、凭据或模型记录。

Flow 测试另覆盖真实 Worker/Agent 执行、三处崩溃恢复、单实例互斥、有序停止、取消与费用事实保存。就绪接口检查数据库、业务迁移及文件卷，不证明 DBOS Worker 已在线；确认 Flow 执行需查看 Worker 的 flow_worker_ready 日志及实际运行状态。
