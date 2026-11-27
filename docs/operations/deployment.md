# 部署与维护

日常启动见[Quickstart](../../README.md#quickstart)。本页维护构建、发布、运维命令、Worker 运维和历史清理；deploy/compose.yaml 只启动开发用 PostgreSQL，单宿主应用栈见 [2.3 节](#23-单宿主-compose)。

[构建与初始化](#1-构建镜像) · [运维命令](#22-运维命令) · [单宿主 Compose](#23-单宿主-compose) · [S3 跨宿主](#24-s3-跨宿主部署) · [更新与恢复](#3-更新与恢复) · [Worker 管理](#4-多-worker-部署与发布) · [健康排障](#worker-健康检查排障) · [历史清理](#历史保留与清理)

## 1. 构建镜像

从仓库根目录执行，命令适用于 Linux/macOS：

```bash
docker build -t keystone-backend:local ./backend
docker build -t keystone-console:local ./frontend/console
docker build -t keystone-browser-chromium:local ./deploy/environments/browser/runtime
```

| 镜像 | Dockerfile | 运行命令 |
|---|---|---|
| 后端 API | `backend/Dockerfile` | `keystone-api`（镜像默认命令） |
| 后端 Worker | 同一个后端镜像 | `keystone-worker` |
| 控制台 | `frontend/console/Dockerfile` | Nginx（镜像默认命令） |
| 浏览器运行时 | `deploy/environments/browser/runtime/Dockerfile` | `browser-runtime`（供 OpenSandbox 创建沙箱） |

API 与 Worker 使用同一份代码、锁文件和镜像，分别启动进程。控制台镜像包含编译后的 Vue 静态文件及 `/api` 反向代理；后端镜像只含 API、Worker 与 CLI 代码。浏览器运行时镜像只在启用交互环境的浏览器类型时需要，必须预先分发到 OpenSandbox 所用的 Docker 宿主，并通过 `browser_runtime_image` 固定版本或摘要。每个工程的 `.dockerignore` 仅允许构建所需源码和配置进入上下文，不读取或打包实际 `keystone.toml`、`.env`；构建不执行初始化、不创建模型配置。容器验证使用 `tests/deploy/compose.yaml`，它继承 `deploy/stack/compose.yaml` 并改为本地构建和测试配置，不改变本机开发启动方式。

## 2. 运行配置与初始化

由实际部署环境配置应用进程、数据库连接、文件卷和入口代理；后端参数见[配置说明](configuration.md)。API/Worker 镜像以 UID 10001 运行，挂载的节点目录和本地正式文件目录须可写。控制台镜像默认通过 Docker 内部 DNS 转发到同网络中名为 `api` 的服务、端口 8000；其他部署拓扑需调整 `frontend/console/docker/nginx.conf` 后构建。

按[浏览器同源规则](../domains/identity.md#7-最小实现约定)配置入口。各级反向代理都须保留浏览器请求的原始 `Host`，包括对外域名和端口（如有），不能改成内部上游地址。Nginx 使用 `proxy_set_header Host $http_host;`，不要使用会丢失端口的 `$host`。

容器运行时将配置只读挂载到 init、API、Worker 的 `/app/keystone.toml`（可用 `KEYSTONE_CONFIG_FILE` 覆盖位置），不把真实配置或秘密写入镜像。容器内建议使用绝对挂载路径，见[单宿主配置示例](../../deploy/stack/keystone.toml.example)。

API、Worker 和 CLI 使用同一制品、PostgreSQL 数据库和公共配置。会话级 advisory lock 要求直连 PostgreSQL 或使用会话级连接池代理，不支持事务或语句级代理池。

先在实际运行环境执行 `keystone init`，再启动 API 和 Worker；初始化和启动不发起模型请求。只有 `keystone init` 写入 `keystone.deployment_binding`；API、Worker 和 `retire-worker` / `reactivate-worker` 只核验已有绑定，不认领也不迁移。

API 的 `/api/v1/health` 只报告存活：绑定缺失、不匹配或数据库不可用时仍返回 200，`/api/v1/ready` 返回 503，业务路由拒绝请求。正确初始化或数据库恢复后，只要绑定和依赖检查通过，API 无需重启即可恢复。Worker 在绑定缺失或不匹配时启动失败，见[健康探针](#worker-健康检查排障)。

### 2.1 调用失败排查

API 与 Worker 将 JSON 日志输出到标准错误，部署时由进程管理器或容器平台保留并轮转。模型失败事件为 `model_call_failed`，主动中断为 `model_call_interrupted`；按调用记录的 UUID 查询 API 和 Worker 日志，HTTP 响应头 `X-Request-ID` 对应日志顶层 `request_id`。未完成发送登记时没有持久调用记录，仍可按请求 ID 查询。

`diagnostic` 包含渠道/模型 ID、操作、`stage`、`transport_phase`、`elapsed_ms`、是否登记及上游状态/请求编号。`network_reason` 区分连接/读写超时、DNS、TLS、连接重置和协议异常；`upstream_error_code` / `upstream_error_type` 仅在上游提供已知安全代码时输出。`exception` 保留有界异常链和文件、函数、行号，不包含异常原文、源码行和局部变量。上游没有明确长度错误时，不能仅按耗时或 unknown 判定输入太长。

每个 HTTP 请求完成后记录 `http_request` JSON 日志，包含 request_id、方法、路由模板、真实响应状态码、耗时毫秒及 completed/interrupted。流式响应头已发送后失败仍记录原状态及 interrupted，不伪造 500；日志不记录实际路径参数、查询参数、请求体或凭据。

本机开发若需跨终端保留日志，可在 `backend/` 下分别启动 API 与 Worker，再从仓库根目录查询：

```bash
mkdir -p ../.artifacts/logs
uv run --locked keystone-api >>../.artifacts/logs/api.log 2>&1
uv run --locked keystone-worker >>../.artifacts/logs/worker.log 2>&1   # 另一终端
rg --fixed-strings '替换为调用记录 UUID 或 X-Request-ID' .artifacts/logs   # 仓库根目录
```

### 2.2 运维命令

在 `backend/` 以 `uv run --locked keystone <命令>` 执行，使用与应用相同的配置。失败只输出安全错误码并以 1 退出，不打印配置或驱动原始异常。

| 命令 | 参数 | 用途 |
|---|---|---|
| `init` | `[--username admin] [--display-name 管理员] [--non-interactive]` | 升级平台链与已登记业务链到各自的 head、应用 DBOS 迁移、初始化文件存储并认领或核验数据库绑定，空安装时创建初始管理员；重复执行保留已有数据、密码和卷标识。`--non-interactive` 要求空安装已设置 `KEYSTONE_INITIAL_ADMIN_PASSWORD` |
| `check` | — | 检查数据库、迁移版本、存储和部署绑定；就绪退出 0，否则 1 |
| `revision` | — | 输出数据库当前迁移 revision |
| `init-admin` | `--username NAME [--display-name NAME]` | 交互输入并确认密码创建管理员；已有任何用户时拒绝 |
| `init-storage` | — | 只初始化节点本地目录和 filesystem 源，不连接数据库，不创建 S3 bucket |
| `init-credential-key` | — | 向私有 TOML 写入随机密钥，不连接数据库，见[凭据加密密钥](configuration.md#41-凭据加密密钥) |
| `recover-model-calls` | `--confirm-api-stopped` | 停止全部 API 后收尾普通 API 调用，见[第 4.3 节](#43-api-调用的离线收尾) |
| `retire-worker` / `reactivate-worker` | `--worker-id ID` | 退役或重新启用固定 Worker 身份，见[第 4.4 节](#44-故障恢复与缩容退役) |
| `retry-file-delete` | `--file-id UUID` | 重新排队已停止自动重试的文件删除，见[文件清理积压](#文件清理积压) |
| `reset-admin-role` | — | 把内置 admin 角色恢复为全部权限并写 system 审计，用于角色被改到无人能管理身份时；已是全部权限时不改动 |
| `prune-history` | `[--apply] [--history-offset N]` | 预览或删除一批过期历史，见[手动预览与执行](#手动预览与执行) |
| `register-environment` | `--owner-id UUID --volume VOLUME [--kind chromium] [--key default]` | 把已有卷导入为指定用户、类型和已声明 key 的交互环境并写系统审计，输出环境 ID；导入环境视为已初始化。卷须预先创建，同一卷或同一用户的同一环境只能登记一次（`environment_volume_registered`）。新环境无需登记，首次使用时自动创建 |

### 2.3 单宿主 Compose

`deploy/stack/compose.yaml` 在一台宿主上运行 PostgreSQL、API、单个 Worker 和控制台，使用预构建镜像。`up` 不运行 init；控制台等待 API 就绪。

```bash
cd deploy/stack
cp .env.example .env                    # 填写镜像、POSTGRES_PASSWORD
cp keystone.toml.example keystone.toml  # 按需填写应用配置
docker compose up -d --wait postgres
docker compose run --rm init            # 首次安装与每次升级各显式执行一次
docker compose up -d --wait
```

| 项 | 约定 |
|---|---|
| 配置 | Compose 固定 `env=production`、数据库地址和监听地址。Compose 所需变量放私有 `.env`；凭据加密密钥写入私有挂载 TOML，该栈不从 `.env` 转发 `KEYSTONE_CREDENTIAL_KEY` |
| 入口 | 控制台默认只监听 127.0.0.1:8080，由宿主上的 HTTPS 反向代理对外。启用交互环境时，反向代理须对以 `/api/v1/environments` 开头的路径转发 WebSocket 升级，读超时不少于 230 秒（打开和结束会话最长等待提供方约 220 秒） |
| 卷 | `postgres_data` 为数据库，`file_data` 承载节点目录和本地正式文件，按[联合备份](backup-restore.md)处理 |
| 主机与 Worker 身份 | 容器 hostname 固定为 KEYSTONE_HOST_ID，Worker 身份固定为 KEYSTONE_WORKER_ID，重建容器不改变；增加 Worker 时按[第 4.1 节](#41-共同条件与增加实例)另加固定身份服务 |
| 停止 | Worker 的 stop_grace_period 默认 60s，须大于 worker_shutdown_seconds 并留出收尾时间 |
| 日志 | json-file，单文件 50 MB、保留 5 个 |

升级时先按[第 3 节](#3-更新与恢复)停写、排空并停止全部应用服务（`docker compose stop api worker console`），备份后更新 .env 中的镜像，再执行 `docker compose run --rm init` 和 `docker compose up -d --wait`。Compose 健康检查只在宿主内可见，unhealthy 不会自动重启容器；存活告警须由外部监控读取 `/api/v1/ready`、`keystone-worker --check` 或容器健康状态。

### 2.4 S3 跨宿主部署

1. 按[存储配置](configuration.md#4-文件存储与部署拓扑)配置相同的 S3 源和[密钥](configuration.md#41-凭据加密密钥)，不为临时文件添加 `filesystem` 源。
2. 每个节点配置独立的本地 `file_coordination_root`，运行 `uv run --locked keystone init-storage`。只在一个节点执行 `uv run --locked keystone init`；新增节点不重复初始化数据库。
3. 启动 API 和 Worker，给每个 Worker 分配[固定身份](#41-共同条件与增加实例)并核对就绪。无需 Docker。

## 3. 更新与恢复

发布前停写，在原版本排空或受控取消 Flow，同步终态并核对外部请求及文件 I/O 已收尾。停止所有节点的旧 API、Worker 和 CLI，禁用自动重启后再[联合备份](backup-restore.md)和迁移。不支持滚动升级或混跑；保留旧执行的原版本恢复路径。迁移边界见[迁移说明](../../backend/migrations/README.md#初始化与已有数据库)。

升级时可清理旧配置：顶层 TOML `browser_origins` 会被忽略，`KEYSTONE_BROWSER_ORIGINS` 不再使用。请按[入口约定](#2-运行配置与初始化)确认各级代理保留原始 `Host`。

`0011` 只建绑定表，不回填记录。首次认领不改写已有文件或 Worker 登记：

- 含 `filesystem`：保留原宿主、节点目录、正式文件和存储标识；已有未退役 Worker 登记的宿主名或节点目录标识不匹配时拒绝认领。
- 仅 S3：所有未删除文件的源名和位置指纹必须与配置的 S3 源匹配，否则拒绝认领。旧 Worker 登记不用于推断存储拓扑或限制 S3 首次认领。

媒体容量及代理时限见[配置](configuration.md#2-应用参数)，部署前可执行[媒体验收](../../backend/tests/README.md#本地媒体专项验收)。

### 3.1 初始化与绑定排障

| 现象 | 处理 |
|---|---|
| `credential_key_required` / `invalid_credential_key` | 检查私有 TOML 或 `KEYSTONE_CREDENTIAL_KEY`；新部署可用 [`init-credential-key`](configuration.md#41-凭据加密密钥)，有加密凭据时恢复原密钥 |
| 绑定缺失 | 确认连接正确的数据库并完成 `keystone init` |
| 绑定冲突 | 核对[已绑定拓扑](configuration.md#4-文件存储与部署拓扑)、原节点目录或原密钥；Worker 还须核对制品与公共配置 |
| `deployment_binding_busy` | 检查 PostgreSQL 可用性和绑定锁竞争 |
| `storage_unavailable` | 核对源配置、实际目录及权限、S3 连通性与授权；不要用空卷替代原 filesystem 目录 |

## 4. 多 Worker 部署与发布

登记、身份上限、准入与退役语义见[任务执行](../execution.md#73-多-worker恢复与发布)，本节只列操作。数量上限不是吞吐保证，部署前仍需核算资源。

### 4.1 共同条件与增加实例


worker_id 同时用作 DBOS executor_id，默认 `keystone-worker`；由部署者固定指定，不随机生成，也不因故障更名。增加实例只需在新进程指定新身份，在 `backend/` 执行：

```bash
KEYSTONE_WORKER_ID=keystone-worker-02 uv run --locked keystone-worker
```

新身份自动登记并由 API 动态发现，无需重启 API/原 Worker 或再迁移，也不提高共享预算。身份锁已占用（`worker_identity_busy`）、身份已退役（`worker_retired`）、配置冲突或超出名额（`worker_capacity_full`）均拒绝启动。排查原进程、恢复正确配置或安全退役，不要随机更换身份绕过。容器不能直接 scale 同身份服务；进程管理器按原身份重启。

SIGINT/SIGTERM 使 Worker 报告 draining 并停止领取，按 `worker_shutdown_seconds` 有界等待，运行时、请求事实和本进程资源完成退出后才释放身份锁并报告 stopped。终止宽限期须大于该值并为事实收尾/文件 I/O 留出时间；等待超时仍可能有待恢复 DBOS 记录，退出不等于业务取消或可以删除身份。

### 4.2 容量核算

队列与模型预算见[任务执行](../execution.md#72-队列预算)，调优项见[配置](configuration.md#2-应用参数)；增加 Worker 不提高全局预算，供应商 RPM/TPM 不受该并发保证。核算 API/Worker 业务池（每进程常驻 10、溢出 5）、DBOS 池、身份锁及查询/推理协调连接、运维预留，以及 CPU、内存和临时磁盘空间；不能把单进程预算乘实例数。维护计划由全部进程装配，不按身份复制，周期见[持久计划](../execution.md#4-持久计划)。

### 4.3 API 调用的离线收尾

停止全部 API 进程，并确保收尾期间不会重启后执行：

```bash
uv run --locked keystone recover-model-calls --confirm-api-stopped
```

确认标志由操作者保证停机，命令不探测进程；中断的普通调用标记为 unknown，保留原调用、费用和审计，不重发推理，可重跑。范围见[离线收尾](../reference/model-access.md#离线收尾)。Flow 调用由原身份 Worker 恢复。

### 4.4 故障恢复与缩容退役

**单实例故障**：先确认旧进程确实退出，并确保不会与恢复进程同时重启，再以原 `worker_id`、原兼容制品和公共配置启动，其他实例可保持运行。filesystem 必须留在绑定的宿主并挂载原目录；仅 S3 可以在另一台已执行 `init-storage` 的宿主恢复同一身份。心跳超时或锁空闲不能替代原进程退出确认。没有自动跨宿主接替，不以新身份代替恢复，也不自动重发未知的外部请求。身份连接丢失或启动收尾超时后仍按原身份恢复，不删除调用或审计。

**缩容退役**：

1. 停用目标服务的自动重启或副本声明，让目标 Worker 排空并等待进程退出；API 和其他 Worker 不停。
2. 执行退役并核对：

```sh
uv run --locked keystone retire-worker --worker-id keystone-worker-02
uv run --locked keystone-worker --inspect
```

退役持目标身份锁核对：没有该 executor 的在途或待恢复 DBOS 执行，也没有该身份的 running 模型调用；通过后标记退役并写系统审计，释放名额，登记和历史保留。进程仍在运行时锁忙拒绝；有归属执行返回 `worker_recovery_required`，先以原身份恢复收尾；查询不可用返回 `worker_inspection_unavailable`；未登记返回 `worker_not_registered`。已在目标状态的命令幂等，不重复写审计。

**重新启用**：先执行 reactivate-worker（重新校验兼容性和名额，不启动进程），再另行启动：

```sh
uv run --locked keystone reactivate-worker --worker-id keystone-worker-02
KEYSTONE_WORKER_ID=keystone-worker-02 uv run --locked keystone-worker
```

### 4.5 后续发布、回退与联合恢复

代码版本、公共预算、执行或存储配置变化按[第 3 节](#3-更新与恢复)停机发布，不混跑新旧版本；同版本增员/退役不走此流程。迁移后按原身份启动同一新制品，不因配置冲突换身份绕过。旧版本未完成的业务保留原制品/身份恢复路径；可重建的维护执行可按第 4.4 节退役，避免旧 PENDING 占住容量，但不删除真实业务记录、防重发记录或把未知费用当作零。费用待办不因发布重置原 Token、期限或金额。

回退前停止新写入和全部新进程，核对新版本已产生的运行及外部副作用。只有 schema、配置和检查点兼容且原版本能安全恢复时才只回退代码；否则按[联合恢复](backup-restore.md)恢复同一停写窗口的数据、密钥与制品，先用原版本与原身份核对，再考虑升级。不能按当下在线进程重建成员。真实上游费用不随数据库回滚，ready 不构成重发许可。

### 4.6 部署验收

发布前运行[完整检查](../../backend/tests/README.md)，在隔离环境验证迁移、双 Worker、取消、崩溃恢复和联合备份恢复；仅 S3 跨宿主部署另验收跨节点文件可见性、拒绝重复身份及原身份跨宿主恢复。实际部署另外验证存储吞吐、磁盘/连接预算、共享维护进度及进程管理器按原身份重启；本地模拟上游的测试结果不代表生产容量。

## Worker 健康检查（排障）

Worker 每 5 秒向 PostgreSQL 上报心跳，启动、依赖失败和排空期间也继续报告；数据库时间判断心跳与维护检查是否超过 30 秒。进程健康检查依赖、DBOS 和队列；共享维护聚合任一同版本实例实际执行的文件清理、Flow 协调及费用扫描结果，空闲实例不因未亲自扫描误报。状态语义见[健康状态](../execution.md#74-健康状态)。

探针与运行 Worker 使用同一身份环境，在 `backend/` 执行：

```bash
KEYSTONE_WORKER_ID=keystone-worker uv run --locked keystone-worker --check
uv run --locked keystone-worker --inspect
```

--check 只查询当前主机的指定身份，不启动执行器或刷新心跳；进程就绪退出 0，其余退出 1。共享维护异常单独告警，不能据此重启所有正常进程。--inspect 输出实例、共享维护及活动 DBOS 队列的身份/版本/状态计数，不读取输入输出、不启动执行器；最多读取 1000 条，truncated 或 unknown 时不能据此认定没有遗留执行，必要时通过 DBOS 公开客户端按 executor/版本核对。它也不证明外部请求已经停止。

### 状态接口

`GET /api/v1/worker/status` 要求当前有效登录身份（会话或 PAT，仍检查用户状态及强制改密），查询前后复查身份；返回总体 status、last_heartbeat_at、instances、maintenance、ready_workers、expected_workers 和 backlog，控制台每 5 秒刷新。expected_workers 按未退役登记计算，普通停止、心跳过期与历史观测清理不移除登记；未退役身份缺失、版本不同、过期或未就绪时不能被另一个 ready 实例掩盖，退役身份单列且不计入，没有登记成员时总体为 offline。公开的 health/ready 不受 Worker 观测影响。

backlog 分别报告 flow（pending 待协调 Flow，按创建时间）、files（待清理文件，按最近更新时间）和 model_costs（已到查费时间的调用，按下次应查时间）的 pending 数量、最早时间及距今秒数。文件重试会刷新更新时间，不能解释为首次请求后的累计等待。各领域独立查询，失败返回 unknown 和空值，不能当作零，也不改变 Worker 健康结论。

### 文件清理积压

`backlog.files.pending` 只统计未达自动尝试上限的删除受理、超过 1 小时未更新的 uploading，以及配置了宽限期后超期的无引用 active 文件；failed 上传和 S3 残留复核不计入，规则见[文件清理与残留](../domains/files.md#33-清理与残留)。`exhausted` 是连续 3 次删除失败、已停止自动重试的 deleting 文件数，控制台总览显示“N 项已停止自动重试，需人工处理”。

处理时先修复存储，再按审计动作 `file.cleanup.failure` 且 `details.attempt=3` 找到文件 ID，执行：

```bash
uv run --locked keystone retry-file-delete --file-id <文件 UUID>
```

命令清零失败次数并写系统审计，由 Worker 继续清理；文件须仍处于已停止的删除状态（否则返回 `file_delete_not_exhausted`）且仍无引用。

### 排障

三类扫描使用独立串行维护队列，费用 HTTP 使用独立查询队列。排障检查 file_cleanup_failed、flow_reconcile_failed、flow_sync_deferred、cost_scan_failed、cost_reconcile_failed，以及 worker_failed 的安全错误码；供应商待出账、限流或 Token 失效保留为费用状态，不等同 Worker 故障。共享维护扫描停止时查看 DBOS 的 PENDING，故障/旧版本记录可能占住全局容量；先按原身份/版本恢复或走[停机发布](#45-后续发布回退与联合恢复)，不删 DBOS 历史。只清理超过 24 小时的 stopped 观测，未处理故障记录继续保留。

## 历史保留与清理

默认自动清理超过保留期的审计日志和已完成的 DBOS 维护任务。过期凭据、已结束的消息投递与入站记录仍需显式开启自动清理。业务实体、FlowRun、业务工作流检查点、文件引用、ModelInvocation、费用与调用幂等记录不在删除范围内。

### 保留规则

| 配置 | 默认 | 规则 |
|---|---:|---|
| `maintenance_cleanup_enabled` | true | 是否自动清理 DBOS 维护历史 |
| `audit_cleanup_enabled` | true | 是否自动清理过期审计日志 |
| `history_cleanup_enabled` | false | 是否自动清理过期凭据与消息历史 |
| `session_retention_days` | 30 | 从会话过期或撤销的较早时刻起保留 |
| `pat_retention_days` | 90 | 从 PAT 过期或撤销的较早时刻起保留；任何 FlowRun 引用均阻止物理删除，包括终态 Run |
| `audit_retention_days` | 30 | 按 `occurred_at` 保留；删除结果写运行日志，不递归生成审计 |
| `maintenance_retention_days` | 14 | 仅删除白名单内、已完成且超过保留期的维护工作流叶节点 |
| `message_delivery_retention_days` | 30 | 按最后更新时间保留已结束的投递（含不再重试的 unknown）与入站记录；pending、sending 的投递和 received 的入站不删除，过期绑定码直接删除 |

天数范围均为 1–3650。相应环境变量使用 `KEYSTONE_` 前缀及大写名称。API、Worker、CLI 应使用相同策略；这些配置参与 Worker 公共配置摘要。

每轮最多选择 600 条 DBOS 维护工作流，应用历史每类仍最多 200 条。凭据删除与身份变更互斥，PAT 引用以 Flow 的公开查询在同一事务中排除，避免被保留的旧 PAT 阻塞后续候选。审计和投递按时间和 ID 分批删除；并行操作跳过已锁行。

DBOS 历史通过原生客户端读取和删除，不直接修改引擎表。仅限 application_name 为 keystone 的文件清理、Flow 协调、查费、消息派发、环境空闲停止和历史清理自身的固定工作流名称；不按时间删除所有工作流。候选及其最多 16 层祖先必须均为本应用已过期的维护终态，不能是业务 Run、其他应用或未标明应用的任务、活动父任务、缺失祖先或历史分叉。批量检查各应用的子任务，结果满批时继续检查尚未确认的父节点。存在子任务时先保留父任务，删除不级联子任务。不读取输入、输出或供应商正文。

未知调用、未核对费用和原执行恢复所需的业务记录始终保留。没有符合条件的历史不代表引擎没有在途任务，也不构成退役、版本兼容或 exactly-once 的证明。

### 手动预览与执行

```bash
uv run --locked keystone prune-history
uv run --locked keystone prune-history --apply
```

第一条只预览一批，第二条显式删除一批；它不改变自动清理开关。输出按类别给出本批数量、执行模式及维护历史的 examined/eligible/deleted，不是全库总数。重复执行重新查询当前事实，不能把上次预览当作固定删除清单。

维护历史返回 next_offset，避免第一页的受保护父任务阻塞后续候选；手动续扫可用 `--history-offset N`，使用执行结果中的下一页偏移。自动任务在当前 Runtime 内推进该游标，到达末页后回到零；进程重启或扫描失败也从零重新检查。游标只优化扫描，不是删除授权、持久业务进度或恢复凭据。

`history_retention` JSON 日志记录各阶段结果。阶段分别提交，失败可能已有前序阶段完成；此时记录 partial_failure 并保留实际已完成数量，不声称整轮原子回滚。凭据、审计和投递阶段各限时 10 秒，引擎阶段限时 30 秒，原生查询及删除设置 3 秒语句/锁等待上限；取消须等待原生操作结束后才关闭连接。

### 自动维护

任一自动清理开关启用时，Worker 共用 `keystone.history.retention`：每 3 分钟运行一轮（最大迟到 60 秒），启动时扫描一次，不补历史触发，合并活动执行。每轮只处理已启用的类别；专用队列全局并发为 1，不占业务 Flow 队列。所有开关均关闭时暂停计划，残留 tick 和任务仍在执行入口检查开关。手动命令始终可预览或清理各类历史，不受自动开关影响。

升级后的 Worker 首次启动也会处理已有审计和 DBOS 历史。如果需要保留超过 30 天的审计日志，应在启动前提高 `audit_retention_days`、完成归档，或设置 `audit_cleanup_enabled = false` 暂缓；`history_cleanup_enabled = false` 不会关闭审计清理。如需暂缓 DBOS 维护历史清理，设置 `maintenance_cleanup_enabled = false`。归档与备份要求见[备份与恢复](backup-restore.md)。
