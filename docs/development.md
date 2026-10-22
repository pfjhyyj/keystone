# 开发与测试

日常运行顺序见 [README](../README.md#本机启动linuxmacos)：Compose 启动 PostgreSQL，本机终端分别启动 API、Worker 和前端。数据库的默认值与 `backend/keystone.toml.example` 一致；平台初始化与服务启动均不要求模型凭据，渠道和模型由操作者之后手动配置。

## 开发配置

- `backend/keystone.toml`：本机 API、Worker 和 CLI 共用。默认使用本机 55432 端口与 `.data/files` 目录。
- `frontend/console/.env`：仅在更改 API 地址时需要，可设置 `KEYSTONE_API_PROXY`；默认代理 `http://127.0.0.1:8000`。
- `deploy/.env`：可选，仅用于覆盖 PostgreSQL 密码、宿主端口或镜像。例如复制示例后使用 `docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait`；改密码或端口时同步修改后端数据库 URL。

`keystone init` 一次完成完整业务结构、Flow 执行结构、存储和管理员初始化，无需单独执行迁移命令。`KEYSTONE_INITIAL_ADMIN_PASSWORD` 仅供非交互初始化使用，本机默认交互输入；已有用户时不重置密码。模型表初始为空，`init` 不创建渠道、模型或调用记录。渠道 API Key 在控制台手动录入并加密保存，保存立即生效，无需模型秘密环境变量或重启。

## 前端目录约定

控制台源码按页面归属组织：

工程根目录保留 Dockerfile 和 Vite、TypeScript 等工具配置；镜像运行配置放在 `frontend/console/docker/`，当前包含 `nginx.conf`，本机开发使用 Vite，无需启动 Nginx。

```text
src/
├── App.vue                    # 全局 UI 配置
├── main.ts                    # 应用入口
├── router/index.ts            # 路由、导航与会话守卫
├── layouts/ConsoleLayout.vue  # 侧栏、页头与页面容器
├── api/                       # 请求层和领域接口、类型
├── stores/session.ts          # 共享身份与权限状态
├── plugins/                   # Element Plus 注册
├── styles/                    # 全局主题和布局
├── composables/               # 跨页面复用的组合逻辑
├── components/                # 共用基础组件及跨页面领域组件
├── views/
│   └── users/
│       ├── UsersView.vue
│       └── components/UserRolesDialog.vue
├── businesses/                # 具体业务表单
└── __tests__/
```

其他页面按 `views/<页面>/<页面名>View.vue` 组织，私有组件放在该页面的 `components/`，没有私有组件就不建空目录。跨页面共用的组件才放到顶层 `components/`；例如 `components/models/` 中的模型选择器和费用展示同时服务模型页面与 Flow。页面不依赖其他页面的私有组件。Flow 的表单映射就近放在 `views/flows/forms.ts`，实际业务表单仍由 `businesses/` 维护。

路由保留[Vue Router 懒加载](https://router.vuejs.org/guide/advanced/lazy-loading.html)，组件使用含义明确的 PascalCase 文件名。目录是本项目按页面归属制定的约定；参考 [Vue 风格指南](https://vuejs.org/style-guide/rules-strongly-recommended.html)，避免继续嵌套通用 `index.vue`、`Item.vue` 等难以区分的文件。公共组合逻辑遵循 [Vue Composables](https://vuejs.org/guide/reusability/composables.html) 的使用方式。

### 文件传输与会话

`api/client.ts` 管理请求的会话版本、取消信号和错误观察，保留 JSON 请求与模型 SSE/JSON 响应。`request` 的第四个参数仍是是否观察身份错误，第五个参数可传调用方的 `AbortSignal`。退出、改密、身份/权限/CSRF 变化时中止未完成的请求、模型响应流、文件校验和上传；已完成请求会解除注册。错误响应使当前会话失效时，调用方仍收到原始 HTTP 错误；旧会话的迟到结果统一拒绝为 `identity_changed`，不自动重试写入或模型调用。

登录、退出、改密由 `stores/session.ts` 协调，`api/auth.ts` 保留认证失败的原始错误。刷新按会话版本合并；登录/退出期间的焦点刷新等待当前操作，旧刷新结束不能清除新刷新的状态。控制台监听窗口焦点和 `keystone:session` 跨标签页通知，重新验证 Cookie 身份；通知只写随机标记，不保存凭据或用户资料。

## 开发检查

编写业务 Flow 时参照[业务 Flow 开发约定与示例](flow-development.md)，其中包含输出验收、
预算、工具摘要和按需读回的具体实现及接入方法。

完成 README 的依赖安装后，从仓库根目录执行：

```bash
(cd frontend/console && pnpm exec playwright install chromium)
# 专用入口库只需建立一次，不能指向开发或业务数据库。
docker compose -f deploy/compose.yaml exec postgres createdb -U keystone keystone_test
export KEYSTONE_TEST_DATABASE_URL='postgresql+psycopg://keystone:keystone_local_only@127.0.0.1:55432/keystone_test'
backend/.venv/bin/python scripts/check.py
```

只运行非数据库检查时使用 `scripts/check.py --unit`。需要验证 Dockerfile、完整测试栈及备份恢复时使用 `scripts/check.py --deploy`；完整容器栈定义在 `tests/deploy/compose.yaml`，只供隔离测试，不参与默认开发启动。

## Worker 健康检查（排障）

Worker 默认每 5 秒向现有 PostgreSQL 写入一次心跳，无需环境变量、文件探针或额外服务。启动中、依赖失败及维护未就绪时仍会上报；就绪需要依赖检查、文件维护循环及已启用的 Flow 执行器都正常。检查结果超过 30 秒没有更新会撤销就绪，心跳本身仍可继续，避免“进程在线”被误认为“任务处理正常”。

控制台“平台状态”每 5 秒刷新 Worker 就绪状态和最近心跳时间。API 的 `GET /api/v1/worker/status` 返回 `ready`、`unready`、`offline` 或 `unknown`，以及 `last_heartbeat_at`。正常退出保留最后心跳并标记离线；异常退出超过 30 秒判为离线。数据库不可访问、迁移未完成或查询失败时返回无法确认，不猜测离线。时间戳与过期判断统一使用数据库时钟。

API 的 `/api/v1/ready` 继续只检查自身数据库、迁移版本和存储；Worker 状态不会影响该接口或登录。API/Worker 使用同一个数据库，API 查询所有实例中仍有新鲜心跳的 Worker；旧实例停止上报或退出不会覆盖新实例。该机制只提供运行观测，不替代 Flow 的单 Worker 执行约束、权限检查或工作流恢复机制。

需要命令行探针时，从 `backend/` 执行：

```bash
uv run --locked keystone-worker --check
```

探针只查询当前主机的最近心跳，不启动执行器、创建记录或刷新心跳；就绪时退出 0，其余状态退出 1。记录按进程实例区分，正常退出保留最近时间，Worker 上报时清理超过 24 小时的旧记录。心跳表是运行观测数据，不作为业务或任务完成依据；从备份恢复的旧记录也必须通过新鲜度检查。


## 本地媒体专项验收

安装可执行 FFmpeg、前端依赖及 Playwright 浏览器后，在专用 keystone_test 数据库上执行：

```bash
backend/.venv/bin/python scripts/accept_local_media.py
# Windows 可选系统 Edge：backend/.venv/Scripts/python.exe scripts/accept_local_media.py --browser-channel msedge
```

脚本建立并清理随机测试库，运行独立 HTTP API，验证 16/256 MiB 流式上传下载、SHA-256、HEAD/Range、API 进程峰值内存，以及浏览器 H.264 播放和跳转。视频使用合法 free box 填充到指定大小，区分传输规模和媒体解码；FFmpeg 仅是验收工具，不是平台运行依赖。结果保存在 `.artifacts/local-media/`，不接触业务库、云端或模型。NAS 性能与客户原片另在部署环境验证。
