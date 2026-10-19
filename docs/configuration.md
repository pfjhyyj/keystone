# 环境变量与配置

默认开发流程中，Compose 只运行 PostgreSQL，API、Worker 和前端在本机运行。平台启动不需要模型 Key；模型凭据与后台配置由操作者之后手动设置。

| 配置文件 | 使用方 | 默认是否需要 |
|---|---|---|
| [backend/.env.example](../backend/.env.example) → `backend/.env` | 本机 API、Worker、CLI | 需要，示例与开发数据库默认值一致 |
| [frontend/console/.env.example](../frontend/console/.env.example) → `frontend/console/.env` | Vite 的 `KEYSTONE_API_PROXY` | 可选，默认代理本机 8000 端口 |
| [deploy/.env.example](../deploy/.env.example) → `deploy/.env` | PostgreSQL 密码、宿主端口等覆盖 | 可选，默认启动不需要复制 |

## 1. 读取方式

**后端**：从 `backend/` 启动，读取该目录的 `.env`；优先级为进程环境、`.env`、代码默认值。API、Worker 和 CLI 使用相同数据库、文件存储及凭据配置，修改后重启相关应用进程。

**前端**：`corepack pnpm dev` 使用默认 API 代理 `http://127.0.0.1:8000`。需要改变时设置 `KEYSTONE_API_PROXY`，Vite 按进程环境、`.env.[mode].local`、`.env.[mode]`、`.env.local`、`.env`、默认值读取。浏览器请求同源 `/api`，前端不存放模型或数据库秘密。

**依赖 Compose**：`docker compose -f deploy/compose.yaml up -d --wait` 只启动 PostgreSQL。默认数据库和用户均为 `keystone`，本地密码 `keystone_local_only`，端口 `127.0.0.1:55432`；这些值与后端示例匹配。需要覆盖时使用：

```bash
cp deploy/.env.example deploy/.env
# 编辑后通过同一个环境文件执行依赖服务命令。
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait
```

Compose 中已有 shell 变量优先于环境文件，再使用代码默认值。依赖服务只读取 `POSTGRES_PASSWORD`、`KEYSTONE_POSTGRES_PORT` 和可选 `KEYSTONE_POSTGRES_IMAGE`，不接收应用秘密、管理员密码或模型配置。

## 2. 应用参数

