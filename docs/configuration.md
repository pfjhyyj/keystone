# 应用配置与环境变量

默认开发流程中，Compose 只运行 PostgreSQL，API、Worker 和前端在本机运行。后端统一使用 TOML；前端和依赖 Compose 保留各自原有配置方式。平台启动不需要模型 Key。

| 配置文件 | 使用方 | 默认是否需要 |
|---|---|---|
| [backend/keystone.toml.example](../backend/keystone.toml.example) → `backend/keystone.toml` | 本机 API、Worker、CLI | 需要，与开发数据库默认值一致 |
| [frontend/console/.env.example](../frontend/console/.env.example) → `frontend/console/.env` | Vite 的 `KEYSTONE_API_PROXY` | 可选，默认代理本机 8000 端口 |
| [deploy/.env.example](../deploy/.env.example) → `deploy/.env` | PostgreSQL 密码、宿主端口等覆盖 | 可选，默认启动不需要复制 |

## 1. 读取方式

**后端**：从 `backend/` 启动，默认读取当前目录的 `keystone.toml`。优先级为进程环境变量、TOML、代码默认值；程序直接传入的参数优先级最高。API、Worker 和 CLI 使用相同规则，不再读取后端 `.env`。旧配置需转写到 TOML，保留原数据库连接、源名称和存储目录。TOML 不展开 `${...}`，秘密可直接通过环境变量覆盖。

需要从其他目录启动或使用其他配置路径时，设置 `KEYSTONE_CONFIG_FILE=/path/to/keystone.toml`。相对的文件存储路径（含环境变量覆盖）以配置文件所在目录为基准。默认 TOML 不存在时可使用纯环境变量配置；显式指定的配置路径不存在、TOML 语法错误、字段未知或值不合法时直接报错。修改应用配置后重启相应 API/Worker。

**前端**：`pnpm dev` 默认代理 `http://127.0.0.1:8000`。需要改变时设置 `KEYSTONE_API_PROXY`；Vite 按进程环境、`.env.[mode].local`、`.env.[mode]`、`.env.local`、`.env`、默认值读取。前端配置方式不变，不读取后端 TOML，浏览器不接收数据库或存储秘密。

**依赖 Compose**：`docker compose -f deploy/compose.yaml up -d --wait` 只启动 PostgreSQL。默认数据库和用户均为 `keystone`，本地密码 `keystone_local_only`，端口 `127.0.0.1:55432`。需要覆盖时使用：

```bash
cp deploy/.env.example deploy/.env
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait
```

已有 shell 变量优先于 Compose 环境文件，再使用 Compose 默认值。依赖服务只读取 `POSTGRES_PASSWORD`、`KEYSTONE_POSTGRES_PORT` 和可选 `KEYSTONE_POSTGRES_IMAGE`，不读取后端 TOML。

## 2. 应用参数

TOML 字段使用小写；对应环境变量加 `KEYSTONE_` 前缀并使用大写：

