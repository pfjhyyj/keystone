# 开发与测试

日常运行顺序见 README 的 [Quickstart](../README.md#quickstart)：Compose 启动 PostgreSQL，本机终端分别启动 API、Worker 和前端。数据库的默认值与 `backend/keystone.toml.example` 一致；平台初始化与服务启动均不要求模型凭据，渠道和模型由操作者之后手动配置。

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
├── services/upload/           # 分块 SHA-256 校验与 Worker，不持有页面或队列状态
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

`api/files.ts` 负责文件传输。上传在 Worker 中以最多 4 MiB 的切片增量校验 SHA-256，再用 XHR 发送原生 File，提供校验、传输和等待服务器确认的进度以及取消信号；服务器继续管理上传期限和容量。文件库保留原文件和请求键供人工重试；取消或断网可能发生在服务端已受理之后，不能把它当作回滚。页面卸载或身份变化会停止校验与传输；这里没有持久业务队列。

下载先请求一个字节验证当前权限（空文件改用 HEAD），立即关闭预检内容流，再交给浏览器原生下载，不在 JavaScript 中聚合整份文件为 Blob。原生 GET 仍由后端重新授权；交给浏览器后由浏览器管理下载进度与取消。

流程目录与运行记录表格使用原生滚动及自动表格布局；窗口在桌面和窄屏间切换时，不使用自定义滚动条反复调整表格高度。浏览器测试保留窗口错误检查，覆盖这一视口切换过程。

## 开发检查

编写业务 Flow 时参照[业务 Flow 开发约定与示例](flow-development.md)，其中包含输出验收、
预算、工具摘要和按需读回的具体实现及接入方法。

模块结构遵循 [MVC + Service 规范](architecture.md#54-模块分层与代码规范)。新增实现归入明确职责目录，模块根目录仅保留包说明与权限/审计声明；Flow 定义使用 schemas/definitions.py。结构及相对导入检查包含在单元测试中，脚手架也执行同一规则，具体覆盖见[后端测试说明](../backend/tests/README.md)。

完成 README 的依赖安装后，从仓库根目录执行：

```bash
(cd frontend/console && pnpm exec playwright install chromium)
# 专用入口库只需建立一次，不能指向开发或业务数据库。
docker compose -f deploy/compose.yaml exec postgres createdb -U keystone keystone_test
export KEYSTONE_TEST_DATABASE_URL='postgresql+psycopg://keystone:keystone_local_only@127.0.0.1:55432/keystone_test'
backend/.venv/bin/python scripts/check.py
```

测试按 `unit/`、`integration/`、`support/` 分层：

```bash
cd backend
.venv/bin/python -m pytest tests/unit                              # 不连接数据库
KEYSTONE_TEST_DATABASE_URL='postgresql+psycopg://keystone:keystone_local_only@127.0.0.1:55432/keystone_test' \
  .venv/bin/python -m pytest tests/integration --require-postgres
```

只运行非数据库检查时使用 `scripts/check.py --unit`。需要验证 Dockerfile、完整测试栈及备份恢复时使用 `scripts/check.py --deploy`；完整容器栈定义在 `tests/deploy/compose.yaml`，只供隔离测试，不参与默认开发启动。测试目录和夹具约定见[后端测试](../backend/tests/README.md)。

## Worker 健康检查（排障）

Worker 每 5 秒向 PostgreSQL 上报心跳，启动、依赖失败和排空期间也继续报告。每个固定 worker_id 按登记所指向的当前 instance_id 展示状态，执行版本和公共配置须一致；默认运行一个实例。数据库时间判断心跳与维护检查是否超过 30 秒，旧实例迟到的退出不遮蔽新进程。

API 按未退役身份登记计算预期数量并自动发现新 Worker，无需同步身份清单或重启 API/已有 Worker。普通停止、心跳过期与历史观测清理不移除登记；永久缩容先停止目标进程，再执行 keystone retire-worker --worker-id ID。退役拒绝仍在运行或有待恢复工作的身份；恢复退役身份须先执行 reactivate-worker。同身份互斥、故障恢复与操作步骤见[部署说明](deployment.md#44-固定槽位调整与故障恢复)。

进程健康与共享维护分开：进程检查依赖、DBOS 和队列；共享维护聚合实际执行过的文件清理、Flow 协调及费用扫描结果。任一同版本实例成功扫描即可更新共享结果，空闲实例不因未亲自扫描误报。普通心跳不能刷新陈旧的任务结果，依赖检查也不能掩盖共享维护故障。供应商待出账、限流或 Token 失效保留为费用状态，不等同 Worker 故障。

GET /api/v1/worker/status 返回总体 status、last_heartbeat_at、instances、maintenance、ready_workers 和 expected_workers，控制台每 5 秒刷新并分开展示。未退役身份缺失、不同版本、过期或未就绪时，不能被另一个 ready 实例掩盖；退役身份单独展示且不计入预期数量，没有登记成员时总体为 offline。API /api/v1/ready 只检查自身数据库、schema 与存储，不受 Worker 观测影响。

从 backend/ 执行，探针与运行 Worker 使用同一身份环境；以下为 Linux/macOS 示例：

```bash
KEYSTONE_WORKER_ID=keystone-worker uv run --locked keystone-worker --check
uv run --locked keystone-worker --inspect
```

--check 只查询当前主机的指定身份，不启动执行器或刷新心跳；进程就绪退出 0，其余退出 1。共享维护异常单独告警，不能据此重启所有正常进程。--inspect 额外列出有界的 DBOS 活动队列/身份/版本/状态计数，不读取输入输出、不启动执行器；unknown 或 truncated 时不能据此认定队列已排空。

三类扫描仍使用独立串行维护队列，费用 HTTP 使用独立查询队列。排障检查 file_cleanup_failed、flow_reconcile_failed、flow_sync_deferred、cost_scan_failed、cost_reconcile_failed，以及 worker_failed 的安全错误码。worker_deployment_conflict 表示主机、版本或公共配置冲突；不要随机更换身份绕过。共享维护扫描停止时查看 DBOS 的 PENDING，故障/旧版本记录可能占住全局容量；先按原身份/版本恢复或走[停机发布核对](deployment.md#4-多-worker-部署与发布)，不删 DBOS 历史。

SIGINT/SIGTERM 触发 draining，停止领取后按 worker_shutdown_seconds 有限等待，再关闭并报告 stopped。超时仍需保留原槽位恢复路径；心跳过期不是外部请求停止证明。只清理超过 24 小时的 stopped 观测，未处理故障记录继续保留。历史无归属 Flow 调用需要明确停机确认的专项入口，不允许新 Worker 启动时全局收尾；命令及首次迁移顺序见部署文档。


## 本地媒体专项验收

安装可执行 FFmpeg、前端依赖及 Playwright 浏览器后，在专用 keystone_test 数据库上执行：

```bash
backend/.venv/bin/python scripts/accept_local_media.py
# Windows 可选系统 Edge：backend/.venv/Scripts/python.exe scripts/accept_local_media.py --browser-channel msedge
```

脚本建立并清理随机测试库，运行独立 HTTP API，验证 16/256 MiB 流式上传下载、SHA-256、HEAD/Range、API 进程峰值内存，以及浏览器 H.264 播放和跳转。视频使用合法 free box 填充到指定大小，区分传输规模和媒体解码；FFmpeg 仅是验收工具，不是平台运行依赖。结果保存在 `.artifacts/local-media/`，不接触业务库、云端或模型。NAS 性能与客户原片另在部署环境验证。
