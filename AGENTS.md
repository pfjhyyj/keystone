# Keystone 协作规范

本文件适用于本仓库，用户的明确要求优先。

## 查阅入口

| 范围 | 文档 |
|---|---|
| 进程、领域与依赖方向 | [整体架构](docs/architecture.md) |
| Task、队列、计划与 Worker | [任务执行](docs/execution-design.md) |
| FlowDefinition/Package、角色、快照、文件路径、Context、预算与恢复 | [Flow](docs/domains/flow.md)、[接入示例](docs/flow-development.md) |
| 模型操作、费用与原任务恢复 | [模型接入](docs/domains/model-access.md) |
| 新业务、权限、审计、迁移 | [模块扩展](docs/extending.md) |
| 文件及其他领域 | [文档索引](docs/README.md) |
| 开发、验证、发布与恢复 | [开发](docs/development.md)、[测试](backend/tests/README.md)、[部署](docs/deployment.md)、[备份恢复](docs/backup-restore.md) |

## 实现约束

- Keystone 面向单企业内部多个业务模块，采用模块化单体。业务直接模块化接入；API、Worker、CLI 共用工程、镜像和发布制品，领域 Service 用 Python 调用协作，不建立内部 HTTP 服务。
- 优先简单开发和部署，业务页面可直接集成到控制台。不以业务独立发布、独立 Worker、多租户、跨宿主或滚动发布作为当前平台目标；按真实需求扩展现有 Service/Schema 和显式装配，不预建分布式协议或插件体系。适用性按架构文档的真实业务验证目标评估。
- 先明确资源、操作、事实归属和权限；优先普通函数、Service 对象和显式依赖注入，不引入通用 CRUD/Repository/BaseService、动态扫描、在线插件或第二执行引擎。
- composition/bootstrap 显式装配声明和依赖；平台不导入业务/examples，core 不依赖领域，跨模块只使用公开 Service/Schema，不导入其他模块 ORM/Adapter。
- 模块根目录只保留 __init__.py 与轻量 catalog.py；一级目录按需使用 controllers、schemas、models、services、adapters、tasks、workflows，不预建空目录。每个持久实体一个文件，显式导出。
- Controller 只绑定 HTTP/身份/传输；Service 负责授权、事务、审计和查询，不依赖 Request/Response、DBOS、Worker、FlowContext 或 SDK 协议；Schema 不查库、不加载 ORM/Service/SDK/执行器；Adapter 不拥有业务授权、领域事务，不反向依赖 Service/ORM。具体依赖和技术例外见[架构](docs/architecture.md#54-模块分层与代码规范)。
- catalog.py 仅声明权限/审计，不加载执行器或注册 Flow。Flow 定义在轻量 schemas，业务流程在 workflows；Task/Workflow 调用 Service，不直接读写领域表。
- 迁移平铺在 backend/migrations/versions，使用全局连续 NNNN_<module>_<change>.py 和同一线性链。历史 revision 不重写，Worker 相关用 worker 前缀；详细规则和当前 head 见[迁移说明](backend/migrations/README.md)。

## 行为保证

- 分别检查功能、对象、文件内容和调用/费用范围；PAT 是当前用户权限与原令牌范围的交集。前端控制不替代后端授权，不统一加入管理员绕过、软删除或审计字段。
- 业务变更与成功审计同事务，失败/拒绝回滚后记录。跨域原子操作由上层协调同一身份事务，被调 Service 不另开事务、不自行提交；外部 I/O 不持长业务事务。
- Flow 每次新操作复查原用户/PAT、Run、契约和固定目标，撤权/取消阻止新副作用；已发生的调用、费用、文件和真实终态按事实受限收尾。
- DBOS 保留原生确定性与 Step 边界；检查点不能保证第三方 exactly-once。模型发送前原子登记，未知结果不换 Run/调用 ID 重发。业务幂等使用稳定标识和唯一约束。
- 待办先持久提交，再由可恢复扫描投递；不能以提交后的临时协程保证不丢失。Task 不强制创建 FlowRun，Step/Agent 也不自动注册 Task。
- 并发、批次、等待、重试和输出有界。反复取消仍须等待原生 I/O、进程及文件清理，再释放句柄/许可；失锁后不得换连接继续提交。
- 代码、目录、声明和资源都影响执行版本。新制品不能冒充旧检查点兼容，保留原制品和身份收尾，不删除 DBOS/未知调用绕过恢复或容量规则。
- 输入、结果、审计、错误和检查点使用显式白名单，不存凭据、运行句柄、供应商原始正文或未控制异常；费用与预算分别表达。
- 人工决定由业务 Service 保存版本、决定和引用，自动阶段分为独立 Flow；不建设通用 HumanTask、人工等待或唤醒 API。

## 验证与维护

- 按行为验证授权、事务、幂等、取消和失败窗口；真实 PostgreSQL/DBOS/跨进程语义使用集成测试，不用 mock 替代关键保证。
- 测试按 unit/integration 分层，共享夹具放 support；unit 不导入 integration。竞争优先使用事件/门闩，不依赖短 sleep；每个场景保留独立事实断言，复用重复装配和数据构造。
- 集成仅使用专用 keystone_test 入口及随机隔离库。统一检查为 backend/.venv/bin/python scripts/check.py，--unit 是部分检查；Windows 使用 .venv/Scripts/python.exe。Ruff 使用 backend/pyproject.toml。
- 完成相关检查后仅在新改动/失败/未决问题需要时扩大或重跑；纯文档改动检查内容、链接和差异。
- 架构维护边界、领域维护当前契约、开发文档维护示例、操作手册维护命令；同一规则一处维护。不要保留实施计划、阶段记录、旧文件映射或测试过程日志。
- 在已授权范围内完成必要实现、review、修复和文档同步，常规选择不反复确认。只有用户要求时提交代码或部署。
