# 环境变量与三层配置

三层均提供 `.env.example`，完整列出各层保留的配置并附简短注释。复制到同目录 `.env`，已有文件直接编辑。实际 `.env`、`.env.local`、`.env.live` 被 Git 忽略，示例只使用本地开发密码。

| 层次 | 示例 | 使用方 |
|---|---|---|
| 后端 | [backend/.env.example](../backend/.env.example) | 本机 API、Worker、CLI：数据库、监听端口、浏览器来源、文件根目录、模型和 Flow |
| 前端 | [frontend/admin/.env.example](../frontend/admin/.env.example) | Vite 开发服务器：`KEYSTONE_API_PROXY` 代理目标 |
| 部署编排 | [deploy/.env.example](../deploy/.env.example) | Compose：数据库初始化、后端参数、宿主端口 |

本机启动前后端时准备对应文件；使用 Compose 提供开发数据库时再准备部署文件。整套 Compose 部署只需部署文件，不读取本机前后端的 `.env`。API、Worker 和一次性 CLI 共用后端配置模型。

## 1. 读取方式

**后端**：从 `backend/` 启动，读取当前工作目录的 `.env`。优先级为进程环境变量、`.env`、代码默认值；修改后重启 API/Worker。`KEYSTONE_PORT` 是本机 API 监听端口，修改时同步调整前端代理目标；浏览器来源填写访问后台时的协议、主机和端口，不带路径或尾斜杠，生产要求 HTTPS。

**前端**：从 `frontend/admin/` 启动 `pnpm dev`。Vite 配置通过 `loadEnv` 读取 `KEYSTONE_API_PROXY`，优先级从高到低为进程环境变量、`.env.[mode].local`、`.env.[mode]`、`.env.local`、`.env`、默认目标 `http://127.0.0.1:8000`；修改后重启 Vite。E2E 脚本提供的随机后端地址优先于个人文件。

浏览器继续请求同源 `/api`，开发由 Vite 代理，生产由 Nginx 转发到 `api:8000`；后台路径为 `/admin/`。不需要 `VITE_API_URL`；前端环境文件不放后端秘密，自定义 `VITE_*` 值会在构建时进入浏览器代码。给已构建的 Nginx 容器新增环境变量不会改变静态页面。

**Compose**：从仓库根目录显式选择部署文件，例如 `docker compose --env-file deploy/.env -f deploy/compose.yaml config -q`。已有 shell 变量优先于部署文件，再使用 Compose 默认值。部署文件用于插值，只有服务 `environment` 声明的变量才注入容器：

| 配置 | 传递范围 |
|---|---|
| `KEYSTONE_ENV`、`KEYSTONE_DATABASE_URL`、`KEYSTONE_BROWSER_ORIGINS`、`KEYSTONE_MODEL_MODE`、`KEYSTONE_MODEL_SECRETS`、`KEYSTONE_FLOW_ENABLED` | API、Worker、migrate、flow-migrate、storage-init；下表注明支持部署配置的日志、安全策略、上传限制和具名存储配置也传给这五个服务 |
| `POSTGRES_PASSWORD` | 仅 PostgreSQL |
| `KEYSTONE_POSTGRES_PORT`、`KEYSTONE_API_PORT`、`KEYSTONE_ADMIN_PORT` | 仅宿主端口映射，默认 55432、8000、8080，绑定 `127.0.0.1` |

容器内部固定 `KEYSTONE_HOST=0.0.0.0`、`KEYSTONE_PORT=8000`、`KEYSTONE_FILE_COORDINATION_ROOT=/data/files`、`KEYSTONE_WORKER_READY_FILE=/tmp/keystone-worker.ready`。迁移从源码的 `backend/migrations` 或已安装容器工作目录下的 `migrations/` 定位，不提供路径配置。数据库就绪检查固定 3 秒，Worker 维护和 Flow 轮询固定 5 秒。`KEYSTONE_API_PORT` 不改变 Nginx 上游或健康检查地址。admin 不接收后端秘密，镜像构建不复制实际环境文件。

## 2. 配置对应

后端示例覆盖全部 16 个 Settings 字段：健康文件默认关闭，以注释形式给出可用路径，其余 15 项直接赋值。前端示例列出唯一的开发代理参数。部署示例完整列出 19 个 Compose 插值输入；容器内部固定值只在 Compose 中声明，避免在 `.env` 中列出不会生效的覆盖项。新增或删除参数时，同步更新实际读取、对应示例、Compose 和文档。

