# 备份与恢复

这是保存重要数据或版本升级时使用的可选操作手册，不属于安装、初始化或日常启动步骤。只有可丢弃测试数据的开发环境可以跳过；开始保存真实业务数据后，应按实际存储建立数据库与文件的联合备份。

本页以默认开发拓扑为例：PostgreSQL 由 `deploy/compose.yaml` 运行，API/Worker 在本机运行，文件位于仓库 `.data/files`。多存储源或其他部署环境按同一停写原则处理实际数据库和存储。应用打包见[打包与发布](deployment.md)。

## 1. 联合备份

先在各终端停止 API、Worker 和其他写入进程，保持 PostgreSQL 在线。数据库与文件必须来自同一个停写窗口；完整数据库包含业务、认证、审计、Alembic 和 DBOS 检查点，文件备份包含 `.keystone-storage`、永久锁文件和已自动生成的 `.model-credentials.key`。该密钥用于解密数据库中的渠道 API Key，数据库与协调目录必须一起恢复；缺少密钥时不会生成替代密钥。

以下为 Linux/macOS 的 Bash 命令，从仓库根目录执行；`shasum` 用于校验：

```bash
set -euo pipefail
umask 077
BACKUP="$PWD/.artifacts/backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$PWD/.artifacts/backups"
mkdir "$BACKUP"
docker compose -f deploy/compose.yaml exec -T postgres \
    pg_dump -U keystone -d keystone -Fc > "$BACKUP/database.dump"
tar -C .data/files -cpf "$BACKUP/files.tar" .
cp backend/.env "$BACKUP/backend.env"
tar -cpf "$BACKUP/source.tar" backend/src backend/migrations \
    backend/pyproject.toml backend/uv.lock backend/Dockerfile \
    backend/.dockerignore frontend/console/src \
    frontend/console/package.json frontend/console/pnpm-lock.yaml \
    frontend/console/index.html frontend/console/tsconfig.json \
    frontend/console/vite.config.ts frontend/console/Dockerfile \
    frontend/console/.dockerignore frontend/console/docker/nginx.conf deploy
(cd "$BACKUP" && shasum -a 256 database.dump files.tar source.tar > SHA256SUMS)
```

使用自定义 Compose 项目或环境文件时，上述 Compose 命令使用相同参数。另行保存实际运行环境中的覆盖变量、前端自定义环境文件、反向代理配置和发布镜像；源码包保留当前文件及锁文件，不能只记 Git 提交而遗漏未提交的改动。备份含数据库凭据、密钥和认证记录，限制访问并加密转存；本机 `.artifacts` 仅作中转。

## 2. 恢复到新环境

原环境保持停写，恢复到新的工作目录、数据库和文件目录。以下例子使用新 Compose 项目和宿主端口 55433，保留原环境的数据与卷。若原环境改过数据库密码，在启动恢复项目之前先设置对应的 `POSTGRES_PASSWORD`。

```bash
set -euo pipefail
BACKUP=/absolute/path/to/backup
(cd "$BACKUP" && shasum -a 256 -c SHA256SUMS)
# 当前目录必须是用于恢复的新工作目录。
tar -xpf "$BACKUP/source.tar"
mkdir -p .data/files
tar -C .data/files -xpf "$BACKUP/files.tar"
cp "$BACKUP/backend.env" backend/.env
export KEYSTONE_POSTGRES_PORT=55433
docker compose -p keystone-recovery -f deploy/compose.yaml up -d --wait
docker compose -p keystone-recovery -f deploy/compose.yaml exec -T postgres \
    pg_restore -U keystone -d keystone --exit-on-error --single-transaction < "$BACKUP/database.dump"
```

启动应用前修改恢复目录 `backend/.env` 的数据库 URL，指向新数据库端口 55433，并核对文件路径、原秘密与浏览器 Origin；使用原版本源码和锁文件，在本机按 [README](../README.md#本机启动linuxmacos) 分别启动 API、Worker、前端。先验证原版本，再按发布流程升级。

核对原文件与报告的字节和权限、认证和字典、模型原调用记录、Flow 原 ID 与 DBOS 步骤。只恢复数据库而使用新的文件卷标识会使原文件读取失败；恢复时必须保留原文件标识和锁文件。会话/PAT 也回到备份时点，上游模型费用或副作用不会随数据库回滚，未知调用不能自动重发。

## 3. 多存储源

filesystem/S3 多源必须在同一停写窗口保存所有源的内容及配置身份，不能只备份默认本地目录。保存源键、endpoint/bucket/region/prefix 及对象版本，恢复时使用原位置身份。S3 删除标记和未完成上传可能仍有维护义务；保留相关元数据与桶策略。文件多源配置见[配置说明](configuration.md#4-同时配置本地与-s3)。
