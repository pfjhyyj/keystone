# 浏览器工作台示例

这个示例包含独立前端、Chromium 镜像和本机 Compose 配置。它复用 Keystone 的身份、浏览器空间权限和运行接口，可作为自建工作台的起点。[浏览器领域设计](../../docs/domains/browser.md)

在仓库根目录运行以下 PowerShell 命令。需要支持 Compose `include` 的 Docker Compose，以及 Linux Docker Engine 或 Docker Desktop 的 Linux 容器环境。宿主需要允许 Chromium 使用用户命名空间沙箱。[Chromium 容器沙箱说明](https://playwright.dev/docs/docker)

```powershell
$env:KEYSTONE_BROWSER_RUNTIME_KEY = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
$env:KEYSTONE_BROWSER_BACKUP_KEY = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
$seccomp = Invoke-RestMethod 'https://raw.githubusercontent.com/microsoft/playwright/v1.64.1/utils/docker/seccomp_profile.json'
$env:WORKBENCH_SECCOMP_JSON = $seccomp | ConvertTo-Json -Compress -Depth 100
$composeFile = 'examples/browser-workbench/compose.yaml'

docker build -t keystone-browser-chromium:local deploy/browser/runtime
docker pull opensandbox/execd:release-1.2.0
docker volume create keystone-browser-profile
docker run --rm -v keystone-browser-profile:/data/profile keystone-browser-chromium:local --init-profile
docker compose -f $composeFile run --build --rm api keystone init
docker compose -f $composeFile up -d --build
```

后续启动时沿用同一个 OpenSandbox 密钥、备份密钥和空间卷。备份密钥应由部署方保存在配置系统或独立密钥库中；丢失后无法解密已有备份。打开 [本机工作台](http://127.0.0.1:8080/workbench/)，用刚设置的管理员账号登录，从「我的空间」空状态复制用户 ID，再登记浏览器空间：

```powershell
$ownerId = '替换为用户 UUID'
docker compose -f $composeFile run --rm api keystone register-browser-workspace --owner-id $ownerId --workspace-name '工作浏览器' --volume keystone-browser-profile
```

回到工作台点「刷新」和「打开浏览器」，在画面里的 Chromium 地址栏输入目标网址。关闭工作台后可以重新连接原运行。若端口已被占用，可设置 `WORKBENCH_HTTP_PORT` 或 `KEYSTONE_POSTGRES_PORT`；如通过反向代理访问，再设置实际入口的 `KEYSTONE_BROWSER_ORIGINS`。

连接画面后，可点「粘贴文本」，把本机的文字粘贴到弹窗并点「发送到剪贴板」，再在远程 Chromium 中按 `Ctrl+V` 或右键粘贴。支持中文和多行文字；文件和图片不通过此入口传输。

正常停止会自动保存一份加密备份；已停止的浏览器空间也可以点「备份空间」。下次打开时优先使用原卷；原卷丢失时从最近成功备份恢复。备份写入 Files 的默认源，因此启用浏览器模块时 `file_default_storage` 必须是本地 `filesystem`。不要删除 Files 的存储卷和协调数据。已有环境升级时，先运行新版 `keystone init`、构建新版 Chromium 镜像；已有运行仍在旧镜像上时，先正常停止，再对已停空间手动备份。

关闭 Compose、更新 Chromium 镜像或轮换密钥前，先在工作台停止浏览器，再执行 `docker compose -f $composeFile down`。不要将空间卷挂载给其他浏览器。如果运行停在「待核对」，先点「重新检查」。对已识别但无法正常退出的沙箱，部署者应核对 OpenSandbox 实例及其 `keystone_run` / `keystone_workspace` 标签后再做清理，随后回到工作台复查。没有已知沙箱 ID 时，空列表不足以排除迟到创建，系统会继续保留空间占用。

noVNC 授权声明随静态资源提供，路径为 `/workbench/novnc/COPYRIGHT`。