| TOML 字段 | 环境变量 | 默认值或用途 |
|---|---|---|
| `env` | `KEYSTONE_ENV` | `development`，可选 `test`、`production` |
| `database_url` | `KEYSTONE_DATABASE_URL` | 必须使用 `postgresql+psycopg`；示例连接本机 55432 端口 |
| `host` / `port` | `KEYSTONE_HOST` / `KEYSTONE_PORT` | `127.0.0.1` / `8000` |
| `browser_origins` | `KEYSTONE_BROWSER_ORIGINS` | 精确的浏览器来源数组；生产要求 HTTPS |
| `log_level` | `KEYSTONE_LOG_LEVEL` | `INFO` |
| `session_hours` / `pat_max_days` | `KEYSTONE_SESSION_HOURS` / `KEYSTONE_PAT_MAX_DAYS` | 8 小时 / 90 天 |
| `[file_storages.<源名>]` | `KEYSTONE_FILE_STORAGES__<源名>__<字段>` | 逐项配置内容源，见第 4 节 |
| `file_coordination_root` | `KEYSTONE_FILE_COORDINATION_ROOT` | 本地共享协调目录，示例为 `../.data/files` |
| `file_default_storage` | `KEYSTONE_FILE_DEFAULT_STORAGE` | `local`，必须指向已配置源 |
| `file_max_bytes` | `KEYSTONE_FILE_MAX_BYTES` | 默认 16 MiB；本地可配置到 1 TiB，同时受临时配额和代理限制；S3 保留 64 MiB 上限 |
| `file_transfer_slots` | `KEYSTONE_FILE_TRANSFER_SLOTS` | 默认 4，范围 1–32；API/Worker 共用本地传输与临时文件槽位 |
| `file_temp_bytes` | `KEYSTONE_FILE_TEMP_BYTES` | 默认 64 GiB；每槽临时文件上限为总额除以槽数，同时不能超过 file_max_bytes |
| `file_min_free_bytes` | `KEYSTONE_FILE_MIN_FREE_BYTES` | 默认 1 GiB，范围 0–1 TiB；协调卷、临时卷及本地正式存储各自保留的最低可用空间 |
| `file_upload_timeout_seconds` | `KEYSTONE_FILE_UPLOAD_TIMEOUT_SECONDS` | 默认 3600 秒，范围 1–86400；包含 API 槽位排队、上传接收与确认的总期限 |
| `flow_worker_concurrency` | `KEYSTONE_FLOW_WORKER_CONCURRENCY` | 默认 2，范围 1–16；单 Worker 的持久队列并发 |
| `flow_enabled` | `KEYSTONE_FLOW_ENABLED` | `true`，API 与 Worker 保持一致 |
| `initial_admin_password` | `KEYSTONE_INITIAL_ADMIN_PASSWORD` | 可选，仅供空安装非交互初始化；默认交互输入，已有用户时忽略 |

嵌套环境变量用双下划线 `__` 分隔层级。例如 `KEYSTONE_FILE_STORAGES__LOCAL__ROOT=/data/files` 只覆盖 `local` 源的根目录。源名称以小写字母开头，可包含数字、单下划线和连字符，不使用连续双下划线；环境变量中的名称使用大写。数组在 TOML 中直接填写；通过环境变量覆盖 `browser_origins` 时使用 JSON 数组，例如 `KEYSTONE_BROWSER_ORIGINS='["https://console.example.com"]'`。

后端镜像默认监听 `0.0.0.0:8000`，配置在运行时挂载，真实 `keystone.toml` 不进入构建上下文，详见[打包与发布](deployment.md)。

本地媒体可从 `file_max_bytes = 17179869184`（16 GiB）、4 个槽位、64 GiB 临时预算开始配置。这个预算限制 `.transfers` 工作副本，不包含正式文件容量；流式上传在读取正文前同时预留临时副本和正式副本，同盘累计、异盘分别检查，并为协调卷保留最低可用空间。预留由共享本地目录中的 OS 锁协调，不按 PID 或超时猜测进程存活；已写入并刷新的顺序写入会扣减预留，外部工具写入则在上下文结束前保留完整预留，并监测空间和产物大小。进程崩溃后的预留及临时残留仅在对应锁已释放时回收。

临时副本在整个上下文中占槽，处理期间再上传文件会占第二个槽，因此应按业务并发留出余量。普通上传和 Worker 默认满载返回 `503 file_transfer_busy`；HTTP 文件内容读取最多等待 3 秒，取得槽位后重新校验当前身份、文件状态及授权，客户端断开会取消等待。空间不足或系统报告磁盘/配额已满返回 `507 file_capacity_full`，容量账本不可用返回 `503 file_capacity_unavailable`，容量锁等待超过 5 秒返回 `503 file_capacity_busy`；每槽配额不足返回 `413 file_temporary_limit`。预留只协调本平台进程，不能阻止其他程序占用磁盘，也不为远端 S3 容量作保证。