| 参数 | 默认值 | 可配置位置 |
|---|---|---|
| `KEYSTONE_LOG_LEVEL` | `INFO` | 后端、部署 |
| `KEYSTONE_WORKER_READY_FILE` | 未设置，不写健康文件 | 后端示例提供注释路径，按需取消注释，父目录须存在；Compose 固定 `/tmp/keystone-worker.ready`。探针使用同一路径，每个进程独占文件 |
| `KEYSTONE_SESSION_HOURS` | 8 小时 | 后端、部署 |
| `KEYSTONE_PAT_MAX_DAYS` | 90 天 | 后端、部署 |
| `KEYSTONE_FILE_STORAGES` | 后端未配置时为 `{}`；Compose 必填 | 后端、部署；显式 JSON 源配置，含秘密，禁止提交实际值 |
| `KEYSTONE_FILE_DEFAULT_STORAGE` | `local` | 后端、部署；必须指向已配置的源 |
| `KEYSTONE_FILE_COORDINATION_ROOT` | 本机无默认，Compose 为 `/data/files` | 后端；独立的共享本地协调目录；配置任何存储源时必填 |
| `KEYSTONE_FILE_MAX_BYTES` | 16777216（16 MiB），最大 64 MiB | 后端、部署；超过 16 MiB 需同步调整 Nginx 并重建 admin 镜像 |
| `KEYSTONE_HOST` | `127.0.0.1` | 仅本机后端；Compose 固定内部监听地址 |
| `KEYSTONE_BACKEND_IMAGE` | `keystone-backend:local` | 仅部署镜像选择，用于发布或恢复，不注入应用 |
| `KEYSTONE_ADMIN_IMAGE` | `keystone-admin:local` | 仅部署镜像选择 |
| `KEYSTONE_POSTGRES_IMAGE` | `postgres:17.11-alpine` | 仅部署镜像选择 |

## 3. 配置变更

JSON 用单引号包裹，保留内部双引号。Compose 部署文件的单引号保留 `$` 字面值；后端 dotenv 仍会展开 `${...}`，包含这类内容的秘密应通过进程环境注入。模型推理和 Management Key 使用不同引用名，设置见[OpenRouter 接入](../README.md#openrouter-live-接入)。

数据库 URL 使用 `postgresql+psycopg://…`，特殊字符需 URL 编码。本机连接宿主 `127.0.0.1:55432`，容器连接 `postgres:5432`；修改密码时同步更新两份连接串和 `POSTGRES_PASSWORD`。后者仅用于空卷初始化，已有数据库需另行改密。

修改后台端口或公开域名时同步修改 `KEYSTONE_BROWSER_ORIGINS`。部署文件变更后通过 `up -d` 重建受影响容器，`restart` 不更新环境；未填写的可选参数使用原默认值。现有环境的发布与备份恢复按[部署手册](deployment.md)执行。

## 4. 同时配置本地与 S3

后端 `.env` 例子（仅示例凭据；真实值勿提交）：

```dotenv
KEYSTONE_FILE_COORDINATION_ROOT=../.data/files
KEYSTONE_FILE_DEFAULT_STORAGE=local
KEYSTONE_FILE_STORAGES='{"local":{"driver":"filesystem","root":"../.data/files"},"cloud":{"driver":"s3","endpoint":"https://s3.example.com","bucket":"keystone-backups","region":"us-east-1","prefix":"documents","access_key":"example-access","secret_key":"example-secret"}}'
```

Compose 使用相同 JSON，但 local.root 必须填写容器内已挂载的 `/data/files`，不能填写宿主路径。三个参数传入 API、Worker 及一次性 CLI；admin 不接收配置或凭据。其他本地目录须显式增加相应持久卷挂载。初始化执行 `keystone init-storage`，创建/校验协调目录及各 filesystem 源；S3 bucket 须预先创建。

S3 必须显式填写 endpoint、bucket、region（默认 us-east-1）、access_key、secret_key；prefix 可选，不能含路径穿越。使用 path-style 请求，不读取主机默认 AWS 凭据或 EC2 metadata。需要对应前缀的对象读写删除权限及就绪探针的 ListBucket 权限；不需要应用创建或删除 bucket。filesystem 和 S3 配置都必须显式提供 FILE_COORDINATION_ROOT，协调目录可以与某个本地内容源共用同一路径，但两者分别配置，不互相推导。

POST /api/v1/files 可传 storage_key=cloud；省略使用默认源，新 File 保存源键及位置指纹。已有文件的源键不随默认配置变化，现有幂等请求省略源时继续定位原源。同一请求键改源报冲突；业务创建本地/云端两份文件应使用不同请求键，并维护两份引用及备份关系。详情仅返回源键。

单本地源也使用 FILE_STORAGES 显式声明 driver 与 root。空映射表示文件存储未配置，协调目录不会自动变成内容源。多源字段直接包含在初始文件结构中，File 的 storage_key 必须由上传服务写入，数据库没有隐式 local 默认值；已有文件的源键及原存储身份须保留，删除源配置会使该源历史文件不可读、不可清理，不会回退到其他源。更换 bucket/endpoint/prefix/region 属于目标变化；使用原源身份的文件会拒绝访问，应使用新源键及业务迁移。密钥轮换不改变存储身份。各进程仍须共享同一宿主协调卷，不提供多宿主锁。
