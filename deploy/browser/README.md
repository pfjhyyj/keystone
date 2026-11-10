# 浏览器运行时镜像

`runtime/` 是 Keystone Browser 模块使用的正式 Chromium 运行镜像，不是示例专用镜像。OpenSandbox 创建沙箱时使用该镜像，镜像提供 Chromium、noVNC、受控停止、空间检查和备份/恢复接口。

启用浏览器模块时，`browser_runtime_url`、`browser_runtime_key` 和 `browser_runtime_image` 都必须配置。需要浏览器空间备份时再设置 `KEYSTONE_BROWSER_BACKUP_KEY`，并要求默认 Files 源是本地 `filesystem`。备份密钥由部署方独立保存，不从镜像或空间卷推导。

## 构建

从仓库根目录执行：

```bash
docker build -t keystone-browser-chromium:<version> deploy/browser/runtime
```

开发环境可使用 `keystone-browser-chromium:local`。发布环境应使用不可变版本标签或镜像摘要，并先把镜像分发到 OpenSandbox 所用的 Docker 宿主。OpenSandbox 只引用镜像，不由 Keystone API 构建或拉取。

## 升级

1. 在工作台停止现有浏览器运行，确认空间备份已成功。
2. 构建并发布新版 `keystone-browser-chromium:<version>`，在运行 OpenSandbox 的宿主上预先拉取。
3. 更新 `KEYSTONE_BROWSER_RUNTIME_IMAGE`，重启 API 使新配置生效。
4. 打开原浏览器空间，分别验证标签页恢复、登录复用和画面连接。

不要在不停止旧运行的情况下直接删除旧镜像。镜像升级可能触发站点重新验证，验收边界见[浏览器领域](../../docs/domains/browser.md#53-运行复用与环境变更)。