API/Worker 必须保持相同协调目录、槽位数、临时预算、磁盘保底和大小限制；升级或调整配置前停止全部旧进程，不允许不支持容量预留的旧进程混跑。不要手动清理 `.capacity`、`.locks` 或受管 `files`。字节可放 filesystem/NAS，协调目录仍须是同宿主可靠本地卷。当前 Nginx 文件路由为 16 GiB 上限、3600 秒上游期限，关闭请求/响应完整缓存；放大文件和时间限制时同时调整代理。上限是配置边界，实际 NAS 吞吐及容量须部署时实测。

## 3. 配置变更

模型配置仍在控制台手动完成，步骤见 [README](../README.md#模型配置手动)。API Key 加密保存到数据库，编辑时留空保留原值，保存后 API 与 Worker 直接使用，无需重启。加密密钥首次保存时自动写入已有共享协调目录；`keystone init` 不创建模型配置或模型加密密钥。

更改开发数据库密码或宿主端口时，同步修改 `keystone.toml` 的 `database_url` 或对应环境变量；URL 特殊字符需编码。`POSTGRES_PASSWORD` 仅初始化空数据库卷，已有数据库改密须执行实际数据库操作。修改浏览器入口或 API 地址时同步 Origin 与 Vite 代理。

## 4. 同时配置本地与 S3

后端 `keystone.toml` 示例：顶层字段放在存储源分节前面。

```toml
file_coordination_root = "../.data/files"
file_default_storage = "local"

[file_storages.local]
driver = "filesystem"
root = "../.data/files"

[file_storages.cloud]
driver = "s3"
endpoint = "https://s3.example.com"
bucket = "keystone-backups"
region = "us-east-1"
prefix = "documents"
```

S3 凭据可由运行环境单独注入，也可填写在本机未跟踪的 TOML 对应分节中：

```bash
export KEYSTONE_FILE_STORAGES__CLOUD__ACCESS_KEY='example-access'
export KEYSTONE_FILE_STORAGES__CLOUD__SECRET_KEY='example-secret'
```

S3 要求 endpoint、bucket 和凭据，region 默认 `us-east-1`；prefix 可选，不能含路径穿越。bucket 需预先存在；使用 path-style 请求，不读取主机默认 AWS 凭据或 EC2 metadata。需要前缀对象读写删除及就绪探针的 ListBucket 权限。`keystone init` 已包含协调目录和 filesystem 源初始化，无需增加初始化步骤。

`POST /api/v1/files` 可传 `storage_key=cloud`；省略时使用默认源。切换默认源只影响新请求，已有幂等请求仍定位原源。已有文件使用中的源名称和位置身份必须保留；更换 bucket、endpoint、prefix 或 region 时应创建新源并迁移业务数据，凭据轮换不改变存储身份。删除源配置后其历史文件不可读、不可清理，不会回退其他源。

协调目录和内容目录分别配置，可共用路径；API 与 Worker 须共享同一宿主协调卷，不支持 NFS 或多宿主锁。依赖 Compose 不管理应用文件。Flow 默认启用，无需 Redis 或模型发送模式开关。

## 5. 初始化时文件存储未配置

`keystone init` 报 `storage_unavailable` 并提示 `file_storages`，说明尚未配置内容源。先确认从 `backend/` 启动，已将 `keystone.toml.example` 复制为 `keystone.toml`。旧后端 `.env` 不再读取。README 保留已有 TOML，不自动补齐缺项。

默认本机文件配置如下；已有字段直接修改，不要重复分节或覆盖原数据库连接：

```toml
file_coordination_root = "../.data/files"
file_default_storage = "local"

[file_storages.local]
driver = "filesystem"
root = "../.data/files"
```

已有文件数据时保留原目录、源名称和卷标识。补齐后重新执行 `uv run --locked keystone init`，已有迁移、数据和管理员不会重置。需要恢复默认开发配置时按 [README 重置步骤](../README.md#已有旧环境重新测试可选) 操作；重置配置与清空数据是独立操作。
