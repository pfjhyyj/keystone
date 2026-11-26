# Keystone 协作规范

本文件适用于本仓库，用户的明确要求优先。

## 查阅入口

| 范围 | 文档 |
|---|---|
| 进程、领域与依赖方向 | [整体架构](docs/architecture.md) |
| Task、队列、计划与 Worker | [任务执行](docs/execution.md) |
| FlowDefinition/Package、角色、快照、文件路径、Context、执行上限与恢复 | [Flow](docs/domains/flow.md)、[接入示例](docs/development/flow.md) |
| 模型操作、费用与原任务恢复 | [模型接入](docs/domains/model-access.md) |
| 新业务、权限、审计、迁移 | [模块扩展](docs/development/business.md) |
| 下游仓库、代码归属、平台缺口与合并上游 | [下游交付项目](docs/development/downstream.md) |
| 文件及其他领域 | [文档索引](docs/README.md) |
| 开发、验证、发布与恢复 | [开发](docs/development/environment.md)、[测试](backend/tests/README.md)、[部署](docs/operations/deployment.md)、[备份恢复](docs/operations/backup-restore.md) |

## 模块设计评审

- 先明确核心能力、真实场景、职责、事实归属、授权和成功标准。关联主流程在同一轮成组评审，每个流程独立画图；跨模块问题联合讨论。
- 运行交互使用时序图、状态表和不变量，简单逻辑可合并；目录、装配、分层和接入选择使用规范、清单或决策表，不虚构运行流程。
- 先确认正常主线，失败、并发、重试、取消作为补充；只有会改变职责、授权、主要状态或不变量时才提前展开。
- 图只表达参与者、调用顺序、事务和外部副作用；路径结果放状态表，保留策略放正文。不为每个 SQL 画图，无持久状态不造状态机。
- 明确前置条件、允许/禁止转换、幂等与必须成立的性质，区分保证和可接受失败。逐项说明检查、锁和状态的用途、成本及更小替代；图表不等于正确性证明。
- 结论写对应领域文档，单列目标与当前实现差异；每条规则只维护一处。AGENTS 只记录方法，不加入业务方案或讨论日志。
- 设计讨论只维护文档，不自动扩为实现、迁移或部署；验证须如实报告范围。

## 实现约束

- Keystone 面向单企业内部多个业务模块，采用模块化单体：API、Worker、CLI 共用工程、镜像和发布制品，领域 Service 用 Python 调用协作，不建立内部 HTTP 服务。优先简单开发和部署，业务页面可直接集成到控制台；仅 S3 存储的部署支持跨宿主，边界见[整体架构](docs/architecture.md#14-外部依赖)。不以业务独立发布、业务专属 Worker、多租户或滚动发布作为当前平台目标，不预建其他分布式协议或插件体系。适用性按架构文档的真实业务验证目标评估。
- 先明确资源、操作、事实归属和权限；优先普通函数、Service 对象和显式依赖注入，不引入通用 CRUD/Repository/BaseService、动态扫描、在线插件或第二执行引擎。
- Keystone 上游仓库的 `businesses/` 保持为空，业务在下游仓库实现；下游不修改平台代码，平台缺口先在上游按通用契约补齐，上游文档和代码不出现下游业务名。
- composition/bootstrap 显式装配声明和依赖；平台不导入业务/examples，core 不依赖领域，跨模块只使用公开 Service/Schema，不导入其他模块 ORM/Adapter。
- 目录、分层依赖、catalog、Flow/Task 放置与技术例外按[分层规范](docs/development/business.md#模块分层与代码规范)和[模块目录](docs/development/business.md#模块目录与后台任务)；平台链与业务链的命名、登记和版本记录按[迁移说明](backend/migrations/README.md)，历史 revision 不重写。

## 行为保证

- 分别检查功能、对象、文件内容和调用/费用范围；PAT 是当前用户权限与原令牌范围的交集。前端控制不替代后端授权，不统一加入管理员绕过、软删除或审计字段。
- 输入、结果、审计、错误和检查点使用显式白名单，不存凭据、运行句柄、供应商原始正文或未控制异常；费用与预算分别表达。
- 事务与审计、外部 I/O、取消与有界性按[核心约束](docs/architecture.md#4-核心约束)；投递、Step、执行版本与多 Worker 按[任务执行](docs/execution.md)；Flow 复查、恢复与人工决定按 [Flow](docs/domains/flow.md)；模型登记与防重发按[模型接入](docs/domains/model-access.md)。

## 验证与维护

- 按行为验证授权、事务、幂等、取消和失败窗口；真实 PostgreSQL/DBOS/跨进程语义使用集成测试，不用 mock 替代关键保证。分层、夹具和断言规则见[后端测试](backend/tests/README.md)。
- 集成仅使用专用 keystone_test 入口及随机隔离库。统一检查为 backend/.venv/bin/python scripts/check.py，--unit 是部分检查；Windows 使用 .venv/Scripts/python.exe。Ruff 使用 backend/pyproject.toml。
- 完成相关检查后仅在新改动/失败/未决问题需要时扩大或重跑；纯文档改动检查内容、链接和差异。
- 架构维护边界、领域维护当前契约、开发文档维护示例、操作手册维护命令；同一规则一处维护。不要保留实施计划、阶段记录、旧文件映射或测试过程日志。
- 在已授权范围内完成必要实现、review、修复和文档同步，常规选择不反复确认。只有用户要求时提交代码或部署。
