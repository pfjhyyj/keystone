# 部署与维护

日常启动见[Quickstart](../../README.md#quickstart)。本页维护构建、发布、Worker 运维和历史清理；deploy/compose.yaml 只启动 PostgreSQL，不启动应用或执行迁移。

[构建与初始化](#1-构建镜像) · [更新与恢复](#3-更新与恢复) · [Worker 管理](#4-多-worker-部署与发布) · [健康排障](#worker-健康检查排障) · [历史清理](#历史保留与清理)

## 1. 构建镜像

从仓库根目录执行，命令适用于 Linux/macOS：

```bash
docker build -t keystone-backend:local ./backend
docker build -t keystone-console:local ./frontend/console
```

| 镜像 | Dockerfile | 运行命令 |
|---|---|---|
| 后端 API | `backend/Dockerfile` | `keystone-api`（镜像默认命令） |
| 后端 Worker | 同一个后端镜像 | `keystone-worker` |
| 控制台 | `frontend/console/Dockerfile` | Nginx（镜像默认命令） |

API 与 Worker 使用同一份代码、锁文件和镜像，分别启动进程。控制台镜像包含编译后的 Vue 静态文件及 `/api` 反向代理；后端镜像只含 API、Worker 与 CLI 代码。每个工程的 `.dockerignore` 仅允许构建所需源码和配置进入上下文，不读取或打包实际 `keystone.toml`、`.env`；构建不执行初始化、不创建模型配置。

## 2. 运行配置与初始化

由实际部署环境配置应用进程、数据库连接、文件卷、精确的浏览器 Origin 和入口代理；后端参数见[配置说明](configuration.md)。API/Worker 镜像以 UID 10001 运行，挂载的协调目录和本地文件目录须可写。控制台镜像默认通过 Docker 内部 DNS 转发到同网络中名为 `api` 的服务、端口 8000；其他部署拓扑需调整 `frontend/console/docker/nginx.conf` 后构建。

后端统一读取 `keystone.toml`。在运行时将同一份配置只读挂载到初始化进程、API 和 Worker 的 `/app/keystone.toml`，不将真实配置或秘密写入镜像。也可通过 `KEYSTONE_CONFIG_FILE=/etc/keystone/keystone.toml` 指定挂载位置。相对文件路径以配置文件目录为基准；容器内建议使用绝对挂载路径，例如：

```toml
file_coordination_root = "/data/files"
file_default_storage = "local"

[file_storages.local]
driver = "filesystem"
root = "/data/files"
```

进程环境变量覆盖 TOML，适合注入 `KEYSTONE_DATABASE_URL`、`KEYSTONE_INITIAL_ADMIN_PASSWORD` 或 `KEYSTONE_FILE_STORAGES__CLOUD__SECRET_KEY` 等秘密。容器默认通过 `KEYSTONE_HOST=0.0.0.0` 监听网络接口；浏览器入口、数据库连接及存储路径按实际环境设置。纯环境变量部署也可运行，文件源逐项注入，不需要 JSON 存储映射；具体规则见[配置说明](configuration.md)。

在应用的实际运行环境中手动执行一次 `keystone init`，然后分别运行 API 与 Worker。初始化一次完成完整业务结构、Flow 执行结构、文件存储与初始管理员，无需单独执行迁移命令；重复执行保留已有数据、密码和卷标识。API/Worker 自身启动只检查依赖，不迁移数据库。

Worker 启动 DBOS 与维护计划。升级前按[更新流程](#3-更新与恢复)处理在途执行，再运行 keystone init 升级当前迁移链。当前 head 及不兼容旧库的处理以[迁移说明](../../backend/migrations/README.md#初始化与已有数据库)为准；保留原协调卷、调用和费用事实，不推测回填历史归属。

模型凭据、渠道和模型由操作者按需在控制台手动配置，步骤见 [README](../../README.md#模型配置手动)。未配置模型时平台仍可启动；启动和初始化不发起真实模型请求。

`/api/v1/health` 检查 API 存活，`/api/v1/ready` 检查数据库、业务版本及存储；Worker 使用独立日志或[健康探针](#worker-健康检查排障)。

`/api/v1/worker/status` 的实例与积压明细要求有效登录身份。凭据、审计及维护任务的自动历史清理默认关闭；按[历史清理](#历史保留与清理)预览保留范围并完成必要归档后，再配置同版本 API/Worker 的保留策略并重新发布。

### 2.1 调用失败排查

API 与 Worker 将 JSON 日志输出到标准错误，部署时由进程管理器或容器平台保留并轮转。模型失败事件为 `model_call_failed`，主动中断为 `model_call_interrupted`；按调用记录的 UUID 查询 API 和 Worker 日志，HTTP 响应头 `X-Request-ID` 对应日志顶层 `request_id`。未完成发送登记时没有持久调用记录，仍可按请求 ID 查询。

`diagnostic` 包含渠道/模型 ID、操作、`stage`、`transport_phase`、`elapsed_ms`、是否登记及上游状态/请求编号。`network_reason` 区分连接/读写超时、DNS、TLS、连接重置和协议异常；`upstream_error_code` / `upstream_error_type` 仅在上游提供已知安全代码时输出。`exception` 保留有界异常链和文件、函数、行号，不包含异常原文、源码行和局部变量。上游没有明确长度错误时，不能仅按耗时或 unknown 判定输入太长。

本机开发若需跨终端保留日志，可在 `backend/` 下分别启动 API 与 Worker：

```bash
mkdir -p ../.artifacts/logs
uv run --locked keystone-api >>../.artifacts/logs/api.log 2>&1
```

```bash
uv run --locked keystone-worker >>../.artifacts/logs/worker.log 2>&1
```

从仓库根目录查询：

```bash
rg --fixed-strings '替换为调用记录 UUID 或 X-Request-ID' .artifacts/logs
```

日志只覆盖采用当前代码启动后的调用；历史 unknown 记录无法补回当时未记录的异常链。无需修改数据库。发布时仍按下一节处理在途调用与同版本 API/Worker，避免为新增日志打断已发送请求。

## 3. 更新与恢复

发布前先停止接收新业务写入，使未完成 Flow 在原版本下完成或受控取消并同步终态，再停止全部旧 API/Worker，联合备份数据库、文件及当前发布制品。取消受理不代表已发送模型请求或原生文件 I/O 已结束，停止前仍需核对事实收尾。按[迁移说明](../../backend/migrations/README.md#初始化与已有数据库)选择初始化目标。已有兼容库执行一次 `keystone init`，旧基线先初始化新库并迁移所需数据。核对后分别启动应用进程。所有实例保持同一制品，同身份的新旧 Worker 不并行运行；无法处理的旧业务执行保留原版本恢复路径，不交给新版本强行接管。不兼容升级失败时，不能只退代码而保留新 schema。

完整备份边界见[备份与恢复](backup-restore.md)。Dockerfile 和测试容器验证使用独立的 `tests/deploy/compose.yaml`，不改变本机开发启动方式。



.transfers 是可重建的工作副本，不是正式内容或恢复依据。媒体容量及代理时限见[配置](configuration.md)，部署前可执行[媒体验收](../../backend/tests/README.md#本地媒体专项验收)。

## 4. 多 Worker 部署与发布

同宿主、同版本、固定身份，最多四个未退役身份；离线或普通停止的登记仍计入名额。每个进程只指定自己的 worker_id，成员由数据库登记，API 动态发现。共享预算继续沿用内置默认值，职责与准入顺序见[任务执行](../execution.md#73-多-worker恢复与发布)。数量上限不是实际吞吐保证，部署前仍需核算资源。

### 4.1 身份与共同部署条件

API、全部 Worker 和初始化进程使用同一制品、依赖、数据库与 DBOS schema，以及相同的执行配置、预算和文件配置。协调目录是同宿主可靠本地共享卷，保留模型加密密钥、文件锁和存储标识；内容可以在已配置的 filesystem/NAS/S3，协调目录仍不支持跨宿主或 NFS。

worker_id 是每个 Worker 独立的固定身份，同时用作 DBOS executor_id；第一个身份保留 keystone-worker，每次启动另生成 instance_id。身份由部署者固定指定，不随机生成，也不因故障更名。原 API 和 Worker 可以持续运行，无需维护额外身份清单。

首次部署仍执行一次初始化，再分别启动 API 和默认 Worker；在 backend/ 执行：

```bash
uv run --locked keystone-worker
```

增加第二个 Worker 时，仅在另一个终端或进程管理器服务中启动：

```bash
KEYSTONE_WORKER_ID=keystone-worker-02 uv run --locked keystone-worker
```

新进程先取得本身份的 OS/PostgreSQL 排他锁，再串行校验宿主、版本、公共执行配置、实际协调卷与未退役名额。通过后登记并沿用自身旧实例恢复、启动前预算注册、DBOS 启动和计划安装顺序。重复身份、已退役身份的普通启动、不兼容配置及超出四个名额均拒绝；拒绝不能污染登记、心跳或队列/计划。并发争用最后一个名额只允许一个新身份成功。

API 从未退役登记及最新心跳发现新成员，随后可用 --inspect 或控制台核对就绪数量；无需修改配置或重启 API、原 Worker，也不执行初始化或迁移。新成员不提高共享预算、不复制维护计划，现有并发、去重和恢复机制保持原样。

容器使用不同固定服务身份、相同只读执行配置和真实共享卷，不能直接 scale 同身份服务。主机校验继续使用进程读取的 hostname；同一宿主的容器须统一该 hostname，统一名称不能替代实际共享本地卷。进程管理器配置原身份重启；终止宽限期须大于 worker_shutdown_seconds 并给事实收尾/文件 I/O 留出时间。代码版本、公共预算或存储配置变化仍采用停机发布，不能由新进程覆盖在用声明。

Worker 准入比较协调卷现有 .keystone-storage 标识，拒绝同路径实际挂载了不同卷的实例。保留原卷标识、模型加密密钥和永久锁文件，不复制空卷冒充原卷。

### 4.2 容量、计划与健康检查

队列、模型及费用查询预算见[任务执行](../execution.md#72-队列预算)，调优项见[配置](configuration.md)。增加 Worker 不自动提高全局预算；供应商 RPM/TPM 不是该并发限制所保证的配额。

核算 API/Worker 业务池、DBOS 池、身份锁及查询/推理协调连接、运维预留，加上 CPU、临时空间和文件传输槽位总量；不能把单进程预算直接乘实例数。全部进程装配相同的稳定维护计划，周期与迟到窗口见[持久计划](../execution.md#4-持久计划)，保留启动立即扫描、不补跑全部旧时间点；无按身份复制的计划或主调度实例。只读查询允许重做，分页进度和费用必须幂等，模型请求仍禁止重发。

控制台按数据库未退役登记展示预期身份、最新实例、就绪数量及共享维护，新增身份自动出现。正常停止或故障不会移除登记、减少预期数量，不能以一个 ready 实例掩盖另一身份离线。心跳历史清理不删除登记，已退役身份单列显示且不计预期数量；零登记时显示离线。共享维护异常单独告警，空闲进程不需要亲自执行每类扫描。探针仍检查确切身份，继承相同身份环境：

```bash
KEYSTONE_WORKER_ID=keystone-worker-02 uv run --locked keystone-worker --check
uv run --locked keystone-worker --inspect
```

--inspect 不启动执行器，输出实例/共享维护及活动 DBOS 队列的身份、版本、状态和数量，不含输入输出。最多读取 1000 条并标记 truncated，截断或 unknown 时不能据此认定没有遗留执行；必要时通过 DBOS 公开客户端按 executor/版本过滤核对。该视图也不证明外部请求已经停止，永久移除身份仍须核对原业务与模型记录。

### 4.3 API 调用的离线收尾

停止全部 API 进程，并确保收尾期间不会重启后，在 backend/ 执行：

```bash
uv run --locked keystone recover-model-calls --confirm-api-stopped
```

确认标志由操作者保证停机，命令不探测进程。它将中断的 API 调用标记为 unknown，保留原调用、费用和审计，不重发推理。已提交批次保留，重新确认停机后可重跑。

Flow 调用由原身份 Worker 使用原制品恢复，按下节的单实例故障恢复处理。

### 4.4 固定槽位调整与故障恢复

动态增加沿用原 Worker 启动命令，永久缩容使用独立 CLI，不需要停止其他实例或 API。

**增加实例**：按第 4.1 节启动新固定身份即可，API 和已有 Worker 持续运行。成员数量与公共执行配置分离，不再因为新增身份触发配置摘要冲突；不迁移、不放大全局预算。新增身份不能代替故障身份的恢复，也不能接管其 PENDING。

**单实例故障恢复**：确认旧进程确实退出，以原身份、原兼容制品和原配置/协调卷重启，保留其他实例运行。先仅收尾自身旧实例的模型调用，再启动 DBOS 恢复；身份锁和实例 ID 继续防止新旧进程重叠。故障登记一直保留并计入预期数量和四个名额，不能按心跳过期自动移除、退役或改名绕过。

启动收尾使用持身份锁的连接逐批提交，每批事务等待上限为 3 秒；断线或超时会回滚当前批次并退出，已提交批次保留。解除数据库阻塞或恢复连接后仍按原身份重启，不删除调用或审计。队列准备完成后还会在 DBOS 启动前复核身份，失锁的启动不能开始恢复执行。

**缩容退役**：

1. 停用目标服务的自动重启或副本声明，仅让目标 Worker 停止领取并排空，等待其进程退出；API 和其他 Worker 不停。排空期间继续报告 draining，退出报告 stopped，但成员仍保留。
2. 退役入口取得目标身份锁，并与登记事务串行核对：旧进程已结束，没有归属该 executor 的在途或待恢复 DBOS 执行，也没有该身份遗留的 running 模型调用。仅看心跳或 stopped 不足以通过；查询失败、未知或截断时拒绝退役。同身份并发启动与退役必须互斥，不能仅先查再写。
3. 符合条件后显式将登记标记退役，保留登记、模型调用、执行及操作审计历史。API 动态更新 expected_workers，释放一个未退役名额；其他实例无需重启。

共享队列中尚未绑定该身份的任务仍由其他匹配 Worker 领取，不因退役删除或取消。退役只锁目标身份，保留 DBOS 历史。排空超时或仍有归属执行时保留成员和原身份/制品恢复路径，先恢复收尾后再退役；已经退役的身份需要显式重新启用并重新准入，不能普通启动静默重新登记。

目标进程退出后，在 backend/ 执行：

```sh
uv run --locked keystone retire-worker --worker-id keystone-worker-02
uv run --locked keystone-worker --inspect
```

退役检查使用 DBOS 公开客户端按 executor_id 精确查询 PENDING、ENQUEUED、DELAYED 是否存在，不以有界聚合的“未出现”推断清空；同时检查该身份的 running 模型调用。退役状态及系统审计在持身份锁的同一数据库连接内提交，连接中断时回滚。正在运行返回互斥忙碌；有归属执行返回 worker_recovery_required；查询不可用返回 worker_inspection_unavailable，均不移除登记。

重新使用已退役身份时先执行下述命令，再另行启动；重新启用仍校验部署兼容性和名额，执行命令本身不启动 Worker：

```sh
uv run --locked keystone reactivate-worker --worker-id keystone-worker-02
KEYSTONE_WORKER_ID=keystone-worker-02 uv run --locked keystone-worker
```

已在目标状态的生命周期命令幂等，不重复写成功审计。未登记身份返回 worker_not_registered，普通启动已退役身份返回 worker_retired；不能自动换名字绕过退役。

SIGINT/SIGTERM 停止领取并报告 draining，心跳在有界等待期间继续发布。只有运行时、请求事实和本进程资源完成退出后才释放锁并报告 stopped；等待超时仍可能有待恢复 DBOS 记录，退出不等于业务取消或可以删除身份。

故障重启后，重新入队的任务可由任一匹配版本 Worker 领取。其他健康实例不按心跳过期自动抢占，也不全局收尾所有模型调用。身份数据库连接丢失会停止当前执行器，不能换连接后继续使用旧身份执行。

身份连接定期核验，单次核验的等待上限为 3 秒，超时也进入停止流程；这不是从故障发生到进程退出的总时限，检测间隔、排空和资源收尾仍需时间。进程管理器仍须等待旧进程确实退出后再以原身份重启。

只读查费可以重做，已提交 checkpoint 和费用继续复用；配置、轮次/revision 或持锁连接改变时旧响应拒绝写回。模型请求已发送但结果缺少可靠检查点时保持未知或结果不可恢复，不自动补发。详见[恢复边界](../execution.md#73-多-worker恢复与发布)。

### 4.5 后续发布、回退与联合恢复

代码版本、公共预算、执行配置或存储配置变化继续停机发布，不混跑新旧版本：阻止新受理，在旧制品处理业务与取消意图，随后停止全部 API/Worker，核对遗留执行并联合备份。单次迁移后按原身份启动同一新制品；公共配置变化时核对登记与旧观测，必要时在全部进程停止期间使用第 4.3 节离线入口，不因配置冲突随机换身份绕过。同版本增员/退役不走此流程。费用待办不因发布重置原 Token、期限或金额。

未处理的旧业务保留原制品/身份恢复路径，不能交给新代码解释旧检查点。可重建维护执行按第 4.3 节受控退役，避免旧 PENDING 占住容量；这不允许删除真实业务记录、防重发记录或把未知费用当作零。

回退前停止新写入和全部新进程，核对新版本已产生的运行及外部副作用。只有 schema、配置和检查点兼容且原版本能安全恢复时才只回退代码；否则恢复同一停写窗口的数据库、全部内容源、协调密钥/锁文件、身份清单、配置及原制品。恢复先用原版本与原身份核对，再考虑升级。真实上游费用不会随数据库回滚，ready 不构成重发许可。

联合备份须保留登记及退役状态，不能在恢复时仅根据当下在线进程重建成员。历史 Flow 离线收尾只处理其原有范围，不自动退役已登记身份。

### 4.6 部署验收

发布前运行[完整检查](../../backend/tests/README.md)，在隔离环境验证迁移、双 Worker、取消、崩溃恢复和联合备份恢复。成员增减须保持已有进程和在途执行可用，失锁不能写回旧结果，未知模型请求不能重发。

实际部署另外验证存储吞吐、磁盘/连接预算、共享维护进度及进程管理器按原身份重启；本地模拟上游的测试结果不代表生产容量。

## Worker 健康检查（排障）

Worker 每 5 秒向 PostgreSQL 上报心跳，启动、依赖失败和排空期间也继续报告。每个固定 worker_id 按登记所指向的当前 instance_id 展示状态，执行版本和公共配置须一致；默认运行一个实例。数据库时间判断心跳与维护检查是否超过 30 秒，旧实例迟到的退出不遮蔽新进程。

API 按未退役身份登记计算预期数量并自动发现新 Worker，无需同步身份清单或重启 API/已有 Worker。普通停止、心跳过期与历史观测清理不移除登记；永久缩容先停止目标进程，再执行 `uv run --locked keystone retire-worker --worker-id ID`。退役拒绝仍在运行或有待恢复工作的身份；恢复退役身份须先执行 reactivate-worker。同身份互斥、故障恢复与操作步骤见[部署说明](#44-固定槽位调整与故障恢复)。

进程健康与共享维护分开：进程检查依赖、DBOS 和队列；共享维护聚合实际执行过的文件清理、Flow 协调及费用扫描结果。任一同版本实例成功扫描即可更新共享结果，空闲实例不因未亲自扫描误报。普通心跳不能刷新陈旧的任务结果，依赖检查也不能掩盖共享维护故障。供应商待出账、限流或 Token 失效保留为费用状态，不等同 Worker 故障。

GET /api/v1/worker/status 要求当前有效登录身份（会话或 PAT，仍检查用户状态及强制改密），返回总体 status、last_heartbeat_at、instances、maintenance、ready_workers 和 expected_workers，控制台每 5 秒刷新并分开展示。匿名或失效凭据不能读取实例明细；公开的 health/ready 探针继续可用，Worker CLI 直接检查数据库，不使用浏览器会话。未退役身份缺失、不同版本、过期或未就绪时，不能被另一个 ready 实例掩盖；退役身份单独展示且不计入预期数量，没有登记成员时总体为 offline。API /api/v1/ready 只检查自身数据库、schema 与存储，不受 Worker 观测影响。

响应的 backlog 分别报告 pending 状态待协调 Flow、待清理文件和已到查费时间的调用数量、最早时间及距今秒数。Flow 使用创建时间，文件使用最近更新时间，查费使用下次应查时间；文件重试会更新最近更新时间，因此不能将其解释为首次请求后的累计等待。正在运行的 Flow 不计作 pending 积压，已删除 S3 对象的例行残留核对不计作待清理积压。各领域独立查询，失败返回 unknown 和空值，不能当作零或覆盖其他领域已确认的状态。这些观测也不改变 Worker 的进程/维护健康结论。查询前后复查身份，避免撤权期间返回实例或积压数据。

每个 HTTP 请求完成后记录 `http_request` JSON 日志，包含 request_id、方法、路由模板、真实响应状态码、耗时毫秒及 completed/interrupted。流式响应头已发送后失败仍记录原状态及 interrupted，不伪造 500；日志不记录实际路径参数、查询参数、请求体或凭据。历史保留和有界清理见[历史清理](#历史保留与清理)。

从 backend/ 执行，探针与运行 Worker 使用同一身份环境；以下为 Linux/macOS 示例：

```bash
KEYSTONE_WORKER_ID=keystone-worker uv run --locked keystone-worker --check
uv run --locked keystone-worker --inspect
```

--check 只查询当前主机的指定身份，不启动执行器或刷新心跳；进程就绪退出 0，其余退出 1。共享维护异常单独告警，不能据此重启所有正常进程。--inspect 额外列出有界的 DBOS 活动队列/身份/版本/状态计数，不读取输入输出、不启动执行器；unknown 或 truncated 时不能据此认定队列已排空。

三类扫描仍使用独立串行维护队列，费用 HTTP 使用独立查询队列。排障检查 file_cleanup_failed、flow_reconcile_failed、flow_sync_deferred、cost_scan_failed、cost_reconcile_failed，以及 worker_failed 的安全错误码。worker_deployment_conflict 表示主机、版本或公共配置冲突；不要随机更换身份绕过。共享维护扫描停止时查看 DBOS 的 PENDING，故障/旧版本记录可能占住全局容量；先按原身份/版本恢复或走[停机发布核对](#4-多-worker-部署与发布)，不删 DBOS 历史。

SIGINT/SIGTERM 触发 draining，停止领取后按 worker_shutdown_seconds 有限等待，再关闭并报告 stopped。超时仍需保留原槽位恢复路径；心跳过期不是外部请求停止证明。只清理超过 24 小时的 stopped 观测，未处理故障记录继续保留。Worker 启动只收尾自身前一实例的调用，不进行全局收尾；API 调用按[离线收尾](#43-api-调用的离线收尾)处理。

## 历史保留与清理

历史清理只处理过期凭据、审计和已完成的 DBOS 维护任务；业务实体、FlowRun、文件引用、ModelInvocation、费用与调用幂等记录不在删除范围内。默认关闭自动删除，先检查预览、确认保留政策及必要归档，再启用。

### 保留规则

| 配置 | 默认 | 规则 |
|---|---:|---|
| `history_cleanup_enabled` | false | 是否安装自动清理计划；手动预览不受影响 |
| `session_retention_days` | 30 | 从会话过期或撤销的较早时刻起保留 |
| `pat_retention_days` | 90 | 从 PAT 过期或撤销的较早时刻起保留；任何 FlowRun 引用均阻止物理删除，包括终态 Run |
| `audit_retention_days` | 365 | 按 occurred_at 保留；删除结果写运行日志，不递归生成审计 |
| `maintenance_retention_days` | 30 | 仅删除白名单内、已完成且超过保留期的维护工作流叶节点 |

天数范围均为 1–3650。相应环境变量使用 `KEYSTONE_` 前缀及大写名称。API、Worker、CLI 应使用相同策略；这些配置参与 Worker 公共配置摘要。

每轮最多选择每类 200 条。凭据删除与身份变更互斥，PAT 引用以 Flow 的公开查询在同一事务中排除，避免被保留的旧 PAT 阻塞后续候选。审计按时间和 ID 分批删除；并行操作跳过已锁行。

DBOS 历史通过原生客户端读取和删除，不直接修改引擎表。仅限 application_name 为 keystone 的文件清理、Flow 协调、查费和历史清理自身的固定工作流名称；不按时间删除所有工作流。候选及其最多 16 层祖先必须均为本应用已过期的维护终态，不能是业务 Run、其他应用或未标明应用的任务、活动父任务、缺失祖先或历史分叉。存在任何应用的子任务时先保留父任务，后续批次再处理已无子任务的父节点；删除不级联子任务。不读取输入、输出或供应商正文。

未知调用、未核对费用和原执行恢复所需的业务记录始终保留。没有符合条件的历史不代表引擎没有在途任务，也不构成退役、版本兼容或 exactly-once 的证明。

### 手动预览与执行

在 `backend/`，使用与应用相同的配置：

```bash
uv run --locked keystone prune-history
uv run --locked keystone prune-history --apply
```

第一条只预览一批，第二条显式删除一批；它不改变自动清理开关。输出按类别给出本批数量、执行模式及维护历史的 examined/eligible/deleted，不是全库总数。重复执行重新查询当前事实，不能把上次预览当作固定删除清单。

维护历史返回 next_offset，避免第一页的受保护父任务阻塞后续候选；手动续扫可用 `--history-offset N`，使用执行结果中的下一页偏移。自动任务在当前 Runtime 内推进该游标，到达末页后回到零；进程重启或扫描失败也从零重新检查。游标只优化扫描，不是删除授权、持久业务进度或恢复凭据。

`history_retention` JSON 日志记录各阶段结果。阶段分别提交，失败可能已有前序阶段完成；此时记录 partial_failure 并保留实际已完成数量，不声称整轮原子回滚。凭据和审计阶段各限时 10 秒，引擎阶段限时 30 秒，原生查询及删除设置 3 秒语句/锁等待上限；取消须等待原生操作结束后才关闭连接。

### 自动维护

配置 `history_cleanup_enabled = true` 并按发布流程重新启动同版本 Worker 后，安装 `keystone.history.retention`，每小时运行（最大迟到 300 秒）、启动时扫描一次、不补历史触发、合并活动执行。专用维护队列全局并发为 1，单个任务预算 60 秒；清理不占业务 Flow 队列。关闭时暂停计划，残留 tick 和任务仍在执行入口复查开关。默认关闭时保留原有三个维护计划。

部署者负责在保留期限到来前完成必要归档与[联合备份](backup-restore.md)。修改策略不会删除现有业务记录，也不允许删除未知调用来绕过恢复。
