# 应用配置与环境变量

默认开发流程中，Compose 只运行 PostgreSQL，API、Worker 和前端在本机运行。

| 配置文件 | 使用方 | 默认是否需要 |
|---|---|---|
| [backend/keystone.toml.example](../../backend/keystone.toml.example) → `backend/keystone.toml` | 本机 API、Worker、CLI | 需要，与开发数据库默认值一致 |
| [frontend/console/.env.example](../../frontend/console/.env.example) → `frontend/console/.env` | Vite 的 `KEYSTONE_API_PROXY` | 可选，默认代理本机 8000 端口 |
| [deploy/.env.example](../../deploy/.env.example) → `deploy/.env` | PostgreSQL 密码、宿主端口等覆盖 | 可选，默认启动不需要复制 |
| [deploy/stack/.env.example](../../deploy/stack/.env.example)、[keystone.toml.example](../../deploy/stack/keystone.toml.example) → 同目录 `.env`、`keystone.toml` | [单宿主 Compose](deployment.md#23-单宿主-compose) 的镜像、秘密与应用配置 | 仅该部署方式需要 |

## 1. 读取方式

**后端**：从 `backend/` 启动，默认读取当前目录的 `keystone.toml`；其他路径设置 `KEYSTONE_CONFIG_FILE=/path/to/keystone.toml`。优先级为程序直接传入的参数、进程环境变量、TOML、代码默认值；API、Worker 和 CLI 规则相同，不读取后端 `.env`。TOML 不展开 `${...}`，秘密通过环境变量覆盖。相对的文件存储路径（含环境变量覆盖）以配置文件所在目录为基准。默认 TOML 不存在时可使用纯环境变量；显式指定的路径不存在、TOML 语法错误、字段值不合法或遇到未作兼容处理的未知字段时直接报错。修改后重启相应 API/Worker。

**前端**：`pnpm dev` 默认代理 `http://127.0.0.1:8000`，需要改变时设置 `KEYSTONE_API_PROXY`；Vite 按进程环境、`.env.[mode].local`、`.env.[mode]`、`.env.local`、`.env` 读取。前端不读取后端 TOML，浏览器不接收数据库或存储秘密。

**依赖 Compose**：`docker compose -f deploy/compose.yaml up -d --wait` 只启动 PostgreSQL，默认数据库和用户均为 `keystone`，本地密码 `keystone_local_only`，端口 `127.0.0.1:55432`。覆盖时复制 `deploy/.env.example` 为 `deploy/.env` 并加 `--env-file deploy/.env`；已有 shell 变量优先。依赖服务只读取 `POSTGRES_PASSWORD`、`KEYSTONE_POSTGRES_PORT` 和可选 `KEYSTONE_POSTGRES_IMAGE`。

## 2. 应用参数

示例配置即可日常启动；并发和退出等待有内置默认值，下表相应字段仅按需调优。Flow 始终启用。DBOS 执行线程池及队列预算见[任务执行](../execution.md#72-队列预算)。TOML 字段使用小写；对应环境变量加 `KEYSTONE_` 前缀并使用大写：

| TOML 字段 | 环境变量 | 默认值或用途 |
|---|---|---|
| `env` | `KEYSTONE_ENV` | `development`，可选 `test`、`production` |
| `database_url` | `KEYSTONE_DATABASE_URL` | 必须使用 `postgresql+psycopg`；示例连接本机 55432 端口 |
| `database_connect_timeout_seconds` | `KEYSTONE_DATABASE_CONNECT_TIMEOUT_SECONDS` | 默认 10，范围 1–60；业务连接池新建连接的等待上限，超时在事务开始前返回 `database_connection_busy` |
| `host` / `port` | `KEYSTONE_HOST` / `KEYSTONE_PORT` | `127.0.0.1` / `8000` |
| `opensandbox_url` / `opensandbox_key` / `browser_runtime_image` | `KEYSTONE_OPENSANDBOX_URL` / `KEYSTONE_OPENSANDBOX_KEY` / `KEYSTONE_BROWSER_RUNTIME_IMAGE` | 可选的[交互环境](../domains/environments/README.md)；OpenSandbox 地址、API Key 和固定的浏览器运行镜像，三者必须同时配置；API 与 Worker 共用 |
| `browser_backup_key` | `KEYSTONE_BROWSER_BACKUP_KEY` | 可选；32 字节十六进制备份密钥。设置后启用浏览器环境备份，且 `file_default_storage` 必须是本地 `filesystem` |
| `log_level` | `KEYSTONE_LOG_LEVEL` | `INFO` |
| `session_hours` / `pat_max_days` | `KEYSTONE_SESSION_HOURS` / `KEYSTONE_PAT_MAX_DAYS` | 72 小时 / 90 天 |
| 历史保留与自动清理 | `KEYSTONE_MAINTENANCE_CLEANUP_ENABLED` 等 | 默认仅清理超过 14 天的 DBOS 维护历史；见[历史清理](deployment.md#历史保留与清理) |
| `[file_storages.<源名>]` | `KEYSTONE_FILE_STORAGES__<源名>__<字段>` | 逐项配置内容源，见第 4 节 |
| `file_coordination_root` | `KEYSTONE_FILE_COORDINATION_ROOT` | 节点本地目录，供临时文件、磁盘预留与本地锁使用；示例为 `../.data/files` |
| `credential_key` | `KEYSTONE_CREDENTIAL_KEY` | 见[凭据加密密钥](#41-凭据加密密钥) |
| `file_default_storage` | `KEYSTONE_FILE_DEFAULT_STORAGE` | `local`，必须指向已配置源 |
| `file_max_bytes` | `KEYSTONE_FILE_MAX_BYTES` | 默认 16 MiB；本地可配置到 1 TiB，同时受代理限制；S3 保留 64 MiB 上限；保存同步图片产物的要求见[产物证据](../reference/model-access.md#产物证据) |
| `file_min_free_bytes` | `KEYSTONE_FILE_MIN_FREE_BYTES` | 默认 1 GiB，范围 0–1 TiB；节点临时文件所在磁盘及本地正式存储各自保留的最低可用空间 |
| `file_upload_timeout_seconds` | `KEYSTONE_FILE_UPLOAD_TIMEOUT_SECONDS` | 默认 3600 秒，范围 1–86400；包含上传接收、写入与确认的总期限 |
| `file_unreferenced_grace_seconds` | `KEYSTONE_FILE_UNREFERENCED_GRACE_SECONDS` | 默认未设置、不回收无引用 active 文件；显式设为 1–31536000 秒前，须核对调用方及存量引用 |
| `flow_worker_concurrency` | `KEYSTONE_FLOW_WORKER_CONCURRENCY` | 默认 2，范围 1–16；单 Worker 的持久队列并发 |
| `flow_global_concurrency` | `KEYSTONE_FLOW_GLOBAL_CONCURRENCY` | 默认 4，范围 1–64；全部 Worker 的默认 Flow 队列并发，不小于单进程并发 |
| `worker_id` | `KEYSTONE_WORKER_ID` | 默认 `keystone-worker`；每个 Worker 单独指定固定身份，探针使用同一身份，见[多 Worker](deployment.md#4-多-worker-部署与发布) |
| `worker_shutdown_seconds` | `KEYSTONE_WORKER_SHUTDOWN_SECONDS` | 默认 10，范围 1–600；停止领取后的有界等待 |
| `model_cost_concurrency` | `KEYSTONE_MODEL_COST_CONCURRENCY` | 默认 2，范围 1–8；API 手动补查和全部 Worker 的查费总预算 |
| `model_inference_concurrency` | `KEYSTONE_MODEL_INFERENCE_CONCURRENCY` | 默认 4，范围 1–32；API 与 Worker 共享的模型发送总预算 |
| `model_scope_concurrency` | `KEYSTONE_MODEL_SCOPE_CONCURRENCY` | 默认 4，范围 1–16，且不超过发送总预算；相同推理站点/真实 Token 共用上限 |
| `model_send_interval_ms` / `model_cooldown_seconds` / `model_cooldown_max_seconds` | `KEYSTONE_MODEL_SEND_INTERVAL_MS` 等 | 同站点/Token 的发送间隔与 429 冷却，范围与语义见[发送与协调](../reference/model-access.md#发送与协调) |
| `initial_admin_password` | `KEYSTONE_INITIAL_ADMIN_PASSWORD` | 可选，仅供空安装首次初始化；设置后交互式 `keystone init` 也直接采用，已有用户时忽略 |

嵌套环境变量用双下划线 `__` 分隔层级，例如 `KEYSTONE_FILE_STORAGES__LOCAL__ROOT=/data/files` 只覆盖 `local` 源的根目录。源名称以小写字母开头，可包含数字、单下划线和连字符，不能含连续双下划线；环境变量中的名称使用大写。

`browser_backup_key` 由部署方保存在 Keystone 配置之外的安全位置；不要从备份文件、数据库或空间卷推导，轮换时不覆盖仍被旧备份引用的密钥。运行镜像见 [`deploy/environments/browser`](../../deploy/environments/browser/README.md)。

## 3. 配置变更

执行、预算、文件和交互环境提供方配置进入 Worker 公共配置校验，API/Worker 必须一致，修改时停止全部旧进程后统一发布（见[更新与恢复](deployment.md#3-更新与恢复)）；固定身份不参与。本地媒体可设置 `file_max_bytes = 17179869184`（16 GiB），容量预留与错误语义见[文件容量](../domains/files.md#51-文件容量与临时目录)。不要手动清理节点目录下的 `.capacity`、`.locks` 或受管 `files`。默认 Nginx API 正文上限为 16 MiB；接入大文件业务时只给实际上传/内容路由设置正文上限、传输期限及 `proxy_request_buffering off` / `proxy_buffering off`，与后端限制一致。实际 NAS 吞吐及容量须部署时实测。

模型渠道和凭据在[控制台手动配置](../domains/model-access.md#51-配置)，保存后无需重启。

更改开发数据库密码或宿主端口时，同步修改 `database_url`（URL 特殊字符需编码）。`POSTGRES_PASSWORD` 仅初始化空数据库卷，已有数据库改密须执行实际数据库操作。

## 4. 文件存储与部署拓扑

至少配置一个文件源；部署拓扑由全部 `file_storages` 决定，与默认源无关：

| 内容源 | 数据库绑定 | 节点本地目录 |
|---|---|---|
| 含 `filesystem`（含混合存储） | 进程可见的宿主名及 `file_coordination_root` 的 `.keystone-storage` 标识 | 限单宿主，所有进程共用同一实际节点目录和正式文件目录。不能用相同路径、伪造宿主名或 NFS 替代共用本地目录 |
| 仅 `s3` | 共享拓扑与凭据加密密钥指纹，不绑定宿主或节点目录 | 各节点使用自己的可写本地目录，同节点进程共用；路径和磁盘余量可按节点配置 |

绑定建立后不能靠改配置切换拓扑；不要删改绑定记录。写入和校验规则见[部署初始化](deployment.md#2-运行配置与初始化)。

下面是混合源示例，顶层字段须放在源分节之前。仅用本地存储时省略 `cloud`；仅用 S3 时移除整个 `local` 分节、将 `file_default_storage` 设为 `cloud`，并按下节配置密钥：

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

S3 凭据由运行环境注入（如 `KEYSTONE_FILE_STORAGES__CLOUD__ACCESS_KEY` / `__SECRET_KEY`），或写在本机未跟踪的 TOML 对应分节。S3 要求 endpoint、bucket 和凭据，region 默认 `us-east-1`，prefix 可选且不能含路径穿越；bucket 需预先存在，使用 path-style 请求，不读取主机默认 AWS 凭据或 EC2 metadata。需要前缀对象的读写删除权限，就绪探针按所用 OpenDAL 版本可能还需要 ListBucket。

上传可使用 `storage_key=cloud`，省略时使用默认源；切换默认源只影响新请求。保留已有文件使用的源名与[位置身份](../reference/files.md#存储与身份)，否则文件不能读取或清理。

### 4.1 凭据加密密钥

只从配置取密钥：私有 TOML 的顶层字符串 `credential_key`，或优先级更高的 `KEYSTONE_CREDENTIAL_KEY`。值为 Fernet 格式（32 字节密钥的规范 URL-safe Base64，保留 `=` 填充）；未配置时省略，不填空字符串。不会生成或读取本地密钥文件，也不会回退到明文。

- **仅 S3**：即使不使用渠道也必填，所有节点的 API、Worker 和 CLI 使用同一个值；更换值会与绑定指纹冲突。
- **含 `filesystem`**：启动可不配置，但保存或使用模型、消息渠道的加密凭据时必填。

新部署准备好私有 `keystone.toml` 后，在 `backend/` 执行：

```sh
uv run --locked keystone init-credential-key
```

命令不连接数据库，不打印密钥，不覆盖已有有效值。可用 `KEYSTONE_CONFIG_FILE` 指向其他目录下名为 `keystone.toml` 的私有文件。跨机部署只生成一次，再通过私有配置或秘密管理系统分发同一密钥；已有密文需要原密钥。

配置变更后统一重启 API 和 Worker；密钥按[备份与恢复](backup-restore.md)单独保护。
