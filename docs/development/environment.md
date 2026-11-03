# 开发与测试

日常运行顺序见 README 的 [Quickstart](../../README.md#quickstart)：连接已有 PostgreSQL 或按需用 Compose 启动数据库，再分别启动 API、Worker 和前端。数据库的默认值与 `backend/keystone.toml.example` 一致；平台初始化与服务启动均不要求模型凭据，渠道和模型由操作者之后手动配置。

## 开发配置

- `backend/keystone.toml`：本机 API、Worker 和 CLI 共用。默认使用本机 55432 端口与 `.data/files` 目录。
- `frontend/console/.env`：仅在更改 API 地址时需要，可设置 `KEYSTONE_API_PROXY`；默认代理 `http://127.0.0.1:8000`。
- `deploy/.env`：可选，仅用于覆盖 PostgreSQL 密码、宿主端口或镜像。例如复制示例后使用 `docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait`；改密码或端口时同步修改后端数据库 URL。

`keystone init` 一次完成完整业务结构、Flow 执行结构、存储和管理员初始化，无需单独执行迁移命令。`KEYSTONE_INITIAL_ADMIN_PASSWORD` 仅供非交互初始化使用，本机默认交互输入；已有用户时不重置密码。模型表初始为空，`init` 不创建渠道、模型或调用记录。渠道 API Key 在控制台手动录入并加密保存，保存立即生效，无需模型秘密环境变量或重启。

## 前端目录约定

控制台以管理总览（`/console/`）为入口，完整服务与 Worker 状态位于 `/console/platform`。模型接入和 Flow 的活动页签保存在查询参数 `tab` 中；用量页保留已应用的筛选、分页和钻取条件，表单草稿不写入浏览器存储。新建或编辑模型使用独立编辑区，普通短表单使用对话框，诊断详情使用抽屉。共享视觉约定见 [DESIGN.md](../../DESIGN.md)。

表格在工作区内使用原生横向滚动，操作列在窄屏保留可访问性，不改变后端固定 50 条的分页语义。低频和危险行操作集中到 `RowMore`，危险操作另行确认。`AppDialog` 的 `protectChanges` 可保护原生输入，使用组件选择器的复杂表单还应传入 `dirty`；`useUnsavedChanges` 保护路由离开和页面关闭，身份或权限变更时仍允许清理旧界面。

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
└── __tests__/
```

其他页面按 `views/<页面>/<页面名>View.vue` 组织，私有组件放在该页面的 `components/`，没有私有组件就不建空目录。跨页面共用的组件才放到顶层 `components/`；例如 `components/models/` 中的模型选择器和费用展示同时服务模型页面与 Flow。页面不依赖其他页面的私有组件。Flow 的表单映射就近放在 `views/flows/forms.ts`，当前为空；需要专用业务表单时再按页面增加组件，未注册的定义继续使用通用 JSON 表单。

路由保留[Vue Router 懒加载](https://router.vuejs.org/guide/advanced/lazy-loading.html)，组件使用含义明确的 PascalCase 文件名。目录是本项目按页面归属制定的约定；参考 [Vue 风格指南](https://vuejs.org/style-guide/rules-strongly-recommended.html)，避免继续嵌套通用 `index.vue`、`Item.vue` 等难以区分的文件。公共组合逻辑遵循 [Vue Composables](https://vuejs.org/guide/reusability/composables.html) 的使用方式。

### 通用模型调用测试

当前「模型接入 → 调用测试」支持文本、图片生成、编辑和理解，复用现有文件上传与预览，不创建 Flow。消息和文本输出离页清空，文件按 Files 规则保存。接口、权限和停止接收语义见[实现参考](../reference/model-access.md#接口与控制台)；该通用测试入口尚未按业务入口目标迁移。

### 文件传输与会话

`api/client.ts` 管理请求的会话版本、取消信号和错误观察，保留 JSON 请求与模型 SSE/JSON 响应。`request` 的第四个参数仍是是否观察身份错误，第五个参数可传调用方的 `AbortSignal`。退出、改密、身份/权限/CSRF 变化时中止未完成的请求、模型响应流、文件校验和上传；已完成请求会解除注册。错误响应使当前会话失效时，调用方仍收到原始 HTTP 错误；旧会话的迟到结果统一拒绝为 `identity_changed`，不自动重试写入或模型调用。

登录、退出、改密由 `stores/session.ts` 协调，`api/auth.ts` 保留认证失败的原始错误。刷新按会话版本合并；登录/退出期间的焦点刷新等待当前操作，旧刷新结束不能清除新刷新的状态。控制台监听窗口焦点和 `keystone:session` 跨标签页通知，重新验证 Cookie 身份；通知只写随机标记，不保存凭据或用户资料。焦点校验期间保留当前页面；身份、权限和 CSRF 未变化时不重建组件，不清空表单或中止模型响应流。实际身份变化仍按会话版本清理页面与在途请求。

`api/files.ts` 负责文件传输。上传在 Worker 中以最多 4 MiB 的切片增量校验 SHA-256，再用 XHR 发送原生 File，提供校验、传输和等待服务器确认的进度以及取消信号；服务器继续管理上传期限和容量。文件库保留原文件和请求键供人工重试；取消或断网可能发生在服务端已受理之后，不能把它当作回滚。页面卸载或身份变化会停止校验与传输；这里没有持久业务队列。

下载先请求一个字节验证当前权限（空文件改用 HEAD），立即关闭预检内容流，再交给浏览器原生下载，不在 JavaScript 中聚合整份文件为 Blob。原生 GET 仍由后端重新授权；交给浏览器后由浏览器管理下载进度与取消。

流程目录与运行记录表格使用原生滚动及自动表格布局；窗口在桌面和窄屏间切换时，不使用自定义滚动条反复调整表格高度。浏览器测试保留窗口错误检查，覆盖这一视口切换过程。

## 开发检查

编写业务 Flow 时参照[业务 Flow 开发约定与示例](flow.md)，其中包含输出验收、
预算、工具摘要和按需读回的具体实现及接入方法。

模块结构遵循 [MVC + Service 规范](business.md#模块分层与代码规范)。新增实现归入明确职责目录，模块根目录仅保留包说明与权限/审计声明；Flow 定义使用 schemas/definitions.py。结构及相对导入检查包含在单元测试中，脚手架也执行同一规则，具体覆盖见[后端测试说明](../../backend/tests/README.md)。

完成 README 的依赖安装后，从仓库根目录执行：

```bash
(cd frontend/console && pnpm exec playwright install chromium)
# 独立测试实例预建入口库；每个用例继续创建随机隔离库。
docker compose -f tests/postgres/compose.yaml up -d --wait
export KEYSTONE_TEST_DATABASE_URL='postgresql+psycopg://keystone_test:isolated-test-only@127.0.0.1:55433/keystone_test'
backend/.venv/bin/python scripts/check.py
```

测试按 `unit/`、`integration/`、`support/` 分层：

```bash
cd backend
.venv/bin/python -m pytest tests/unit                              # 不连接数据库
KEYSTONE_TEST_DATABASE_URL='postgresql+psycopg://keystone_test:isolated-test-only@127.0.0.1:55433/keystone_test' \
  .venv/bin/python -m pytest tests/integration --require-postgres
```

只运行非数据库检查时使用 `scripts/check.py --unit`。需要验证 Dockerfile、完整测试栈及备份恢复时使用 `scripts/check.py --deploy`；完整容器栈定义在 `tests/deploy/compose.yaml`，只供隔离测试，不参与默认开发启动。数据库不可达时，`scripts/check.py` 全量和 `pytest --require-postgres` 会在启动阶段直接报错；默认 `pytest` 会统一跳过 integration，仅运行 unit。`scripts/check.py` 默认最多用 8 个 `pytest-xdist` worker 并行跑后端测试，可用 `KEYSTONE_TEST_WORKERS=1` 改为串行。测试目录和夹具约定见[后端测试](../../backend/tests/README.md)。

按组件验证使用 `scripts/check.py --scope frontend|backend|browser`。前端范围仅执行格式、单元测试、类型和构建，不依赖 PostgreSQL 或 uv；后端范围执行 Python 格式、单元／集成测试和打包；浏览器范围单独启动隔离 API、Worker 和 Chromium，仍必须连接专用测试库。`--unit` 仅接受 full／backend 范围，`--deploy` 仅接受完整范围。浏览器排查可直接运行 `backend/.venv/bin/python frontend/console/e2e/run.py admin.spec.ts`，不重复后端全量测试。浏览器测试只在失败时截图，成功路径的布局和协议以断言验证。

本仓库不配置 GitHub Actions CI。提交前按改动范围手动运行上述本地检查；每次检查独立记录结果与耗时，后端另输出最慢的 20 项。

Worker 状态与排障见[运维说明](../operations/deployment.md#worker-健康检查排障)，媒体专项验收见[测试说明](../../backend/tests/README.md#本地媒体专项验收)。
