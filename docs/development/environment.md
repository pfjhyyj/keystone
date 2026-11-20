# 开发与测试

日常运行顺序见 README 的 [Quickstart](../../README.md#quickstart)：连接已有 PostgreSQL 或按需用 Compose 启动数据库，再分别启动 API、Worker 和前端。数据库的默认值与 `backend/keystone.toml.example` 一致；平台初始化与服务启动均不要求模型凭据，渠道和模型由操作者之后手动配置。

## 开发配置

本机 API、Worker 和 CLI 共用 `backend/keystone.toml`，前端与依赖 Compose 的可选覆盖文件及读取规则见[配置说明](../operations/configuration.md)。`keystone init` 一次完成迁移、存储和管理员初始化，不创建模型配置，命令参数见[运维命令](../operations/deployment.md#22-运维命令)。

## 前端目录约定

控制台以管理总览（`/console/`）为入口，完整服务与 Worker 状态位于 `/console/platform`。模型接入和 Flow 的活动页签保存在查询参数 `tab` 中；用量页保留已应用的筛选、分页和钻取条件，表单草稿不写入浏览器存储。新建或编辑模型使用独立编辑区，普通短表单使用对话框，诊断详情使用抽屉。共享视觉约定见 [DESIGN.md](../../DESIGN.md)。

表格在工作区内使用原生横向滚动，操作列在窄屏保留可访问性，不改变后端固定 50 条的分页语义；`ListPager` 支持输入页码跳转，跳过末页时回到原页。低频和危险行操作集中到 `RowMore`，危险操作和放弃修改统一使用 `utils/confirm.ts` 的 `confirmAction` 确认，同一时间只开一个确认框。`AppDialog` 的 `protectChanges` 可保护原生输入，使用组件选择器的复杂表单还应传入 `dirty`；`useUnsavedChanges` 保护路由离开和页面关闭，身份或权限变更时仍允许清理旧界面。

工程根目录保留 Dockerfile 和 Vite、TypeScript 等工具配置；镜像运行配置放在 `frontend/console/docker/`（`nginx.conf`），本机开发使用 Vite，无需启动 Nginx。控制台源码按页面归属组织：

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
└── __tests__/
```

其他页面按 `views/<页面>/<页面名>View.vue` 组织，私有组件放在该页面的 `components/`，没有私有组件就不建空目录。跨页面共用的组件才放到顶层 `components/`；例如 `components/models/` 中的费用展示服务用量页面与 Flow。页面不依赖其他页面的私有组件。业务表单及提交接口放在对应业务模块，平台 Flow 页只展示运行。

路由保留[Vue Router 懒加载](https://router.vuejs.org/guide/advanced/lazy-loading.html)，组件使用含义明确的 PascalCase 文件名。目录是本项目按页面归属制定的约定；参考 [Vue 风格指南](https://vuejs.org/style-guide/rules-strongly-recommended.html)，避免继续嵌套通用 `index.vue`、`Item.vue` 等难以区分的文件。公共组合逻辑遵循 [Vue Composables](https://vuejs.org/guide/reusability/composables.html) 的使用方式。

### 模型与 Flow 页面

模型接入页面负责配置与余额，用量页面负责调用和费用，Flow 页面负责运行监控。生成与受理由具体业务页面发起，接口见[实现参考](../reference/model-access.md#接口与控制台)。

### 请求与会话

`api/client.ts` 管理请求的会话版本、取消信号和错误观察，保留 JSON 请求与模型 SSE/JSON 响应。`request` 的第四个参数仍是是否观察身份错误，第五个参数可传调用方的 `AbortSignal`。退出、改密、身份/权限/CSRF 变化时中止未完成的请求和模型响应流；已完成请求会解除注册。错误响应使当前会话失效时，调用方仍收到原始 HTTP 错误；旧会话的迟到结果统一拒绝为 `identity_changed`，不自动重试写入或模型调用。

登录、退出、改密由 `stores/session.ts` 协调，`api/auth.ts` 保留认证失败的原始错误。刷新按会话版本合并；登录/退出期间的焦点刷新等待当前操作，旧刷新结束不能清除新刷新的状态。控制台监听窗口焦点和 `keystone:session` 跨标签页通知，重新验证 Cookie 身份；通知只写随机标记，不保存凭据或用户资料。焦点校验期间保留当前页面；身份、权限和 CSRF 未变化时不重建组件，不清空表单或中止模型响应流。实际身份变化仍按会话版本清理页面与在途请求。

运行记录表格使用原生滚动及自动表格布局；窗口在桌面和窄屏间切换时，不使用自定义滚动条反复调整表格高度。浏览器测试保留窗口错误检查，覆盖这一视口切换过程。

## 开发检查

业务 Flow 参照[Flow 开发约定与示例](flow.md)，模块结构遵循 [MVC + Service 规范](business.md#模块分层与代码规范)；结构及相对导入检查包含在单元测试中，脚手架执行同一规则。

统一检查入口、组件范围、测试库准备和分层约定见[后端测试](../../backend/tests/README.md#运行)，媒体专项验收见[本地媒体专项验收](../../backend/tests/README.md#本地媒体专项验收)，Worker 状态与排障见[运维说明](../operations/deployment.md#worker-健康检查排障)。
