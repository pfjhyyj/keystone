# 备份与恢复

这是保存重要数据或版本升级时使用的可选操作手册，不属于安装、初始化或日常启动步骤。只有可丢弃测试数据的开发环境可以跳过；开始保存真实业务数据后，应按实际存储建立数据库与文件的联合备份。

若启用[历史清理](deployment.md#历史保留与清理)，应在保留期限到来前完成所需归档与备份；清理后不能从当前数据库补回已删除的凭据、审计、消息投递记录或维护检查点。恢复旧备份时先复核保留配置，再启动自动维护。

命令以默认开发拓扑为例：PostgreSQL 由 `deploy/compose.yaml` 运行，API/Worker 在本机运行，文件位于仓库 `.data/files`。非 Compose 部署可直接对实际 PostgreSQL 使用 `pg_dump` / `pg_restore`。

## 1. 联合备份

先停止所有节点的 API、Worker 和 CLI 写入进程，保持 PostgreSQL 在线。完整数据库（含 Alembic、部署绑定、Worker 登记和 DBOS 检查点）与所有正式文件必须来自同一停写窗口；已脱离进程的远端请求仍可能落地，备份前核对未完成操作。

- **含 `filesystem`**：保存正式内容、本地锁文件及正式文件目录和 `file_coordination_root` 的原 `.keystone-storage` 标识。
- **仅 S3**：不靠节点目录、磁盘预留或锁恢复，可用 `keystone init-storage` 重建节点目录。
- **含 S3 源**：按[第 3 节](#3-多存储源)备份。

保留对应配置和制品。**实际生效的原凭据加密密钥**（若已配置）及含密钥的私有 TOML 单独存入受访问控制的秘密管理系统或加密备份；仅有数据库或文件归档权限的操作者不得取得密钥。指纹不能解密或重建密钥。`.transfers` 中的临时副本不是恢复依据。

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
tar --exclude='.env' --exclude='keystone.toml' \
    -cpf "$BACKUP/source.tar" backend/src backend/migrations \
    backend/pyproject.toml backend/uv.lock backend/Dockerfile \
    backend/.dockerignore frontend/console/src \
    frontend/console/package.json frontend/console/pnpm-lock.yaml \
    frontend/console/index.html frontend/console/tsconfig.json \
    frontend/console/vite.config.ts frontend/console/Dockerfile \
    frontend/console/.dockerignore frontend/console/docker/nginx.conf deploy
(cd "$BACKUP" && shasum -a 256 database.dump files.tar source.tar > SHA256SUMS)
```

使用自定义 Compose 项目或环境文件时，上述 Compose 命令使用相同参数。另行保存私有部署配置和实际运行环境中的覆盖变量（含自定义路径下的配置）、前端自定义环境文件、反向代理配置和发布镜像。源码包保留当前文件及锁文件，不能只记 Git 提交而遗漏未提交的改动；自定义秘密文件不得放进源码包或文件归档。数据备份含凭据密文和认证记录，也须限制访问并加密转存；本机 `.artifacts` 仅作中转。

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

export KEYSTONE_POSTGRES_PORT=55433
docker compose -p keystone-recovery -f deploy/compose.yaml up -d --wait
docker compose -p keystone-recovery -f deploy/compose.yaml exec -T postgres \
    pg_restore -U keystone -d keystone --exit-on-error --single-transaction < "$BACKUP/database.dump"
```

从受保护备份恢复私有配置、环境变量和原凭据加密密钥（若有）。将恢复配置指向数据库端口 55433，核对文件路径与浏览器 Origin。按原版本和锁文件启动 API、Worker 和前端（见 [Quickstart](../../README.md#quickstart)），先验证再升级。

恢复不改变拓扑。含 `filesystem` 时使用绑定中的宿主身份和恢复的实际本地目录，不能挂空卷；仅 S3 时使用原源位置与密钥，按[跨宿主部署](deployment.md#24-s3-跨宿主部署)准备节点。

核对原文件的字节与访问权限、认证和字典、模型原调用记录、Flow 原 ID 与 DBOS 步骤。有已保存的模型或消息凭据时，在受控恢复环境验证其仍可解密，但不回显凭据、不重发历史请求。会话/PAT 也回到备份时点，上游模型费用或副作用不会随数据库回滚，未知调用不能自动重发。

## 3. 多存储源

混合或仅 S3 部署须覆盖每个 S3 源，不能只备份默认源。保存源键、endpoint/bucket/region/prefix、对象和版本，恢复时使用原位置身份。S3 删除标记和未完成上传可能仍有维护义务；保留相关元数据与桶策略。定位规则见[存储配置](configuration.md#4-文件存储与部署拓扑)。

## 4. 浏览器空间卷

上述联合备份不会直接导出 Chromium 的 Docker 空间卷。浏览器备份目前要求默认文件源为本地 `filesystem`，仅 S3 部署不支持。符合条件时配置 `KEYSTONE_BROWSER_BACKUP_KEY`，由浏览器通过 Keystone Files 保存最近成功的加密副本。覆盖该空间的联合备份应同时保留 Files 中的该副本、对应数据库引用，以及配置系统或独立密钥库中保存的 `KEYSTONE_BROWSER_BACKUP_KEY`。该密钥不能与备份文件交给同一无权操作者。