| 参数 | 默认值或用途 |
|---|---|
| `KEYSTONE_DATABASE_URL` | 本机后端示例连接 `127.0.0.1:55432/keystone`，必须使用 `postgresql+psycopg` |
| `KEYSTONE_HOST` / `KEYSTONE_PORT` | 本机 API 默认 `127.0.0.1:8000` |
| `KEYSTONE_BROWSER_ORIGINS` | 精确的浏览器来源；本机包括 5173 与 8080，生产要求 HTTPS |
| `KEYSTONE_LOG_LEVEL` | `INFO` |
| `KEYSTONE_SESSION_HOURS` / `KEYSTONE_PAT_MAX_DAYS` | 8 小时 / 90 天 |
| `KEYSTONE_FILE_STORAGES` | 应用必须显式配置内容源；后端示例为本地 `.data/files` |
| `KEYSTONE_FILE_COORDINATION_ROOT` | 必须是本地共享协调目录；后端示例为 `.data/files` |
| `KEYSTONE_FILE_DEFAULT_STORAGE` | `local`，必须指向已配置的源 |
| `KEYSTONE_FILE_MAX_BYTES` | 16 MiB，最大 64 MiB；使用 Nginx 镜像时同步调整代理限制 |
| `KEYSTONE_MODEL_SECRETS` | 默认 `{}`；需要模型时手动注入引用名到真实秘密的映射 |
| `KEYSTONE_FLOW_ENABLED` | 默认 `true`；Worker 执行流程，不另需消息队列服务 |
| `KEYSTONE_INITIAL_ADMIN_PASSWORD` | 可选，仅供非交互 `keystone init`；本机首次默认交互输入，已有用户时忽略 |
| `KEYSTONE_WORKER_READY_FILE` | 默认未设置；独立健康探针按需配置，见[开发说明](development.md#worker-健康检查排障) |

构建后的容器通过运行环境显式注入这些参数，文件目录使用实际挂载路径。后端镜像默认监听 `0.0.0.0:8000`；前端 Nginx 镜像默认代理同 Docker 网络的 `api:8000`。镜像构建不读取实际 `.env`，具体边界见[打包与发布](deployment.md)。

## 3. 配置变更

模型配置是手动操作，步骤见 [README](../README.md#模型配置手动)。实际密钥留在服务端环境中，后台只保存引用名；`keystone init` 不读取模型来创建渠道或模型记录。修改凭据后同时重启 API 与 Worker。

JSON 值用单引号包裹，保留内部双引号。后端 dotenv 会展开 `${...}`，包含这类内容的秘密应通过进程环境注入。

更改开发数据库密码或宿主端口时，同步修改 `backend/.env` 的 URL；URL 中的特殊字符需编码。`POSTGRES_PASSWORD` 仅用于初始化空数据库卷，已有数据库改密须执行实际数据库改密操作。修改浏览器入口或 API 地址时同步对应 Origin 与 Vite 代理。

## 4. 同时配置本地与 S3

后端 `.env` 例子（仅示例凭据；真实值勿提交）：

```dotenv
KEYSTONE_FILE_COORDINATION_ROOT=../.data/files
KEYSTONE_FILE_DEFAULT_STORAGE=local
KEYSTONE_FILE_STORAGES='{"local":{"driver":"filesystem","root":"../.data/files"},"cloud":{"driver":"s3","endpoint":"https://s3.example.com","bucket":"keystone-backups","region":"us-east-1","prefix":"documents","access_key":"example-access","secret_key":"example-secret"}}'
```

打包后的 API/Worker 使用相同 JSON，但 local.root 须填写实际挂载的容器路径。参数由应用部署环境注入；控制台不接收配置或凭据。初始化执行 `keystone init-storage`，创建/校验协调目录及各 filesystem 源；S3 bucket 须预先创建。

S3 必须显式填写 endpoint、bucket、region（默认 us-east-1）、access_key、secret_key；prefix 可选，不能含路径穿越。使用 path-style 请求，不读取主机默认 AWS 凭据或 EC2 metadata。需要对应前缀的对象读写删除权限及就绪探针的 ListBucket 权限；不需要应用创建或删除 bucket。filesystem 和 S3 配置都必须显式提供 FILE_COORDINATION_ROOT，协调目录可以与某个本地内容源共用同一路径，但两者分别配置，不互相推导。

POST /api/v1/files 可传 storage_key=cloud；省略使用默认源，新 File 保存源键及位置指纹。已有文件的源键不随默认配置变化，现有幂等请求省略源时继续定位原源。同一请求键改源报冲突；业务创建本地/云端两份文件应使用不同请求键，并维护两份引用及备份关系。详情仅返回源键。

单本地源使用 FILE_STORAGES 显式声明 driver 与 root，后端示例已配置开发目录；依赖 Compose 不管理应用文件。空映射表示文件存储未配置，协调目录不会自动变成内容源。多源字段直接包含在初始文件结构中，File 的 storage_key 必须由上传服务写入，数据库没有隐式 local 默认值；已有文件的源键及原存储身份须保留，删除源配置会使该源历史文件不可读、不可清理，不会回退到其他源。更换 bucket/endpoint/prefix/region 属于目标变化；使用原源身份的文件会拒绝访问，应使用新源键及业务迁移。密钥轮换不改变存储身份。各进程仍须共享同一宿主协调卷，不提供多宿主锁。

Flow 默认启用，`KEYSTONE_FLOW_ENABLED=false` 只用于明确不执行流程的环境；模型始终使用真实服务，无需发送模式开关。已有实际 `.env` 的显式值优先；旧环境若仍显式设为 false，需同步改为 true。Casbin 权限判断保留。
