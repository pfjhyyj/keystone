# 服务打包与发布

日常开发使用本机 API、Worker 和 Vite，启动命令见 [README](../README.md#本机启动linuxmacos)。`deploy/compose.yaml` 仅用于 PostgreSQL 等第三方依赖，不启动应用、执行迁移或配置模型。

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

API 与 Worker 使用同一份代码、锁文件和镜像，分别启动进程。后台镜像包含编译后的 Vue 静态文件及 `/api` 反向代理。每个工程的 `.dockerignore` 仅允许构建所需源码和配置进入上下文，不读取或打包实际 `keystone.toml`、`.env`；构建不执行初始化、不创建模型配置。

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

在应用的实际运行环境中手动执行一次 `keystone init`，然后分别运行 API 与 Worker。初始化仅负责业务迁移、Flow 迁移、文件存储与初始管理员；重复执行保留已有数据、密码和卷标识。API/Worker 自身启动只检查依赖，不迁移数据库。

模型凭据、渠道和模型由操作者按需在控制台手动配置，步骤见 [README](../README.md#模型配置手动)。未配置模型时平台仍可启动；启动和初始化不发起真实模型请求。

`/api/v1/health` 检查 API 存活，`/api/v1/ready` 检查数据库、业务版本及存储；Worker 使用独立日志或[健康探针](development.md#worker-健康检查排障)。

## 3. 更新与恢复

发布前停止写入和全部旧 API/Worker，联合备份数据库、文件及当前发布制品，再用新版本执行 `keystone init` 并分别启动应用进程。首版保持一个 Worker，同身份的新旧 Worker 不并行运行；未完成 Flow 先在原执行版本下完成或取消。不兼容升级失败时，不能只退代码而保留新 schema。

完整备份边界见[备份与恢复](backup-restore.md)。Dockerfile 和测试容器验证使用独立的 `tests/deploy/compose.yaml`，不改变本机开发启动方式。
