# Keystone 设计文档

状态：核心领域、仓库结构与实施顺序已确定，2026-10-19；六领域最小实现、三批后台整理，以及单宿主 Compose 冷启动和联合备份恢复验收已完成。新增 live 基础能力和 OpenRouter 接入，已通过本机 HTTP、真实 SDK 和 DBOS Worker 验证；尚未向真实供应商发送请求。开发与接入命令见[仓库说明](../README.md)，运维步骤见[部署与恢复](deployment.md)，本批结果见[最终代码联合回归](implementation-plan.md#428-清理与-flow-最终代码联合回归)。

## 平台定位

Keystone 是从 Ether 重新整理的通用底座，采用模块化单体。各业务模块共享用户、登录和权限管理，文件、字典与模型接入提供可复用能力。先确定领域职责、模型和关键规则，再进入接口与实现设计。

仓库以 keystone/ 为根：backend/ 包含一个 Python 工程，frontend/ 容纳独立 admin 和按需新增的业务前端；API、Worker 共用后端代码和镜像，通过不同入口运行，CLI 承担一次性管理命令。业务接口、模型与 Flow 集中在各自业务模块。目录与依赖边界见 [仓库结构](architecture.md#5-仓库结构与扩展边界)。

已确定首版不做租户、Department 和 SSO；使用本地认证与 Casbin。管理员创建用户，登录名固定，用户只停用不删除，初始化命令创建首位管理员，内置管理员拥有全部已声明权限。

## 阅读顺序

开发业务扩展先读[业务权限、审计与模型接入](architecture.md#55-业务权限审计与模型接入)：业务声明、应用级目录、显式 ORM 加载、共用迁移链及 Service 授权事务。Database 必须显式绑定 Catalog；当前会话缺少目录时拒绝授权及审计写入。Flow 声明、Worker 装配和业务表扩展检查见[接入校验记录](implementation-plan.md#424-flow-接入声明与装配校验)，输出契约和输入文件错误处理见[修复记录](implementation-plan.md#425-flow-输出契约与输入文件错误)。最终输入的 64 KiB 限制见[大小检查记录](implementation-plan.md#427-flow-最终输入大小检查)。接入点已实现，尚未新增具体业务。

文件多源配置见[本地与 S3](configuration.md#4-同时配置本地与-s3)，实现和验证见[文件多存储源](implementation-plan.md#423-文件多存储源)；每个 File 仍对应一份内容，备份关系由业务维护。

Worker 运行检查见[最小健康探针](deployment.md#12-worker-最小健康检查)：本地就绪文件、只读检查命令及 Compose healthcheck，不增加数据库表；API ready 不代表 Worker 在线。

准备首次真实账户联调时，直接查看[OpenRouter 准备清单与最小验收](domains/model-access.md#79-真实账户联调准备与最小验收)。目前仅整理步骤，未配置真实密钥或发送付费请求。

先读 [整体架构](architecture.md)，了解模块边界、调用关系、API/Worker 组织和 Ether Flow 的参考范围，再按领域查看模型与规则。领域模型收敛后，按 [技术选型与实现计划](implementation-plan.md) 确定依赖基线、Ether 复用范围、实现阶段与验收条件。

新增或修改后端模块统一遵循[模块分层与代码规范](architecture.md#54-模块分层与代码规范)：按领域组织 Controller、Service、Model 和 Schema，Vue 承担 View；六领域统一 controllers/services/models/schemas 四层目录，一个实体一个文件，按用例拆分服务；AccessContext、跨领域操作和显式响应字段见该规范。本批记录见[Flow 安全错误与列表批量查询](implementation-plan.md#418-flow-安全错误与列表批量查询)。

数据库初始迁移与六个领域一一对应，使用单条 Alembic 链；字段、约束和索引写在各领域迁移内。对应表及后续增量规则见[迁移说明](../backend/migrations/README.md)，DBOS 的执行表仍独立管理。

本地开发和部署使用[三层环境配置](configuration.md)：后端、前端、部署编排分别提供 `.env.example`，说明各自的读取方式、优先级与传递范围；浏览器继续使用同源 `/api`。配置验证见[实施记录](implementation-plan.md#419-三层环境变量配置)。

实现收尾保持领域表不变：文件管理响应采用最小回执、字典审计覆盖实际写入字段、角色删除由上层用例协调；普通模型调用增加[离线收尾命令](domains/model-access.md#76-普通-api-调用离线收尾)。审计操作名统一以[审计领域](domains/audit.md#7-当前实现)和代码 ACTIONS 目录为准。

## 按领域阅读

每个领域用一份完整文档，统一包含“范围与边界、领域模型、主要流程、权限与审计、设计取舍、待确定事项”。审计字段直接列在各实体字段表中，审计语义也在各领域内重复说明，阅读时无需跳转到公共字段文档。

| 领域 | 核心对象 | 与其他领域的关系 | 当前状态 |
|---|---|---|---|
| [身份与权限](domains/identity.md) | 用户、角色、权限目录、会话、个人令牌及授权关联 | 为各领域提供身份与功能权限判断 | 六表、Cookie/PAT、Casbin、管理员初始化、账户及用户/角色页面已实现 |
| [字典](domains/dictionary.md) | Dictionary、DictionaryItem | 复用身份与审计，为业务提供选项查询、校验与历史名称解析 | 两表、管理 API、独立编辑弹窗与分页选项、业务校验与历史解析已实现，只停用不删除 |
| [文件](domains/files.md) | File、FileGrant、FileReference | 复用身份与权限，供业务引用和管理文件 | 三表、本地/S3 多源、上传下载/授权/引用和 Worker 清理已实现；业务维护多文件备份关系；后台已补齐授权编辑与只读引用 |
| [模型接入](domains/model-access.md) | Channel、ModelEntry、ModelInvocation | 复用身份与权限，供业务调用模型与查询费用 | 三表不变，支持 disabled/mock/live；OpenRouter 普通/流式发送、费用及余额通过本机 HTTP 验证，真实账户待联调 |
| [审计日志](domains/audit.md) | AuditLog | 各领域共用的操作留痕能力，独立于调用记录和运行日志 | 单表追加、身份/文件/字典/模型/Flow 留痕、授权分页查询及只读页面已实现；审计自动清理后置 |
| [Flow](domains/flow.md) | Definition（代码）、FlowRun；步骤为引擎只读视图 | 编排普通步骤与 Harness Agent，关联身份、文件、模型调用和审计 | 16 字段单表、DBOS Worker、Harness Agent、发起/详情弹窗、筛选/轮询及崩溃恢复已通过模拟验证 |

```text
docs/
├── README.md                 平台定位、领域索引和阅读约定
├── architecture.md           模块边界、调用关系、API/Worker 与 Ether 参考范围
├── implementation-plan.md    技术选择、版本基线、Ether 复用与实施顺序
├── configuration.md          三层环境变量、示例、优先级与 Compose 传递范围
├── deployment.md             单宿主部署、停写发布、联合备份恢复与可重复验收
└── domains/
    ├── identity.md           身份与权限：设计、模型、规则
    ├── dictionary.md         字典：业务选项、引用、启停与审计
    ├── files.md              文件：设计、模型、规则
    ├── model-access.md       模型接入：设计、模型、规则
    ├── audit.md              审计日志：记录范围、字段、写入与查询规则
    └── flow.md               Flow：单表模型、DBOS 执行与 Agent 接入
```

## 文档约定

- 文中“已确定”表示讨论确认的方向；“建议”“待确定”仍需讨论。文档不是已经实现的功能说明。
- 整体架构维护模块边界和跨模块约束；领域文档维护具体字段、权限组合和生命周期。审计字段仍在各领域完整列出，不为去重拆成公共字段文档。只在新证据或具体业务暴露问题时重开模型决策，避免将参数和待验证项反复扩展成领域设计。
- 各领域负责自己的对象访问规则；身份模块的功能权限不自动替代业务对象授权。文件领域明确内置管理员的 inspect/manage 例外，内容读取仍需显式授权；不扩展为通用管理员绕过规则。
- 按用途选择审计信息：管理对象记录创建/修改，授权关系保留创建信息，文件引用关系的建立与解除通过 AuditLog 追溯，会话和令牌记录生命周期，调用记录保留归属与处理时间；代码目录不增加数据库审计。只列各对象需要的字段，不统一套四字段。
- 删除、撤销和停用分别设计。重要删除操作需要独立日志；只有模型需要保留删除状态/时间时才增加相应字段，不统一软删除。各领域内明确删除策略和留痕方式。
- 新领域沿用相同结构，不再拆成含义模糊的 design.md 与 domain-model.md。领域内部出现足够独立的专题时再考虑拆分。
- keystone/docs/ 是当前设计文档的唯一维护入口。
