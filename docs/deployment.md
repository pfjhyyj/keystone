# 部署、冷启动与备份恢复

范围：单宿主、一个 API、一个 Worker、PostgreSQL 17 和本地共享文件卷。沿用 Ether 的独立 Compose 验收、停写发布、`pg_dump/pg_restore` 与 `tar` 联合恢复方式，不增加备份服务或领域表。整体边界见[架构](architecture.md)，验收结果统一记在[实施记录](implementation-plan.md#412-部署冷启动与备份恢复)。

## 1. 首次启动

从仓库根目录复制 `deploy/.env.example` 为 `deploy/.env`，设置数据库密码和 URL。模拟验收使用 `KEYSTONE_ENV=test`、`KEYSTONE_MODEL_MODE=mock`、`KEYSTONE_FLOW_ENABLED=true`，秘密引用配置为 `{"inference":"fake","billing":"fake"}`。这些值只用于模拟，生产配置拒绝 mock。

三层 `.env.example` 完整列出各自保留的配置，日志、安全策略、上传限制和镜像选择均已在对应示例中提供，见[配置对应](configuration.md#2-配置对应)。整套 Compose 部署只使用显式选定的部署环境文件；本机前后端 `.env` 不参与容器配置。后端参数显式传给 API、Worker 与一次性服务，admin 不接收后端秘密。浏览器保持同源 `/api`，Nginx 固定转发 `api:8000`。

已有 shell 变量优先于部署文件，未填写的可选参数沿用默认值；修改文件后通过 `up -d` 重建受影响容器，单纯 `restart` 不更新容器环境。容器内部端口及路径固定，`KEYSTONE_API_PORT` 等变量仅控制宿主映射。上传上限超过默认 16 MiB 时，还需同步调整 Nginx 的 `client_max_body_size` 并重建 admin 镜像。

默认模型模式仍为 disabled。OpenRouter live 已提供，配置见[接入说明](../README.md#openrouter-live-接入)：API/Worker 必须一致设置 live 和秘密映射，推理 Key 与 Management Key 分开引用，只允许官方 HTTPS 地址。首次接入使用独立空数据库和文件卷并初始化，不把已有 mock 部署切为 live，也不迁入模拟调用、余额或 DBOS 历史；运行模式属于部署，不能靠新建渠道隔离。模式与秘密不在数据库迁移中，备份恢复必须保持原模式和原账户配置。本地验收脚本继续强制使用 mock 与假秘密，不访问真实渠道。

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yaml config -q
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build -d --wait --wait-timeout 90
docker compose --env-file deploy/.env -f deploy/compose.yaml ps -a
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm --no-deps api keystone init-admin --username admin
docker compose --env-file deploy/.env -f deploy/compose.yaml logs --tail 30 worker
```

每条命令成功后才执行下一条。管理员密码交互输入；已有用户时初始化拒绝执行，不重置密码。默认入口 `http://127.0.0.1:8080/admin/`；三个宿主端口可通过 `KEYSTONE_ADMIN_PORT`、`KEYSTONE_API_PORT`、`KEYSTONE_POSTGRES_PORT` 调整。修改入口端口时同步修改 `KEYSTONE_BROWSER_ORIGINS`。

启动顺序：PostgreSQL 健康 → 业务迁移、DBOS 迁移与文件卷初始化全部成功 → API/Worker → API 就绪后开放 admin。API/Worker 使用相同镜像，均以 UID 10001 运行。首次挂载空卷由镜像目录提供写权限；不要手工把文件卷改成 root 独占。

`/api/v1/ready` 检查数据库、业务版本及文件卷标识，返回依赖检查、各存储源状态、Flow 启用配置和模型模式；它不证明 Worker 在线，也不逐个扫描数据库中的历史文件。`/healthz` 仅证明 Nginx 在线。Worker 使用下述独立健康探针；Flow 的实际执行仍需在独立 mock 环境验收。live 环境的模型 Flow 需另行有界验收，不能为检查健康临时切换模式。

### 1.1 本地 live 联调准备环境

使用独立 Compose 项目、空数据库和文件卷，从当前 `deploy/.env.example` 创建专用环境文件。按[配置说明](configuration.md#4-同时配置本地与-s3)显式声明内容源和协调目录；后台端口改变时同步调整浏览器 Origin。先保持 `KEYSTONE_MODEL_MODE=disabled`、`KEYSTONE_MODEL_SECRETS={}`、`KEYSTONE_FLOW_ENABLED=false`。

按上节首次启动流程构建当前镜像、迁移、初始化存储和管理员；每条 Compose 命令都使用同一项目名和专用环境文件。以实际 `/api/v1/ready`、Worker 探针和登录结果判断环境是否可用。数据库密码与管理员凭据由当前环境单独生成并安全保存。

随后按[真实账户准备与最小验收](domains/model-access.md#79-真实账户联调准备与最小验收)配置凭据和启用 live。首次 live 使用独立空库、空卷，已有 mock 环境保留在原项目中。

### 1.2 Worker 最小健康检查

参考 Ether 的就绪文件方式，Worker 在本进程文件系统维护一个短期标记，不写数据库、不增加心跳表。Compose 固定配置 `/tmp/keystone-worker.ready`，文件不进入共享文件卷或备份。API、迁移进程不会更新这个文件。

Worker 启动先清除旧标记；每轮基础依赖检查通过且文件维护尝试完成后刷新。维护循环固定等待 5 秒；启用 Flow 时还须 DBOS 启动、队列注册完成且 Flow 任务没有退出。依赖不可用、维护循环抛错时移除标记，后续恢复可重新写入。停止时在等待 DBOS 排空之前移除；强制退出或循环阻塞留下的标记在 30 秒后失效。无法写入已配置的健康文件会使 Worker 退出，父目录须预先存在且可写。

检查命令：

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yaml exec -T worker keystone-worker --check
```

`--check` 只读取文件是否存在及修改时间，不启动 Runtime、DBOS，不连接数据库或模型渠道，不更新标记。新鲜且非未来时间返回 0，其余情况返回 1。Compose 每 10 秒检查一次，超时 5 秒、连续 3 次失败标为 unhealthy，启动宽限 30 秒；`up --wait` 会等待 Worker 健康。unhealthy 不会单独触发 Docker 的自动重启，现有 restart 策略只处理进程退出。

本机运行可给 Worker 和探针设置相同的 `KEYSTONE_WORKER_READY_FILE`；未配置时 Worker 保持可运行，但探针返回失败。不同本机 Worker 必须使用不同文件，不能共享标记。健康只表示维护循环近期完成且所需执行器已初始化，不保证每个流程、模型请求或文件清理成功，也不检测版本不匹配等业务阻塞；仍结合 Run 状态和日志排查。API ready 和后台概览继续只反映 API 依赖状态。

## 2. 重启与发布

普通 `stop` 或 `down` 保留两个命名卷，`up -d --wait` 可重新启动。重复 migrate、migrate-flow、init-storage 保留已有数据与卷标识。已有进程不会因某次迁移失败自动停止，所以发布必须先停写。

发布顺序：确认未完成 Flow 的处置 → 停止 admin、全部 API/Worker 及外部写入 → 联合备份 → 部署新镜像并执行两项迁移 → 全部成功后启动并检查。首版只运行同一版本的一个 Worker，旧执行版本的 Flow 不自动改绑新版本；先用旧构建完成/取消，或保留其原构建与可恢复备份。不兼容升级失败时，不能只退镜像而保留新 schema。

API 重建后同步重建或重启 admin，使 Nginx 重新解析 API 容器地址。API 单独重启保持容器地址时无此要求。

## 3. 联合备份

首版采用停写备份。数据库包含整个应用库：`keystone` 的十六张领域表、Alembic 版本、会话/PAT/审计，以及 `dbos` 的版本和检查点；不能只导出领域 schema。文件卷包含文件字节、`.keystone-storage` 和永久 `.locks` 文件。二者必须取自同一停写窗口。

下面是 PowerShell 操作示例。显式使用实际项目名和 env 文件；失败立即停止。只读取源卷，不删除现有环境。备份目录必须新建，不能混入上次备份。

```powershell
$Project = 'keystone'
$ComposeArgs = @('--env-file', 'deploy/.env', '-p', $Project, '-f', 'deploy/compose.yaml')
function Invoke-Docker { & docker @args; if ($LASTEXITCODE -ne 0) { throw 'Docker operation failed' } }
function Invoke-Compose { Invoke-Docker compose @ComposeArgs @args }
$Backup = Join-Path (Get-Location) ('.artifacts/backups/' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $Backup -ErrorAction Stop | Out-Null
$Backup = (Resolve-Path -LiteralPath $Backup).Path
$Api = Invoke-Compose ps -q api
$Admin = Invoke-Compose ps -q admin
$Postgres = Invoke-Compose ps -q postgres
$BackendImage = Invoke-Docker inspect -f '{{.Image}}' $Api
$AdminImage = Invoke-Docker inspect -f '{{.Image}}' $Admin
$PostgresImage = Invoke-Docker inspect -f '{{.Image}}' $Postgres

# 保存实际运行环境以核对密钥/配置，不打印在终端；这些文件也属于敏感备份。
Invoke-Docker inspect -f '{{json .Config.Env}}' $Api | Set-Content -Encoding utf8 "$Backup/api-env.json"
Invoke-Docker inspect -f '{{json .Config.Env}}' $Postgres | Set-Content -Encoding utf8 "$Backup/postgres-env.json"
Copy-Item -LiteralPath deploy/compose.yaml -Destination "$Backup/compose.yaml"
Copy-Item -LiteralPath deploy/.env -Destination "$Backup/deploy.env"
@{backend=$BackendImage; admin=$AdminImage; postgres=$PostgresImage} |
    ConvertTo-Json | Set-Content -Encoding utf8 "$Backup/images.json"
Invoke-Docker image save -o "$Backup/images.tar" $BackendImage $AdminImage $PostgresImage

Invoke-Compose stop admin api worker
Invoke-Compose ps --status running --services
# 确认只剩 postgres，外部脚本/一次性 CLI 写入也已停止，之后保持停写。
Invoke-Compose exec -T postgres pg_dump -U keystone -d keystone -Fc -f /tmp/keystone.dump
Invoke-Docker cp "${Postgres}:/tmp/keystone.dump" "$Backup/database.dump"
Invoke-Docker run --rm --network none --user 0:0 `
    --mount "type=volume,src=${Project}_file_data,dst=/data/files,readonly" `
    --mount "type=bind,src=$Backup,dst=/backup" `
    --entrypoint tar $BackendImage -C /data/files -cpf /backup/files.tar .
Get-FileHash -Algorithm SHA256 "$Backup/database.dump", "$Backup/files.tar", "$Backup/images.tar" |
    Select-Object Path, Hash | ConvertTo-Json | Set-Content -Encoding utf8 "$Backup/checksums.json"
```

卷名示例基于当前 Compose 的默认命名，使用自定义卷时先核对实际挂载。源 `.env` 可能已被修改，恢复配置以保存的运行时环境为准；同时保存反向代理/TLS 等宿主配置及代码发布版本。模型秘密值在部署配置中，不在 Channel 表里，须一起保管。备份包含密码哈希、会话/PAT 哈希、业务文件与配置秘密，限制访问并加密转存离线位置；同一宿主的 `.artifacts` 仅为中转，不能当作灾备副本。

备份完成且不切换恢复环境时，可以启动原环境。若要切换，原环境必须持续停写，禁止两套 Worker 同时处理同一批恢复数据。`pg_dump` 不会暂停文件写入，所以单独的在线数据库导出不满足此联合恢复规则。

## 4. 恢复到新环境

先核对校验和，加载备份镜像，使用新的项目名、空数据库和新文件卷。不要恢复覆盖正在使用的数据库/文件卷；不要执行 `down -v` 清空原环境。以下步骤只适用于当前 Compose 的内置 PostgreSQL。

1. 用备份时的 Compose 文件和实际运行配置准备恢复专用 env 文件。数据库地址仍指向新项目内部 `postgres:5432/keystone`；用户名、密码与原备份对应。设置 `KEYSTONE_BACKEND_IMAGE`、`KEYSTONE_ADMIN_IMAGE`、`KEYSTONE_POSTGRES_IMAGE` 为 `images.json` 中的原镜像 ID；在新主机先 `docker image load -i <备份路径>/images.tar`。
2. 设置未被占用的三个宿主端口及精确的 `KEYSTONE_BROWSER_ORIGINS`；保留原 Flow 开关、秘密引用和值。恢复文件是配置，不是重新初始化管理员；不要运行 init-admin。
3. 执行下面命令。`$Backup` 需指向可信备份的绝对目录，恢复项目名在 Docker 中必须尚不存在。

```powershell
$RestoreProject = 'keystone-recovery-20261018'
$ComposeArgs = @('--env-file', 'deploy/.env.recovery', '-p', $RestoreProject, '-f', "$Backup/compose.yaml")
# 沿用上一节的 Invoke-Docker / Invoke-Compose，已核对新项目没有现存卷。
Invoke-Compose up -d --no-build --pull never --wait postgres
$Postgres = Invoke-Compose ps -q postgres
Invoke-Docker cp "$Backup/database.dump" "${Postgres}:/tmp/keystone.dump"
Invoke-Compose exec -T postgres pg_restore -U keystone -d keystone `
    --exit-on-error --single-transaction /tmp/keystone.dump
# 只准备新卷的 UID 10001 权限；此时不能启动 API/Worker。
Invoke-Compose run --rm --no-deps storage-init
$BackendImage = (Get-Content "$Backup/images.json" | ConvertFrom-Json).backend
Invoke-Docker run --rm --network none --user 0:0 `
    --mount "type=volume,src=${RestoreProject}_file_data,dst=/data/files" `
    --mount "type=bind,src=$Backup,dst=/backup,readonly" `
    --entrypoint tar $BackendImage -C /data/files -xpf /backup/files.tar
# tar 恢复原标识、锁文件及 UID/权限，不能保留新初始化生成的标识。
Invoke-Compose up -d --no-build --pull never --wait --wait-timeout 90
Invoke-Compose logs --tail 30 worker
```

先恢复原版本并验收，之后才按发布流程升级。没有原镜像或相关密钥时，不能声称已经可恢复。只恢复数据库而挂上一个新初始化文件卷时，API ready 仍可能成功，但旧文件读取会因卷指纹不符返回 503；这是防串卷保护，不是完整性扫描。

恢复后核对：登录和权限边界、字典及审计；原文件/报告的实际字节和授权；模型调用原记录与费用；已完成 Flow 的原 ID、结果和 DBOS 步骤；待执行 Flow 在同一执行版本继续处理；新上传和新调用可以写入。会话与 PAT 数据也会回到备份时点，尚未过期且在该时点有效的凭据可能继续有效，不能把“恢复”当作全量登出。切换前按事件情况撤销相关凭据。

普通 API 模型调用不随 Worker 自动收尾；备份中若有遗留 running 记录，在全部 API 保持停止时执行现有 `recover-model-calls --confirm-api-stopped`，转为 unknown 并保留账务证据。上游费用/副作用不会随数据库回滚：不能因恢复了旧备份就自动重新发送已发生的真实请求，需按原回执查证。本轮模拟验收不证明真实渠道的灾备语义。

## 5. 可重复验收

准备 backend 开发虚拟环境、admin 依赖与 Playwright Chromium，确认 Docker Linux 引擎可用，然后从仓库根目录执行：

```powershell
backend/.venv/Scripts/python.exe tests/deploy/verify.py
```

脚本默认构建两个实际 Dockerfile，使用随机项目名、独立卷和随机管理入口端口，不读取 `deploy/.env`，API/数据库不发布宿主端口。三个渠道均为进程内 MockTransport，使用假凭据。检查迁移失败阻断、空卷启动/重复初始化、非 root 运行、真实 Nginx/Chromium 登录与深链刷新、Cookie/CSRF、三个模拟渠道及 SSE、真实 DBOS/Harness、联合恢复和保留卷重建。

恢复检查包含错误文件卷的负例、原登录/角色和文件授权、字典/审计、字节/费用/步骤历史，以及停止 Worker 后提交的 pending Flow 恢复完成；它不是任意执行位置的崩溃恢复矩阵，后者仍由既有 Flow 后端测试覆盖。

日志、构建记录、恢复样本、截图及 `result.json` 保存在 `.artifacts/deploy-verify/<随机目录>/`。脚本失败也清理自己生成的两个项目及卷；只在全部检查和清理成功后输出最终 PASS。`--skip-build` 仅用于调试已构建的 `:verify` 镜像，正式验收使用默认命令。镜像缓存保留，不执行全局 prune。

本地验收不覆盖生产 TLS/证书、备份加密和异地保存设施、S3/NAS、多宿主、多个 Worker 或真实渠道；这些配置在目标部署环境单独验证。

## 多存储源的发布与备份

文件多源配置和完整 JSON 示例见[配置文档](configuration.md#4-同时配置本地与-s3)。Compose 仍挂载 file_data:/data/files 用于 local 源和协调锁；新增 S3 源无需增加常驻服务。所有内容源都通过 FILE_STORAGES 显式配置，FILE_COORDINATION_ROOT 独立指定协调目录；纯 S3 内容源也使用协调卷。多源字段包含在 0004_files 初始结构中，新库按首次启动流程初始化六份领域结构。文件卷保留 `.keystone-storage`。init-storage 不创建 S3 bucket。

此前的数据库+file_data 备份命令覆盖默认本地部署。启用其他 filesystem 源或 S3 后，须在同一停写窗口额外保留所有源的内容、源键及原配置身份；按恢复点保存桶快照或对象版本，并在恢复时还原到匹配的 endpoint/bucket/region/prefix。仅备份本地卷不能覆盖云端文件，仅复制对象也不能替代数据库元数据、授权、引用及 DBOS 备份。应用中的两份 File 是业务关联文件，不自动形成灾备恢复点。

S3 deleted 元数据用于持续复核迟到 PUT，不能在数据库恢复或维护中随意丢弃。版本化桶的历史版本和未完成 multipart 由桶策略清理；应用 DELETE 不代表永久删除所有历史版本。源移除或身份改变时历史文件访问和清理都会失败，先完成业务迁移，再停用旧源。
