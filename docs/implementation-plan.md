# Keystone 技术选型与实现计划

状态：首版持续整理，2026-10-19；六领域最小实现、阶段 6 三批后台整理和单宿主容器部署/联合恢复验收已完成；新增 live 基础能力与 OpenRouter 接入，经本机 HTTP 和独立 DBOS Worker 验证，未发起真实供应商请求。领域基线见 [设计索引](README.md)，目录与进程边界见 [整体架构](architecture.md#5-仓库结构与扩展边界)，运维步骤见[部署与恢复](deployment.md)。本文统一维护技术选择、Ether 复用范围、实施顺序和验收条件。

## 1. 实现方向

keystone/ 作为独立仓库根目录；backend/ 是一个 Python 工程，frontend/admin/ 是首个独立前端工程，其他业务前端按需加入 frontend/。后端用 uv，各前端工程分别用 pnpm 并维护自己的锁文件。API 与 Worker 共用后端镜像，首版各运行一个进程；CLI 是一次性部署/管理命令。共用 PostgreSQL 与文件存储，不因前端项目或后端进程独立而拆数据库。

以 Ether 已有验证为复用基础，直接建立正式工程，不再单独设置前置兼容性验证阶段或演示工程。优先迁移 Ether 的基础代码、边界处理和行为测试，再按 Keystone 模型改造；适配测试在相应模块阶段完成，不把 Ether 整个平台作为运行依赖。身份模块提供够用的认证、授权和审计后，就推进文件、模型和 Flow，不等所有管理页面与优化全部完成。

首版不引入 Redis、消息代理、独立模型网关、Sandbox、插件容器、通用 Repository 或独立前端 SDK 包。登录限流不能因此省略：在单 API 进程边界内采用有界内存限流，明确重启会清空计数；生产入口另提供基础请求限流。扩展为多个 API 进程前，必须确定统一登录限流方案，不能把多个独立内存计数器当作全局限流。

## 2. 技术选择

| 部分 | 首版选择 | Ether 复用与裁剪 |
|---|---|---|
| Python 与依赖 | Python 3.12、uv，backend/ 内单 pyproject.toml 与 uv.lock | 沿用运行基线；去掉多包 workspace 与包发布装配 |
| HTTP API | FastAPI、Uvicorn；Pydantic 2 与 pydantic-settings | 复用请求校验、错误映射、配置、健康检查；显式注册路由 |
| 数据库 | PostgreSQL 17、SQLAlchemy 2 async、psycopg 3 | 复用事务、连接生命周期和有针对性的锁；生产与集成测试都用 PostgreSQL |
| 迁移 | Alembic，一条 Keystone 业务迁移链 | 从新领域模型建立迁移；不复制 Ether 历史迁移。DBOS 表仍由 DBOS 管理 |
| 本地认证 | argon2-cffi；随机秘密凭据与摘要；数据库会话和 PAT | 复用密码校验及凭据安全原语；重写 Cookie 会话入口，不搬入 JWT/RefreshToken 流程 |
| 权限 | PyCasbin，使用已有 UserRole、RolePermission | 保留 Ether 的当前授权加载和管理边界；接入 Casbin，不增加 casbin_rule 表 |
| 文件存储 | OpenDAL，小型存储适配；本地/S3 多源，协调卷仍为同宿主本地卷 | 复用 fingerprint、内部 object_key、流式读写和错误脱敏；S3 已接入，不增加存储管理或副本实体 |
| 模型调用 | OpenAI 兼容协议 SDK 的异步客户端，封装在 model_access | 复用 Ether 的运行时凭据注入、关闭传输重试、错误分类及观测入口；不引入 LiteLLM Proxy/NewAPI |
| 渠道差异 | DeepSeek、OpenRouter、阿里云三个普通适配实现 | 新写费用/余额映射；阿里云账务需要时使用其官方 OpenAPI SDK。未知费用按领域规则保留 |
| Flow 与 Agent | DBOS、Pydantic AI slim、Pydantic AI Harness | 沿用目录、Worker 补投、恢复和 Agent 组织；不搬入人工任务、Sandbox、配额体系 |
| 契约校验 | Pydantic 处理当前输入输出；jsonschema 校验历史输出快照 | 两者分别处理代码契约与持久快照，不新建动态 Schema 领域 |
| 管理前端 | frontend/admin/：Vue 3、TypeScript、Vite、Vue Router、Pinia、pnpm | 复用 Ether 页面布局、组件与交互；请求代码留在应用内，改用 Keystone Cookie/PAT 契约；具体业务前端独立工程、按需创建 |
| 测试与质量 | pytest、pytest-asyncio、Ruff；前端 Vitest、Playwright | 迁移行为用例并重写断言，优先覆盖事务、授权和恢复边界 |
| 部署与日志 | Docker Compose；同一后端镜像启动 API/Worker；标准结构化日志 | 裁剪 Ether 部署，只保留 PostgreSQL、API、一个 Worker、Web 入口和持久卷；迁移为独立部署步骤 |

这里的“一个 PostgreSQL”允许业务与 DBOS 使用独立命名空间，备份时同时包含二者和文件卷。API/Worker 不在启动时争抢业务迁移。管理前端与 API 同源部署，开发时通过 Vite 代理；Cookie、CSRF 与退出撤销按身份文档实现，不把秘密凭据放进浏览器持久存储。

### 2.1 版本如何确定

以下是本次读取 Ether 的 pyproject、uv.lock 与部署文件得到的候选基线，不是“当前最新版本”，也不表示已在 Keystone 验证：

| 依赖 | 本地参考版本或范围 |
|---|---|
| Python / PostgreSQL | 部署文件使用 Python 3.12.14、PostgreSQL 17.11；Keystone 采用同主版本并在建工程时锁定补丁版本 |
| FastAPI / Pydantic | uv.lock：0.141.1 / 2.13.5 |
| SQLAlchemy / psycopg / Alembic | uv.lock：2.0.54 / 3.3.6 / 1.20.0 |
| OpenDAL / argon2-cffi | uv.lock：0.47.10 / 25.1.0 |
| DBOS / Pydantic AI slim | 显式固定：3.2.0 / 2.51.0 |
| Harness | 素材粗选项目显式固定：pydantic-ai-harness[skills] 0.36.0 |
| 模型 SDK | Ether uv.lock 的 openai 为 3.22.1；实际 HTTP 客户端随其兼容依赖组合锁定，不混抄其他版本示例 |
| 前端运行环境 | Ether admin 要求 Node >=24.15.0；以 Node 24、现有 pnpm 锁文件为候选参考，裁剪后生成 Keystone 自己的锁文件 |

依据：[后端依赖](M:/code/ether-workspace/ether/packages/platform/pyproject.toml)、[API 依赖](M:/code/ether-workspace/ether/apps/api/pyproject.toml)、[Flow 依赖](M:/code/ether-workspace/ether/packages/flow/pyproject.toml)、[锁文件](M:/code/ether-workspace/ether/uv.lock)、[素材粗选依赖](M:/code/ether-workspace/ether-flows-media-selection/pyproject.toml)、[前端依赖](M:/code/ether-workspace/ether/apps/admin/package.json)、[后端镜像](M:/code/ether-workspace/ether/deploy/backend/Dockerfile)、[部署配置](M:/code/ether-workspace/ether/deploy/compose.yml)。

PyCasbin 不在 Ether 的该候选依赖中；身份阶段已选择并锁定 pycasbin 2.8.0，经 PostgreSQL 授权场景验证，密码库锁定 argon2-cffi 25.1.0。工程骨架阶段锁定基础依赖，各模块接入时再添加所需库并运行相关检查；安装环境、SDK 组合和 DBOSDurability API 的验证纳入相应模块工作，不作为独立工程。不要直接复制整个 Ether 锁文件或一次升级所有库。Pydantic AI 的 DBOS extra 需要显式纳入所选依赖组合，官方文档支持 slim 包的 dbos extra，见 [DBOS 集成](https://pydantic.dev/docs/ai/capabilities/durable_execution/dbos/)。

### 2.2 三处实现约束

**权限只保留一个数据来源。** 异步读取 Keystone 的角色关系，在当前短操作内构造 Casbin 判断所需的策略；不把全局 Enforcer 当作长期权限快照。所有授权写入仍通过 identity 服务及原业务事务完成，不调用 Casbin 的策略持久化来另写数据库。用户状态、PAT 上限和对象访问规则继续独立检查。Casbin 的策略持久化与 AutoSave 是适配器能力，首版无需使用，见 [Casbin Adapter](https://casbin.apache.org/docs/adapters/)。

**数据库会话按操作隔离。** 同一个原子数据库用例内传递 AsyncSession；并发任务各用自己的会话，Worker 不跨等待持有业务事务。不要用 SQLite 通过的测试替代 PostgreSQL 的行锁、唯一约束、并发授权及 DBOS 恢复验收。AsyncSession 不能供多个并发任务共享，见 [SQLAlchemy asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-asyncsession-with-concurrent-tasks)。

**模型记录靠近真正发送边界。** 普通模型请求和 Agent 每轮请求经过同一 model_access 能力。DBOS、Agent、SDK 的重试与恢复必须统一验证；仅关闭 SDK 重试不足以证明不重发。固定版本的适配必须能为同一持久请求复现 ID、原子登记，并在缺少可恢复结果时阻断发送。验证失败先修适配或版本组合，不静默放宽领域规则，也不先增加第二套调用表。

## 3. Ether 的具体复用范围

| 范围与来源 | 迁移方式 | 必须调整的部分 |
|---|---|---|
| [数据库设施](M:/code/ether-workspace/ether/packages/platform/src/ether/core/database.py)、[API 错误映射](M:/code/ether-workspace/ether/apps/api/src/ether_api/errors.py) | 裁剪后迁移基础实现 | 去掉 SQLite 双后端、配额锁池、应用插件装配；保留实际需要的并发与事务边界 |
| [密码原语](M:/code/ether-workspace/ether/packages/platform/src/ether/user/auth/security.py)、[授权加载](M:/code/ether-workspace/ether/packages/platform/src/ether/user/rbac/engine.py) | 复用哈希、摘要、当前状态检查与测试 | Ether 当前有 JWT 且授权引擎直接计算权限；Keystone 使用随机 Cookie 会话与 Casbin，不能整文件照搬 |
| [文件上传](M:/code/ether-workspace/ether/packages/platform/src/ether/files/service.py)、[引用](M:/code/ether-workspace/ether/packages/platform/src/ether/files/references.py)、[存储](M:/code/ether-workspace/ether/packages/platform/src/ether/storage/backend.py) | 优先迁移已处理过的存储与竞争边界 | 去掉过期字段和终态临时产物清理；保留 Keystone 的固定 owner、引用保留、删除确认规则 |
| [审计写入](M:/code/ether-workspace/ether/packages/platform/src/ether/user/audit/service.py) | 复用显式白名单与调用方事务模式 | 按 AuditLog 当前字段改写；不搬入旧 actor_name、changes 字段或 Department 白名单 |
| [模型运行时](M:/code/ether-workspace/ether/packages/platform/src/ether/ai/runtime.py)、[调用观测](M:/code/ether-workspace/ether/packages/platform/src/ether/ai/invocations.py) | 迁移受控客户端、观测和脱敏思路 | 重做 Channel/ModelEntry 映射与 ModelInvocation 发送登记；去掉配额、连接授权和加密秘密存储；费用/余额适配新写 |
| [Flow 服务](M:/code/ether-workspace/ether/packages/platform/src/ether/flow/service.py)、[协调器](M:/code/ether-workspace/ether/packages/platform/src/ether/flow/reconcile.py)、[引擎封装](M:/code/ether-workspace/ether/packages/flow/src/ether_flow/engine.py)、[Worker](M:/code/ether-workspace/ether/apps/worker/src/ether_worker/runtime.py) | 迁移固定身份补投、查询、取消、停止与恢复机制 | 使用 16 字段 FlowRun；去掉 Task、投影、Sandbox、多 Worker 租约与多版本分发 |
| [素材粗选流程](M:/code/ether-workspace/ether-flows-media-selection/src/ether_flows_media_selection/workflows/select_material_v17/workflow.py) | 参考普通步骤与 Agent 的组合、独立消息上下文与 Skills 组织 | 视频处理、提示词和业务判断留在业务层；不引入 FFmpeg/PyAV 等作为底座依赖 |
| [管理前端](M:/code/ether-workspace/ether/apps/admin/src) | 迁移布局、表格、表单、登录及管理交互 | 移除 Department、应用插件、配额和 Sandbox 页面；重写会话刷新逻辑、类型和 API 契约 |
| 字典 | 按当前两表模型直接实现 | 复用通用事务、权限、审计；不为两张表引入动态配置平台 |

复用按功能小块迁移，每块只带必要依赖与行为测试。保留来源说明；旧模型、接口名和测试断言都要按 Keystone 规则核对，不能把旧测试通过等同于新设计正确。现有 Ether 数据迁移不是首版目标，建库从空库开始；若以后要导入历史数据，另行定义映射。

## 4. 实现顺序与完成条件

| 阶段 | 交付范围 | 完成条件 |
|---|---|---|
| 1. 仓库与工程骨架 | backend 单工程；API/Worker/CLI 入口；配置、数据库、Alembic、日志、错误映射；frontend/admin 最小工程；部署基础 | API 与 Worker 可独立启动/停止；迁移命令可连接空库；admin 可独立构建且经开发代理访问 API 健康接口；没有隐式建表或运行时迁移竞争 |
| 2. 最小身份与审计 | 用户、角色、Casbin、会话、PAT、AuditLog、管理员初始化；最小登录界面 | Cookie 登录/退出、PAT 调用、撤权与最后管理员保护生效；成功变更与审计同事务；不等待完善所有管理页面 |
| 3. 文件管理与字典 | File/Grant/Reference、存储卷；Dictionary/Item | 上传、读取、授权、引用与删除链路通过；并发引用不能绕过删除；字典所属校验、启停和历史显示符合规则 |
| 4. 模型接入 | 三个实体、统一发送边界、模拟普通/流式请求、渠道适配 | 每次尝试登记与未知费用正确；配置与历史权限分离；余额刷新无需 Worker；先用模拟响应验收，再有界联调真实渠道 |
| 5. Flow 接入与业务扩展 | platform/flow 的运行机制；businesses 下最小示例流程、普通步骤与 Agent；受控文件和模型能力 | 完成下述端到端链路；重启、取消竞争、撤权、重复受理和文件保留通过；API 不加载执行器；新增示例流程不修改平台核心 |
| 6. 管理界面与部署验收 | 补齐 admin 的用户/角色/PAT、文件、字典、模型配置/费用、审计和 Flow 页面；完成 Compose | 浏览器端权限表现与 API 一致；同一后端镜像以不同命令启动 API/Worker/CLI；冷启动、迁移、备份恢复与完整链路通过；不为验收虚构业务前端 |

阶段 1 骨架、阶段 2 身份/审计、阶段 3 文件/字典、阶段 4 模拟模型、阶段 5 Flow 和阶段 6 管理/部署验收均已完成，容器验收结果见 4.12（4.2 保留当时的环境限制记录）。具体业务采用和真实渠道继续按需推进。每阶段交付可运行的功能、对应迁移和必要适配测试，更新相关说明后再推进；不一次性生成所有领域的空壳。各阶段同步提供够用的 API 和页面，阶段 6 补齐使用体验，不把所有前端工作压到最后。具体业务前端只有在业务场景确定时单独排期，不是底座交付的前置条件。

第一个正式纵向链路固定为：登录 → 上传小文件 → 发起文件审查 Flow → 一个 Agent 读取文件并调用模型 → 生成报告文件 → 查询结果、文件及调用费用 → 业务显式采用。先采用模拟模型，费用未知和无权限分别展示；渠道真实联调时逐个确认能力，不用价格估算替代实际费用。

字典在身份之后即可独立完成，不作为 Flow 接入的技术前置条件；不需要为验收底座先实现素材粗选整个视频业务。业务示例使用正常业务模块和显式注册，不增加独立流程发布包。

### 4.1 阶段 1 实施清单

实施清单已完成以下代码与本地检查；容器运行的后续验收已在 4.12 补齐：

- [x] 在 keystone/ 初始化独立 Git 仓库与 .gitignore，保留现有 docs；不把历史资料或 Ether 源仓库复制进来。
- [x] 建立 backend/ 的 Python 包、pyproject.toml、uv.lock，按已确定目录放置实际需要的启动和基础设施代码；领域代码随阶段加入。
- [x] 建立 API、Worker、CLI 三个薄入口与显式装配。API 先提供健康/就绪检查，Worker 先提供受控启动和停止，不将尚未接入的 Flow 标为可用；CLI 先提供迁移入口，管理员初始化随身份模块实现。
- [x] 裁剪 Ether 的配置、数据库生命周期、错误映射与日志设施；区分短业务事务和外部 I/O，不搬入 SQLite、配额、插件或 Sandbox 分支。
- [x] 在 backend/migrations/ 配置单条 Alembic 业务迁移链，仅管理 Keystone 表；领域迁移随实现增加，DBOS schema 在 Flow 接入时按其机制初始化。
- [x] 建立 frontend/admin/ 的独立工程、锁文件、基础页面与 API 请求入口；只显示真实健康状态，不预填模拟业务数据。不创建占位 business-app 或共享包。
- [x] 建立 deploy/ 的共用后端镜像、Compose 与入口代理基础配置，覆盖 PostgreSQL、API、Worker、admin；迁移作为一次性命令，文件卷在文件阶段启用。
- [x] 检查后端导入与启动、独立进程停止、迁移连接、前端构建及健康接口联通，补充真实开发命令。
- [x] 后续 4.12 已补齐后端/admin 镜像构建、Nginx 代理及 Compose 冷启动/停止和联合恢复验收。

阶段 1 不要求完成登录、文件、模型调用或 DBOS 流程；这些能力必须在其阶段完成后才宣称可用。迁移和启动检查使用独立开发数据库，不操作 Ether 的现有数据库或文件卷。

### 4.2 阶段 1 实施记录

2026-10-18：工程骨架已落在正式仓库内。基础依赖按 Ether 候选裁剪并生成自己的锁文件；TypeScript 采用 Ether 锁文件中的 6.0.3。Vue Router、Pinia、Vitest、Playwright 和领域依赖在对应功能需要时加入，健康页不预装。Windows 异步入口直接复用 ether.core.asyncio 的 Selector 循环，修复真实 PostgreSQL 验证发现的 psycopg 不支持 Proactor 的问题。

| 检查 | 本轮结果 |
|---|---|
| 后端静态检查与打包 | Ruff 检查、格式检查通过；uv build 生成 sdist 与 wheel |
| 后端测试 | 5 项通过，包含真实 PostgreSQL 空库检测、重复迁移、版本差异、事务回滚、错误脱敏和 Worker 资源释放；保留上游 TestClient/httpx 弃用提示，未屏蔽 |
| CLI | migrate 可重复运行；revision 返回当前迁移头；check 确认数据库与迁移就绪 |
| 独立进程 | API、Worker 分别启动，Ctrl+C 后分别完成生命周期清理；Worker 明确 Flow 未启用 |
| 前端 | TypeScript 检查和生产构建通过；浏览器通过 Vite 代理访问真实 API，确认未配置与已就绪两种数据库状态 |
| 部署 | Compose 配置解析通过；Docker Desktop 因 sailor-ingest.sock 文件访问错误退出，镜像构建、Nginx 运行和 Compose 冷启动尚未验证 |

真实数据库验证使用 [EDB 官方 PostgreSQL 二进制](https://www.enterprisedb.com/download-postgresql-binaries) 17.11，位于忽略的 .cache/postgres，独立监听回环端口 55433。测试入口库 keystone_test，每轮新建并清理随机命名的测试库；未访问 Ether 的数据库。临时服务在验证后停止，缓存不属于项目交付依赖。日常开发仍按 Compose 或自行提供的 PostgreSQL 运行。

阶段 1 交付时迁移基线只有 keystone.alembic_version，尚无领域表或 DBOS 表，管理后台只展示服务状态；未创建空领域目录、业务占位工程、共享前端包或独立兼容性验证项目。开发、测试、迁移和部署命令集中维护在[仓库 README](../README.md)。

### 4.3 阶段 2：最小身份与审计

2026-10-18：已按收敛模型完成六张身份表和一张 AuditLog，不增加租户、Department、SSO、JWT、策略表或权限缓存。

- 后端：用户创建/资料/启停、密码变更/重置、角色 CRUD 和分配、当前权限加载、Cookie 会话、PAT 签发/查询/撤销、他人凭据批量撤销、审计追加与分页查询。
- CLI：新增交互式 init-admin；无默认密码，无密码命令参数，已有用户时拒绝重复初始化。正式命令已在独立浏览器验收库执行。
- 前端：本阶段交付登录、刷新恢复会话、强制/主动改密、退出、个人令牌创建/列表/撤销和健康状态；用户/角色/审计管理页面后来在阶段 6 第一批完成，见 4.9。
- 权限：PyCasbin 使用当前 UserRole/RolePermission 构造短期 Enforcer；PAT 与用户当前权限取交集。普通全权限角色不具备内置管理员身份，管理操作检查变更前后范围。
- 并发与审计：身份共享/排他事务锁保护短操作；哈希在写锁外，签发前复查。成功日志同事务，失败/拒绝在回滚后独立记录，失败审计不可用不覆盖原始结果。
- 登录保护：精确 Origin、会话绑定 CSRF、HttpOnly/SameSite Cookie、生产 Secure/__Host- Cookie、单 API 有界限流；Nginx 补充登录入口限流配置。

身份与审计当前分别由 0001_identity、0002_audit 迁移负责；模型字段、生命周期和管理规则详见[身份领域](domains/identity.md#7-最小实现约定)与[审计领域](domains/audit.md#7-当前实现)。FileGrant 删除检查、FlowRun 对 PAT 的引用保护以及自动清理在对应模块落地，不预建空表。

验收：19 项后端测试通过，涵盖真实 PostgreSQL 空库迁移及 ORM 一致性、Cookie/CSRF、强制改密、PAT 上限与失效、权限恢复、管理员特殊身份、管理范围并集、并发最后管理员、登录/PAT 签发与停用竞争、初始化防重复、审计回滚/独立失败日志/历史目标/操作者、敏感字段与限流，以及未变化授权保留原创建审计。Ruff、Python sdist/wheel 和前端 TypeScript/生产构建通过。保留原有 TestClient/httpx 上游弃用提示。

真实浏览器验证登录、页面刷新后恢复会话、个人令牌页面与退出；改密和 PAT 生命周期由接口集成测试验证。测试使用随机隔离数据库，浏览器使用一次性 keystone_browser_test，验收后清理，未创建真实部署账户。Docker 引擎故障仍使镜像构建、Nginx 实际限流和容器冷启动未验收，不能将配置检查当作运行验证。

### 4.4 阶段 3：文件管理与字典

2026-10-18：新增 File、FileGrant、FileReference、Dictionary、DictionaryItem 五表，字段保持已确定模型，不增加上传任务、文件版本、字典引用或通用软删除表。当前按领域拆为 0003_dictionary 与 0004_files。

- 文件：沿用 Ether 的 OpenDAL 0.47.10、卷标识校验、显式对象授权及业务引用思路；按 Keystone 规则保留同一上传行和授权、只更新变化引用、明确管理员 inspect/manage 例外。共享本地卷使用 portalocker 3.2.0 协调跨进程上传/读取/清理，避免数据库连接断开使旧写入与清理交叉；本阶段采用本地源，当前本地/S3 多源配置见 4.23。
- 字典：两表管理、通用读取与业务内部校验、停用历史解析；父字典行锁协调选择与停用；标识不可变且不删除。无预置业务示例数据。
- API/Worker：上传下载、管理授权、同事务引用库接口；Worker 每轮有界清理 deleting 和中断上传残留。角色删除已检查 FileGrant。成功审计与数据库变更同事务，失败清理保留授权。
- 页面与部署：本阶段交付文件库上传/分页/下载/改名/启停/删除状态，字典及选项管理；授权编辑和引用查看后来在阶段 6 第二批完成，见 4.10。共享 file_data 卷、独立 init-storage 命令和 Compose storage-init、16 MiB 代理限制已配置。就绪检查增加卷标识状态。

验收：33 项后端测试通过（新增 14 项），真实 PostgreSQL 验证模型与迁移一致性、幂等与冲突、owner 无隐式权限、管理员内容读取限制、PAT 范围、角色引用保护、失败上传重试不补授权、引用/删除竞争、撤权后受限完成、删除审计失败回滚、跨进程文件锁及取消等待原生 I/O、字典唯一性/历史/停用并发与审计回滚。Ruff、Python 打包、前端 TypeScript/生产构建和 Compose 配置检查通过；保留原有上游 TestClient/httpx 弃用提示。

浏览器使用一次性测试库验证字典/选项创建、文件列表及改名；上传和删除由真实 API/集成测试验证。独立 API 和 Worker 实测删除受理后内容清理、状态推进及授权清除。测试账户和数据库在验收后清理，未使用 Ether 数据。Docker 引擎故障仍未解决，容器卷权限、镜像构建和 Compose 冷启动未验收；Flow 专属文件保留和结果同步在阶段 5 实现。

### 4.5 阶段 4：模型接入模拟验证

2026-10-18：新增 Channel、ModelEntry、ModelInvocation 三表，当前对应迁移 0005_model_access。按已确定字段实现，不增加模型名称、business_ref、价格表、钱包、路由池或任务表；配置只停用不删除。

- 复用：参考 Ether ai/runtime.py、dispatch.py、invocations.py 的凭据注入、关闭重试、受控 HTTP 和响应观测；锁定 openai 3.22.1、httpx2 2.13.1，按 Keystone 发送前登记规则改写。没有增加 LiteLLM/NewAPI 服务。
- 后端：管理配置、文本普通/流式调用、用量/费用记录、权限过滤后的历史与汇总、同步余额及单次费用补查。秘密只通过部署环境按引用注入，调用/配置/历史权限独立。
- 故障：实际发送前原子插入并提交记录；稳定 ID 防止并发/恢复重复发送，SDK 零自动重试。超时、断流、执行中断和结果无法确认保留 unknown；结果补全数据库故障不触发重发，running 遗留不按时间强制改写。真实 DBOS 协调仍在阶段 5。
- 渠道：三个明确的费用/余额适配器，加全局显式 mock 传输。SDK 的实际发送路径经过内存响应，不访问网络。默认 disabled，生产禁止 mock；阿里云真实账务 SDK/地域/签名及三个渠道真实调用均未联调。
- 前端与部署：模型配置、启停、余额刷新、普通/流式模拟调用、历史、费用汇总和补查；请求不自动重试，展示模拟标识、未知费用和单位。Compose/API 配置显式传入模式与秘密映射；Worker 无需增加模型任务。

验收：完整后端 53 项通过，新增 20 项模型集成场景，包含真实 PostgreSQL 迁移/ORM 一致性、三渠道 SDK 普通/流式响应、发送前提交、稳定 ID 并发与成功后重放、无重试、429/超时/断流/取消、数据库登记/补全故障、精确费用与零/未知区分、固定目标、PAT/历史权限、撤权后收尾、审计回滚、余额失败/在途配置变化及费用补查。Ruff、Python sdist/wheel、前端 TypeScript/生产构建、Compose 配置解析通过。

浏览器使用一次性数据库验证 OpenRouter 流式模拟调用、DeepSeek 普通调用与未知费用、OpenRouter 费用补查更新原记录、余额刷新；三种余额分别显示，不跨账户求和。测试账户、数据库和临时服务验收后清理。没有真实付费请求，也未访问 Ether 数据。Docker 引擎故障仍使容器运行与冷启动未验收；模型内部接口可供下一阶段集成，但不表示 Harness/DBOS 已完成接入。

### 4.6 阶段 5：Flow 模拟验证

2026-10-18：新增 16 字段 FlowRun，总计十六张领域表，当前对应迁移 0006_flow。沿用 Ether 的受理后补投、固定执行身份、DBOS 查询/取消、宿主能力与生命周期结构，按 Keystone 简化模型适配；没有 Task、Sandbox、Worker 租约、多版本分发或业务投影表。

- 依赖锁定 DBOS 3.2.0、pydantic-ai-slim[dbos] 2.51.0、pydantic-ai-harness 0.36.0、jsonschema 4.26.0。一次性 migrate-flow 管理独立 dbos schema，启动不迁移。
- bootstrap 注册轻量契约，Worker 单独导入业务 workflow。新增 businesses/document_review，普通步骤与 Harness Skill/Agent 混合；每轮模型请求复用 model_access，文件通过原身份和权限服务访问。
- API 支持目录、幂等受理、分页、详情、取消、步骤白名单和费用查询；管理后台能发起、轮询、查看步骤/费用并下载报告。原模型选择固定在 input，费用和文件授权独立检查。
- 受理/输入引用/审计及结果/输出引用/审计分别原子提交；报告上传完成即登记产物引用。PAT 撤销阻断新操作，既有事实仍同步；取消不撤回已发生的调用或文件写入。
- PostgreSQL 全量 67 项测试通过，其中 Flow 14 项。真实子进程验证发送前登记、响应后检查点缺失、报告上传后检查点缺失三种崩溃恢复；前两者拒绝重发，后者复用检查点及原文件完成。覆盖并发幂等、审计回滚、跨用户取消、在途取消、受限输出引用、单 Worker 互斥与正常停止。
- 浏览器在一次性库验证“API 先受理、Worker 后启动”的完整链路，显示两轮模型调用、报告与模拟费用。验证后清理测试库、文件、账户与临时进程。未访问 Ether 数据或真实渠道。

当前一个 Worker、队列并发 2、同卷 OS 排他锁；API 就绪不代表 Worker 在线。输入/结果、调用及时间预算见 Flow 文档。无 Run/DBOS 历史自动清理；具体业务显式采用和相应并发用例留到业务出现后完成。Ruff、Python 打包、前端构建和 Compose 配置通过；Docker 引擎故障导致容器构建/冷启动仍未验收。

### 4.7 初始迁移按领域重整

2026-10-18：将未发布阶段按实施批次编写的迁移重新整理为 identity、audit、dictionary、files、model_access、flow 六份初始迁移。移除空 baseline，统一文件名与 revision，保留单链、单 head，不增加迁移装配层、领域分支或新的表。迁移内显式列出字段、约束和索引，不依赖运行时 ORM 或动态字段拼装。

使用隔离 PostgreSQL 库分别执行旧链和新链，确认全部领域表的字段、默认值、约束和索引一致。新增逐领域升级、逆序回滚、重新升级及 DBOS schema 保留验证；全量 68 项测试与 Ruff 通过。当前初始结构及后续增量规则见[迁移说明](../backend/migrations/README.md)。

### 4.8 跨领域实现收尾

2026-10-18：按代码与领域模型核对结果完成四项修复，保持十六张领域表、六份初始迁移和既有权限目录不变。

- 文件修改和删除统一返回 ID 回执，完整元数据继续通过 inspect 查询；覆盖 manage-only PAT 及缺少 inspect 对象授权的普通用户。
- 字典完整表单保存与 before/after 审计使用相同字段范围，省略说明或排序字段导致的默认值写入也保留旧值。
- 普通模型调用新增 recover-model-calls --confirm-api-stopped 离线命令。由操作者确认全部普通调用进程已停止；每批最多 100 条遗留调用收尾为 unknown，保留原用量、费用与请求编号，逐条系统审计与该批变更原子提交。重跑幂等，不处理 Flow 调用，也不发送上游请求。
- 角色删除由 bootstrap 装配 use_cases.RoleDeletion，调用身份及文件模块入口，在同一身份排他事务内完成检查、删除和审计；移除身份服务对文件表的直接依赖。覆盖文件授权引用保护、并发新增授权等待与审计失败回滚。

整体架构、领域文档、索引和操作说明同步更新；审计示例统一为实际的 file.reference、file.delete.request/complete、file.cleanup.failure，离线收尾增加 model.invocation.recover。不增加通用 guard、恢复任务表或新领域。

验收：新增 6 项测试，全量 74 项通过，包含真实 PostgreSQL 迁移与事务验证、真实 DBOS Worker 崩溃恢复；Ruff 与 83 个 Python 文件的格式检查通过。离线命令通过实际 CLI 分支验证明确确认、范围排除、费用保留、补查恢复、幂等和审计失败回滚。前端现有文件编辑/删除不读取响应元数据，无须修改。未连接业务数据库或真实模型渠道；本轮未重复前端构建或 Docker 验收。

### 4.9 管理后台第一批

2026-10-18：在既有 Vue/Vite 后台内完成基础整理及用户、角色、审计页面，保留文件、字典、模型和 Flow 原有功能。后端领域代码、十六张表、迁移、API 和审计操作名不变。

| 范围 | 本批交付 |
|---|---|
| 后台基础 | /admin/ 路由、深链接与刷新、登录回跳、强制改密入口、权限导航、共享会话、统一 401/403 处理；身份变化后清空旧页面并丢弃旧请求结果 |
| 用户 | 搜索分页、创建、展示名修改、启停、密码重置、撤销全部凭据、逐角色分配；不增加删除、部门和导入 |
| 角色 | 普通角色创建/修改/删除、按组筛选权限、只读权限目录；内置角色和可授予范围在界面约束，最终管理范围和引用保护由 API 判断 |
| 审计 | 操作/操作者/目标/结果/时间过滤、分页及只读详情；目标 ID 保留字符串语义，用户/系统/匿名分别显示 |
| 公共交互 | 原生 dialog、提交防重、服务端错误展示、50 条分页、窄屏表格横向滚动；密码在尝试提交和关闭时清空 |

参考 Ether admin 的 router/auth、access 状态、usePagedList、AppDialog 和用户/角色/审计页面，复用会话复查、原生对话框及请求代次处理方式，按 Keystone 的字段和授权规则裁剪。不复制 Department、用户归档、批量导入、独立会话列表或 Ether 客户端包；不引入 Pinia、通用授权框架及共享前端工程。

依赖沿用现有 Vue/Vite/TypeScript，新增 Vue Router 5.3.1；测试使用 Vitest 5.0.2、Vue Test Utils 2.5.1、jsdom 30.1.1 和 Playwright 1.63.0，版本锁入 pnpm-lock.yaml。

验收：14 项前端测试通过，覆盖只读权限、提交防重和秘密清理、逐角色授权、内置/越权角色、引用拒绝、审计字符串目标和文本转义、失效请求丢弃、401/403、强制改密及登录回跳。另有 1 项真实 PostgreSQL/API/Chromium 连续场景通过，覆盖用户管理、角色分配、只读访问、首次改密、引用拒删、凭据撤销及审计查询，并检查桌面/窄屏截图和页面错误。vue-tsc 与 Vite 生产构建通过，浏览器测试启动脚本通过 Ruff。

浏览器测试使用专用入口库派生随机测试库、临时文件目录和独立 API，结束时清理；不访问业务库、Ether 数据或真实渠道。运行命令见[仓库测试说明](../README.md#检查与测试)。后端生产代码未改，本批未重复上一批 74 项后端测试或 Docker 验收。文件授权/引用界面、字典体验、模型/Flow 运维细化继续分批处理，阶段 6 尚未全部完成。

### 4.10 管理后台第二批

2026-10-18：补齐文件授权、引用查看和字典管理体验，沿用现有 API、权限目录和审计操作名。后端生产代码、领域模型、十六张表和迁移不变，未增加依赖。

| 范围 | 本批交付 |
|---|---|
| 文件库 | 统一分页、加载/错误状态、编辑与删除确认弹窗；可展开文件 ID、所属人与时间，继续保留上传请求键和独立内容下载检查 |
| 文件授权 | 用户/角色/所有登录用户三类主体；分别授予查看信息、读取内容、管理权；分页、重复授权、撤销确认和撤权后重新查询；所有登录用户不能获得管理权 |
| 主体选择 | 具备 user:read 时搜索分页用户，具备 role:read 时分页角色；否则填写已知 UUID，不要求为文件管理额外授予目录读取权限 |
| 文件引用 | 管理者分页查看 owner/slot，刷新与空状态；没有解除引用入口，删除冲突保留服务端原因。可按已知文件 ID 打开授权和引用，不绕过对象权限 |
| 字典 | 目录和选项独立分页、刷新与只读浏览；编辑使用独立弹窗，固定 key/value，完整保留说明与排序，区分父字典停用和选项停用 |

参考 Ether 的 FilesView、GrantPanel、SubjectPicker 和 FileReferencesDialog；复用上一批的 AppDialog、ListPager、usePagedList 和会话处理，按 Keystone 的授权组合、50 条分页及管理接口适配。没有复制 Ether 的通用连接授权、前端对象能力字段或业务引用写入功能。Ether admin 未找到可直接搬用的字典页面，字典交互基于现有接口整理。

验收：新增 11 项前端测试，合计 25 项通过；新增 1 项真实 PostgreSQL/API/Chromium 场景，与上一批场景共 2 项通过。覆盖无目录权限手填 ID、选择器分页、共享主体限制、提交防重、自身管理权撤销、引用分页与文本转义、删除中禁止改授权、只读字典、跨字典请求竞争、完整编辑字段与整数排序。真实浏览器验证上传、角色查看授权与用户下载授权分离、重复授权、撤权后列表清除、引用阻止删除、字典创建/选项排序和父子独立启停。检查桌面/窄屏截图，无页面脚本错误；补齐授权选择器明确的可访问名称。TypeScript、生产构建和浏览器测试脚本 Ruff 检查通过。

浏览器夹具通过现有文件服务和可信 FileReferences 创建一条业务引用，用于验证只读展示和删除保护；仅存在于随机隔离测试库与临时文件目录，不增加生产接口。测试入口连接增加 5 秒超时，避免端口未启动时无限等待。运行方式仍见[仓库测试说明](../README.md#检查与测试)。本批未重复后端全量 74 项测试或 Docker/Worker 验收，未访问 Ether 数据或真实渠道。

### 4.11 管理后台第三批

2026-10-18：整理模型接入与 Flow 管理，沿用现有 API、领域字段、十六张表和审计操作名。后端生产代码、迁移和依赖均未改动，真实供应商发送仍未开放。

| 范围 | 本批交付 |
|---|---|
| 渠道与模型 | 独立编辑弹窗、只停用不删除；保留引用名/账户字段，固定服务商、模型 key 和已确定账户；配置与选择器按 50 条分页，保留当前不在页内的选择 |
| 余额与调用 | 余额成功时间与尝试时间分开，失败时明确旧快照；普通/流式模拟调用、停止接收、不自动重试，离页清空输入输出 |
| 调用记录 | 渠道/Flow Run/时间筛选与汇总一致，记录详情展示用量、费用来源、上游 ID、错误及 Flow 关联；费用按渠道和单位分组，不把未知视为零 |
| Flow 发起 | 独立弹窗，文本审查使用分页文件/模型选择，其他定义支持 JSON 对象；同页关闭重开保留请求键，修改输入或显式准备新请求才重新生成 |
| Flow 详情 | 状态筛选与分页、运行/步骤/费用分别处理失败、未完成运行每 3 秒刷新、关闭丢弃旧响应、取消确认及异步状态展示、报告下载沿用文件授权 |

参考 Ether ModelProbe、FlowStartDialog、FlowRunDialog 和 useFlowRunDetail，复用失效结果处理、操作隔离及分区查询方式；沿用本后台原生 dialog 和分页组件。未引入通用连接授权、人工任务、可视化流程编辑器或通用 Schema 表单框架。所有页面已使用共享身份状态，不再传入权限副本。

验收：本批新增 15 项前端测试，合计 40 项通过；覆盖只读隔离、固定编辑字段与跨页选择、过期余额、相同筛选区间、停止接收/卸载、流内 401 会话清除、Flow 幂等键保留、JSON 校验、步骤失败、取消确认、撤权清空与轮询清理。TypeScript 和生产构建通过，浏览器测试脚本通过 Ruff 与格式检查。

浏览器测试扩展隔离夹具，初始化独立 DBOS schema、启动真实 API 和 Worker，仅注入假秘密及 mock 模式。共 3 项场景通过，前两批回归保持通过；新增场景覆盖 DeepSeek、OpenRouter、阿里云百炼的配置与模拟余额，普通/流式发送、未知/已知费用及费用补查；Flow 由真实 Worker 执行 Agent 两轮模拟调用，页面确认终态、步骤、0.0024 credits 模拟费用、报告下载和重复提交返回同一运行。桌面/窄屏截图、页面错误及弹窗横向溢出已检查。

测试结束停止临时 API/Worker 并清理随机库与文件目录。未访问 Ether 数据或真实渠道；本批未重复后端全量 74 项测试、崩溃恢复矩阵或 Docker 验收。运行方式见[仓库测试说明](../README.md#检查与测试)。阶段 6 后台三批已完成，部署与备份恢复仍待验收。

### 4.12 部署、冷启动与备份恢复

沿用 Ether `tests/deploy/verify.py` 和部署手册的随机 Compose 项目、真实镜像、停写备份与新环境恢复方式，裁剪为单 API/Worker、PostgreSQL 和文件卷。没有新增领域、表、迁移或产品运维接口。Compose 的后台入口改为等待 API 健康，三个镜像引用和宿主端口可配置。

新增独立验收入口 `tests/deploy/verify.py` 与 Chromium 检查 `tests/deploy/browser.mjs`。九组实际容器检查通过：

1. 迁移失败阻断 API、Worker 和后台启动。
2. 空卷冷启动、重复业务/DBOS 迁移及文件初始化、十六张领域表和 UID 10001 运行。
3. Nginx 静态资源/深链、登录/CSRF、角色/文件/字典、三个模拟渠道与 SSE。
4. Chromium 访问构建后的后台、登录、刷新恢复和 Flow 页面。
5. 真实 DBOS/Harness 完成流程；停止 Worker 后新提交的 Flow 保持 pending。
6. 只恢复数据库且挂载新卷时，旧文件读取返回 503，不绕过卷指纹检查。
7. 联合恢复保留会话、角色和文件授权、字节、字典、渠道余额、调用费用及 DBOS 步骤历史。
8. pending Flow 在原 ID 下完成，幂等请求返回原 ID，已完成调用不重发，恢复后可以新上传。
9. `down` 保留卷后重新 `up`，原报告仍可读，模型调用数量不变。

真实镜像构建包含后端锁定安装、前端 vue-tsc/Vite 构建；验收脚本 Ruff/格式检查通过。恢复前有 5 条模型调用，恢复 pending Flow 后为 7 条，再次重建不增加；原 5 条记录及终态 Flow 的步骤/费用保持不变。源环境持续停写，测试结束删除两个随机项目和卷；未访问 Ether 数据、开发数据库或真实渠道。

部署手册明确全库加文件卷成套保存、保留原镜像和实际配置、恢复到新项目、会话/PAT 的备份时点语义，以及外部费用/副作用不会回滚。容器 ready 与 Worker/历史文件完整性是不同检查。运行命令和产物位置见[部署验收](deployment.md#5-可重复验收)。

本批只修改部署配置、文档和测试；后端领域与前端产品代码未改，未重复此前 74 项后端、40 项前端测试及 3 项页面 E2E。此次新增真实容器/Chromium 验收不替代既有任意执行位置的崩溃恢复测试。生产 TLS、异地加密备份设施、多个宿主/Worker、S3/NAS 和真实渠道仍不在本轮验收范围。

### 4.13 live 基础能力与 OpenRouter

2026-10-18：保留原有三张模型领域表、十六张总表、迁移和依赖，增加显式 live 运行模式。默认仍为 disabled，production 继续拒绝 mock；DeepSeek 和阿里云暂不开放真实发送。

复用 Ether ai/runtime.py、dispatch.py 和 invocations.py 的受控 SDK 边界：固定目标、临发送权限复查、凭据注入、网络及 SDK 零重试、禁止跳转和环境代理。Keystone 继续先登记调用再发送，普通调用和 Flow/Agent 使用同一路径；OpenRouter 只允许官方地址和推理、费用补查、余额三个路径，没有引入独立网关。

响应 usage.cost 与费用补查 total_cost 作为账户实际报告值，未知保持未知；Management Key 单独查询账户 credits 余额，不影响无账务 Key 的推理。补齐 HTTP 402 安全错误码及 HTTP 200 内错误处理，流内失败保留已收到的费用和编号。失败账务查询保留已知事实，补查不重发推理。后台明确显示 live、真实发送按钮及费用提示，Flow 发起也显示模式；live 新配置默认官方地址，上游模型由用户填写。

验收：新增 9 项后端测试，全量 **83 项通过**；新增 2 项前端测试，合计 **42 项通过**；既有真实 PostgreSQL/API/Worker/Chromium **3 项场景回归通过**，TypeScript、生产构建及 Ruff 检查通过。新测试使用独立协议响应、本机真实 HTTP socket、假秘密与隔离 PostgreSQL，覆盖 SDK 普通/SSE、费用与余额、凭据分离、非法目标、无重试/跳转、HTTP 200 错误、费用保留、稳定调用 ID 防重发及独立 live DBOS/Agent 执行。测试只替换最后的 socket 目的地，产品没有测试专用目标豁免。

未读取真实密钥、访问 Ether 数据或向供应商发起请求；未重复上一批九组 Docker 备份恢复验收。mock 与 live 使用独立数据库和文件卷，不把已有模拟环境直接切换为真实环境，不新增运行模式字段。配置方式、官方协议依据和账户待验证项见[OpenRouter 接入](domains/model-access.md#78-openrouter-live-接入)。

### 4.14 模块分层与统一代码结构

2026-10-18：按 MVC 职责和 Service 用例层统一六个平台模块，保留领域聚合目录。每个模块使用 controller.py、service.py、models.py、schemas.py；Vue 承担 View，后端不增加模板层。具体命名、依赖和扩展规则见[模块规范](architecture.md#54-模块分层与代码规范)。

原 routes.py 统一改名为 controller.py 并更新装配；文件、字典及 Flow 契约和响应投影移入 schemas.py，通用 Input 移入 core/schemas.py。审计和字典查询从 Controller 移入服务，Flow 步骤读取及模型费用协调分别移入 FlowService 和 FlowCosts；保留查询前后授权复查。身份限流及审计、权限目录、文件预检、模型配置/发送前置规则也由服务负责。SSE 编码、Cookie/CSRF 与请求体读取保留在 Controller。

没有新增领域对象、迁移、依赖、Repository 或 BaseService，未修改公开接口路径及字段；对比重构前后的完整 OpenAPI 文档一致。新增 6 项模块结构检查，防止 Controller 直接访问 ORM、业务服务依赖 HTTP，以及 Schema 查询数据库。

验证覆盖 92 项后端用例：首轮 84 项通过；测试库端口未匹配导致的 7 项 setup 错误，及 1 项历史查询失败，在纠正测试端口后重跑 8 项全部通过。3 项真实 PostgreSQL/API/Worker/Chromium 场景回归通过，Ruff 和格式检查通过。浏览器夹具仍使用随机测试库和模拟渠道；没有访问真实模型。前端产品代码未变，本批未重复前端单元测试或 Docker 备份恢复矩阵。

本地 keystone-live 后端镜像更新后仍保持所有容器停止，专用联调数据库和文件卷不变。源码结构变动会改变 DBOS execution_version；正式升级仍按停写发布规则处理旧运行，不能直接改绑执行版本。本地准备环境没有历史 Flow Run。

### 4.15 领域内目录与职责收敛

2026-10-19：六领域统一为 controllers/、services/、models/、schemas/，16 个持久化实体各自一个文件，关联实体同样独立；Controller/Service 按资源用例组织。Identity 提取 AccessContext，账户管理拆为认证、用户、角色与令牌服务；Audit 写入与查询分离，避免上下文循环依赖。

Flow 不直接操作文件和模型接入实体：模型选择与中断收尾交给 Model Access，受控文件引用交给 Files；保持调用方事务、共享行锁、Worker 独占锁及有效使用记录检查。所有原自动遍历 ORM 列的响应改为明确字段，角色权限读取和投影分离。更新启动、CLI、Worker、测试和 E2E 装配路径。未新增领域表、接口或通用 Repository/BaseService。

验证完成：最终源码的完整后端回归共 102 项，100 项通过；两项测试补齐导入、明确费用统计时间窗口后复测通过。新增递归分层、相对导入、上下文职责及响应字段检查，原权限、事务、文件保留、DBOS 取消/恢复与本机 OpenRouter 协议测试继续覆盖。三项真实 API/PostgreSQL/Worker/Chromium E2E 全部通过，均使用模拟渠道。OpenAPI JSON、16 张业务表及索引的 PostgreSQL DDL 与重构前快照完全相同，Ruff 检查和格式检查通过。未改前端产品代码，因此不重复前端单元测试和部署/备份矩阵。测试使用本机临时 PostgreSQL 服务，随机测试库由测试工具清理，完成后关闭测试服务。容器保持停止，不配置真实密钥、不向真实供应商发送请求；本批不重建镜像。源码变化会改变 DBOS execution_version，恢复或发布仍遵守停写发布与旧运行版本规则。

### 4.16 共用规则与领域边界收敛

2026-10-19：按复查的六项建议继续小范围整理，沿用 Ether 的受控文件与执行上下文边界，不新增层级、领域表、迁移或依赖。

- ExecutionIdentity 只覆盖身份授权，复用 AccessContext 的事务、身份锁、参数契约、失败审计与冲突转译。
- Flow 文件入口返回 bytes，接受读取上限与上传 MIME；示例的 UTF-8/16 KiB 规则回到 document_review。Files 内部上传入口负责幂等校验和原子引用保留，不再把 ORM 传给 Flow 回调；保留完成后再次授权。
- 审计与模型历史共用时间窗口，支持仅传历史 until；默认截止和审计写入使用数据库时钟，移除测试中放宽默认查询窗口的绕法。
- live 模型目录及发送边界共用静态能力声明；API 离线和 Worker 启动恢复共用分批更新及系统审计，各自的进程停止前提和记录范围不变。

验证完成：后端全量 **108 项通过**（新增 6 项，补充既有恢复审计断言），真实 API/PostgreSQL/Worker/Chromium **3 项 E2E 全部通过**。覆盖执行上下文冲突转译/失败审计/会话限制、二进制读写与读取上限、幂等引用、取消后的上传保留、历史窗口边界和应用时钟偏差、渠道目录与发送能力一致性。OpenAPI、16 张领域表及索引的 PostgreSQL DDL 与重构前快照一致，Ruff 和格式检查通过。前端产品代码、迁移及部署配置未变，不重复前端单元测试和部署备份矩阵。

本批测试使用独立本机 PostgreSQL、模拟渠道和本机协议服务器；随机测试库已清理，临时 PostgreSQL 已关闭，keystone-live 容器保持停止。未配置真实密钥、访问供应商或重建镜像。源码变化仍遵守 DBOS 执行版本与停写发布规则。

### 4.17 模型调用与权限读取收敛

2026-10-19：按复查的五项建议整理，参考 Ether ai/runtime.py 的受控客户端与异常包装、user/rbac/engine.py 的批量权限读取及 core/models.py 的通用时间归属。保留现有模块目录、领域表和公开接口。

- 统一模型异常链分类，安全错误码与调用状态使用同一结果，修正 Agent HTTP 402/429 被误记为 unknown 的问题。
- OpenRouter 费用补查的路径、参数及回执解析回到 providers.py；查询服务只做授权、配置复查与事实保存。
- Agent 改用 invoke_sdk 内部入口，不再借用公开 Invoke 构造占位消息；仍复用同一发送核心、稳定 ID 和防重发逻辑。
- 当前身份与管理目标共用一次联表角色权限读取，保留各自的账户/PAT 限制，不引入缓存。
- utcnow 移到 core/time.py，所有领域及测试更新引用，数据库时间用途不变。

验证完成：后端全量 **116 项通过**（本批新增 8 项），真实 API/PostgreSQL/Worker/Chromium **3 项 E2E 全部通过**。新增覆盖 SDK 与 Agent 异常分类一致、包装后的超时/取消、安全消息，以及真实 DBOS Worker 经本机 HTTP 收到 402/429 后的错误码、failed 状态和只发送一次。角色权限测试验证一次查询、空角色、未知权限过滤、管理员展开、PAT 范围、强制改密及停用目标的管理范围；费用补查、配置复查、流式取消和防重发沿用既有回归。OpenAPI、16 张领域表及索引 DDL 与重构前快照一致，Ruff 和格式检查通过。

文档索引、整体架构和 Identity/Model Access/Flow 文档已同步。没有新增依赖、迁移或框架层；前端产品及部署配置未变，不重复前端单元测试和部署备份矩阵。随机测试库已清理，临时 PostgreSQL 已关闭，keystone-live 容器仍停止。本批使用模拟渠道与本机协议服务器，未配置真实密钥、访问真实供应商或重建镜像。源码更新仍遵守 DBOS 执行版本与停写发布规则。

### 4.18 Flow 安全错误与列表批量查询

2026-10-19：参考 Ether flow/errors.py 的显式安全错误目录及 user/rbac/engine.py 的批量权限读取，补齐两处小范围问题，保持现有模块结构和领域模型。

- Flow 白名单补齐模型不存在、请求目标改变、输入/响应过大、流未完成、中断、非法请求和上游模型不存在这 8 个代码。只允许完整代码匹配，未知代码及原始异常消息仍投影为 flow_execution_failed。
- 用户、角色列表完成分页后，一次读取当前页的角色分配或权限；空页跳过关联查询。保留搜索与排序、空角色、内置管理员展开和未知权限过滤，不增加缓存或通用查询层。

验证完成：后端全量 **132 项通过**（新增 16 项），真实 API/PostgreSQL/Worker/Chromium **3 项 E2E 全部通过**。新增测试覆盖安全码投影、异常消息隔离、真实 DBOS Worker 经本机 HTTP 收到 400/404 后的错误一致性与只发送一次，以及用户/角色分页、搜索、空页和关联查询次数/当前页范围。OpenAPI、16 张业务表及索引 DDL 与重构前快照一致，Ruff 和格式检查通过。

同步整体架构、Identity/Flow 领域文档及文档索引；没有新增依赖、迁移、领域字段或框架层。前端产品及部署配置未变，不重复前端单元测试和部署备份矩阵。随机测试库已清理，临时 PostgreSQL 已关闭，keystone-live 容器保持停止，未重建镜像。测试仅用模拟渠道和本机协议服务器，未配置真实密钥或向真实供应商发送请求。源码更新仍遵守 DBOS 执行版本与停写发布规则。

### 4.19 三层环境变量配置

2026-10-19：后端、前端开发工具和部署编排分别提供 `backend/.env.example`、`frontend/admin/.env.example`、`deploy/.env.example`，统一按用途分组并注明读取方式。前端使用 Vite loadEnv 支持 `.env`、本地及模式文件，保留 KEYSTONE_API_PROXY 与进程环境优先级；浏览器继续请求同源 `/api`，生产 Nginx 上游仍为 `api:8000`。

三份示例完整列出保留配置及简短注释：后端覆盖全部 14 个 Settings 字段，其中健康文件默认关闭，以注释提供路径，其余 13 项直接赋值；前端 1 项，部署覆盖全部 17 个 Compose 插值输入。容器内固定的监听、存储和健康文件路径只由 Compose 声明，不伪装成部署环境文件的可覆盖项。API、Worker 与三个一次性服务显式接收后端参数。职责和覆盖范围见[配置说明](configuration.md)，README、文档索引及部署手册已同步。

进一步从 Settings、Compose、Dockerfile、测试启动器及当前配置文档中删除就绪超时、Worker 轮询间隔和迁移目录三个可调参数。就绪检查固定 3 秒，维护和 Flow 轮询固定 5 秒；源码按代码位置定位迁移，安装后的镜像从 `/app` 工作目录读取随镜像交付的 `migrations/`。测试也使用正式轮询间隔，不保留环境变量加速入口。

验证：最终后端非数据库 **54 项**、PostgreSQL/真实 Worker **89 项**通过，共 **143 项**，覆盖三处崩溃恢复、取消、单实例及有序停止；新增 3 项迁移定位用例，覆盖源码、容器安装布局和文件缺失。数据库回归使用固定源码快照，避免并行修改改变 DBOS 执行版本；快照与最终工作区源码一致。另用缓存后端镜像的临时容器验证实际 site-packages 布局下的迁移定位。Ruff、格式、配置消费与 Compose 传递、四项保留调参覆盖、三项镜像覆盖、文档链接及 Git 忽略规则通过。

本阶段此前的前端 **42 项**、TypeScript/生产构建和 10 项 Vite 读取/真实 HTTP 同源代理检查已通过；本轮未改前端产品代码，不重复前端和浏览器 E2E。临时数据库容器已删除，未修改实际 `.env` 或 `.env.live`，未重建镜像或启动现有部署；本轮不重复备份恢复矩阵。

随后补齐保留配置的示例赋值与简短注释，不再只在文档中列可选项。专项复核通过：后端 14 字段与示例完全对应，健康文件默认关闭且取消注释后可正确解析；部署 17 个输入与示例完全对应，五个后端服务的读取、默认值与参数覆盖一致；前端示例通过实际 Vite 配置加载，进程覆盖及同源 `/api` 路径保持不变。同步文档并检查链接、已删除参数无回流及 Git 忽略规则。本次仅修改示例和说明，不重复业务回归，不修改实际环境文件或运行部署。

### 4.20 Worker 最小健康检查

2026-10-19：参考 Ether Worker 的就绪文件和退出清理方式，新增 `keystone-worker --check` 只读探针及 Compose healthcheck。保持单 Worker、本地文件、现有固定 5 秒轮询；不增加心跳表、监控服务、依赖或领域字段。

启动清除旧标记，依赖检查通过且维护尝试完成后更新；启用 Flow 时还须 DBOS 队列初始化完成、Flow 任务未退出。依赖异常或维护循环失败时清除，恢复后重写；退出在排空前清除，强制退出或阻塞留下的文件超过 30 秒即失效。探针不创建 Runtime、不访问数据库或模型；未配置路径返回失败。本机可设置 KEYSTONE_WORKER_READY_FILE，Compose 固定容器内 `/tmp/keystone-worker.ready`。健康不代表每个 Flow 或文件清理成功，API ready 和后台概览含义不变。

验证：后端全量 **143 项通过**。新增 6 项用例覆盖缺失/过期/未来时间、探针退出码与不启动 Runtime、旧标记清理、依赖和维护失败后恢复、等待 Flow 初始化及执行器退出、标记无法写入时关闭资源；既有真实 DBOS Worker 测试补充健康标记有效、第二实例拒绝及正常退出移除断言。Ruff、格式检查、Compose 配置静态解析通过；OpenAPI、16 张业务表及索引 DDL 与重构前一致。

部署验收脚本新增探针调用，本轮按要求未运行容器矩阵、构建镜像或启动容器，不声称新 healthcheck 已通过容器实测。前端无修改，未重复浏览器回归。测试使用本机隔离 PostgreSQL 与模拟渠道，随机测试库已清理、临时 PostgreSQL 已关闭，未访问真实供应商。运行配置和现有镜像未修改；源码更新仍遵守 DBOS 执行版本与停写发布规则。使用方法与检查范围见[部署手册](deployment.md#12-worker-最小健康检查)。

### 4.21 业务权限、审计动作与模型接入

2026-10-19：参考 Ether 的业务权限声明、应用目录隔离和调用方事务审计，补齐业务扩展接入点。保持同一后端制品、六个平台领域、16 张表和单条 Alembic 链，不新增具体业务、依赖、迁移、插件框架或权限/审计目录表。

- core.Catalog 复制并冻结权限及动作字段集合，composition.py 显式合并平台与业务声明。启动拒绝命名冲突、平台前缀占用和非法声明；流程要求的权限须已声明。业务目录为空，现有 document_review 示例不新增实体或权限。
- Runtime 创建目录并绑定到自己的 Database session factory；角色目录与批量列表、管理员展开、可授予范围、PAT 和 Worker 身份授权共用目录。不同应用的权限/审计目录互不覆盖，普通角色和已有 PAT 不自动获得新增权限。
- 业务动作仍写 AuditLog，沿用白名单、大小限制、实际身份与 correlation_id。业务修改和成功审计同事务；失败与拒绝在回滚后独立留痕，不为业务新增上下文或审计服务。
- model_registry.load_models() 是应用与 Alembic 的共同显式 ORM 加载入口，迁移不导入 API 或执行代码。接入测试发现同名数据库角色的默认搜索路径会导致 Alembic 错报表和外键差异，迁移连接固定 search_path=public，领域表仍显式位于 keystone。

验证：新增 16 项接入测试通过，覆盖非法声明、目录不可变、角色/PAT/Worker 授权与撤权、两个应用目录隔离、业务成功/失败/拒绝审计、事务回滚与安全详情，以及测试实体进入 Alembic 元数据、建表后差异归零。后端全量 **159 项通过**，包括现有 PostgreSQL、模拟渠道及真实 DBOS Worker 回归。OpenAPI、16 张表及索引 DDL 与既有快照一致；Ruff 和格式检查通过。

同步整体架构的业务接入规范、Identity/Audit 领域文档、迁移说明、README 与文档索引。业务示例仅出现在说明与测试中，未新增生产业务。按要求不启动容器、不重建镜像、不配置真实密钥或调用真实供应商；本机 PostgreSQL 仅用于随机隔离测试库；测试库已全部清理，临时实例已关闭，keystone-live 容器仍全部停止。

### 4.22 业务扩展接入收敛

2026-10-19：按接入点复查补齐四项，参考 Ether 的实例级流程目录、重复定义拒绝和模型归属检查，保持原有模块和单条迁移链，不新增具体业务。

- Flow 定义从全局覆盖式注册改为 Runtime 持有的不可变映射。显式列表统一验证重复键和权限声明，API、执行身份与 Worker 同步共用当前应用目录；执行代码仍由 Worker 单独导入。
- AccessContext 在取得授权上下文后保留 PAT 编号，失败和拒绝审计补传 credential_id；余额刷新独立异常路径同步补齐。未认证凭据仍不推测身份，不新增审计字段。
- 权限资源名拒绝空点分段，审计字段集合拒绝字符串，配置错误立即失败。
- 新增独立进程的业务模型检查，先捕获显式注册再扫描，检测漏注册、漏导出、表归属及反向依赖；测试用临时假模型证明规则生效，生产不动态发现模型。

专项验证 30 项通过，覆盖两个应用的目录展示、发起、执行授权与补投隔离，全部定义校验、PAT 成功/失败/拒绝及未认证审计，以及模型正反例。既有余额刷新并发/配置变更测试补充 PAT 编号断言。后端全量回归 182 项通过，1 项既有文件取消测试因工作区存储更新增加预检而过早触发取消；工作区已将注入点限定为实际 write 后，该项单独复测通过，共验证 183 项。Ruff 和格式检查通过。

同步整体架构、Identity/Audit/Flow 文档、迁移说明及文档索引。本批未修改生产实体、迁移或接口字段，不新增依赖；不启动容器、不重建镜像、不调用真实渠道。数据库验证仅使用本机临时 PostgreSQL 随机测试库；随机库及客户端连接均已清空，临时实例已关闭，现有 keystone-live 容器全部保持停止。

### 4.23 文件多存储源

2026-10-19：参考 Ether 的 StorageConfig、OpenDAL filesystem/S3 适配和位置指纹，扩展为可同时配置多个具名存储源。每个 File 仍对应一个源中的一份不可变内容，业务维护本地/云端两个 file_id、引用、备份状态及重试；保持 File、FileGrant、FileReference 三表，不增加 FileReplica、源管理表或平台复制任务。

- 使用 KEYSTONE_FILE_STORAGES 显式声明全部内容源、KEYSTONE_FILE_DEFAULT_STORAGE 选择上传默认源、KEYSTONE_FILE_COORDINATION_ROOT 独立指定共享本地协调目录；后端、Compose 和两层示例同步。上传支持 storage_key，options 返回可选源键，后台提供选择及源键展示，Flow host 上传可指定产物源。
- storage_key 包含在 0004_files 初始结构中，由上传服务明确填写，ORM 和数据库没有默认值；共六份领域迁移，head=0006_flow。配置目标改变或源被移除时拒绝访问，默认源变化不改已有文件路由。
- 本地协调锁从适配器拆到 LocalLeases；S3 仍限同宿主共享本地协调卷。同请求重试仅写相同校验内容；远端迟到写入由 failed/deleted 记录持续复核，deleted 不恢复状态和授权。辅助源故障不阻断默认本地源，数据库可用时逐源清理继续运行。
- 新增真实 OpenDAL + 本机 S3 HTTP 协议夹具测试，覆盖显式源与协调目录校验、未配置时不创建内容源、独立目录初始化、读写删除、源身份及密钥轮换、失败写入重试、迟到写入清理、路由固定和初始结构。复核已删除对象时先检查存在，避免不断制造 S3 删除标记。前端测试覆盖源选择和失败后复用请求键；修正 Flow 取消测试，使注入仅发生在实际 write 后。

验证：统一显式配置后，冻结验收源码运行 203 项后端回归全部通过（187.32 秒），包含本地/S3 协议夹具、未配置源时的行为、协调目录必填与独立初始化、初始结构/ORM 一致性、文件授权引用及真实 DBOS Worker。更新启动配置后的真实浏览器 3 项通过；Ruff 检查通过。多源前端此前的 43 项测试和 TypeScript/Vite 构建通过，本次未修改前端产品代码。Compose 验证显式源 JSON 与独立协调目录传入全部五个后端服务。临时测试数据库容器在验收后移除，未修改既有部署或实际凭据。真实对象存储账户尚未联调；不代表桶版本、multipart 生命周期或多宿主协调已验收。详细配置与远端最终清理边界见[配置文档](configuration.md#4-同时配置本地与-s3)和[文件领域](domains/files.md#71-存储和上传)。

### 4.24 Flow 接入声明与装配校验

2026-10-19：参考 Ether 在目录构建时检查执行入口和能力字段的做法，补齐业务接入最后三项校验，继续使用显式装配，不新增业务、实体、迁移或依赖。

- build_definitions 拒绝空白或重复执行入口、缺失的模型字段，以及输入输出中不存在或类型不符的文件路径。保留对象点路径与末端 ID 列表，可空值不产生引用，不引入通配符或对象数组遍历。
- document_review 工作流装饰器直接引用 definition.entrypoint；独立子进程测试观察真实 DBOS 装饰器，覆盖全部显式定义，并验证漏注册能够被发现。测试不连接数据库、不启动执行器，不依赖 DBOS 私有注册表。
- 模型加载隔离测试改为检查平台表集合完整，允许额外业务表；以子进程内临时实体覆盖扩展情况，沿用已有业务实体注册、导出和分层检查。

验证：Flow 声明、业务扩展、业务模型、模块分层、基础装配及 Worker 健康检查共 64 项通过，5 项 PostgreSQL 集成测试按本批范围未运行；新增装配测试只导入执行模块，不启动 Worker。后端 Ruff 全量检查和本次修改文件格式检查通过。本批未启动数据库或容器，未发起真实渠道请求；未重跑全量数据库/DBOS 执行回归。

### 4.25 Flow 输出契约与输入文件错误

2026-10-19：参考 Ether 的序列化输出 Schema 和输入文件错误转换，修复业务扩展中的两处局部不一致，不新增业务、字段、迁移或依赖。

- Definition.output_schema() 统一使用序列化模式及原字段名，启动校验、目录展示与运行快照共用；示例工作流显式按 by_alias=False 输出，受理后的输入也使用原字段名。已有快照不修改。
- 输入文件 ID 在创建 Run 和解析模型选择前提取，非法 UUID、超过 100 个不同文件 ID 均返回已有的 422 flow_invalid_input；继续复用 Files 授权和失败审计。
- 回归覆盖字段序列化器、输入/输出别名、快照与目录一致性、合法结果成功同步和错误结果被拒绝，以及真实 API 的 422、无 Run/引用残留、失败审计与原请求键可重新使用。

验证：相关回归最终一轮 74 项通过（17.73 秒），包含真实 PostgreSQL API、失败审计、结果同步、业务扩展、模型与分层、基础装配及 Worker 健康检查；Ruff 全量检查、修改文件格式与文档文件链接检查通过。首次测试的本机端口不匹配及新增 FileReference 断言错误已修正后重跑。随机测试库已清理，本机临时 PostgreSQL 已停止；未启动容器、DBOS Worker 或真实渠道调用，未重跑全量执行恢复测试。

### 4.26 首版兼容残留清理

2026-10-19：复查整个后端、管理前端、迁移、部署脚本及维护文档，清理首版开发中保留的默认目录路径和骨架字段，不新增迁移、依赖或领域实体。

- Database 必须显式传入 Catalog；权限与审计直接读取当前会话绑定的目录。遗漏目录时立即失败，显式空目录不继承平台权限或动作。初始化夹具、浏览器脚本与独立脚本统一使用 build_catalog；PERMISSIONS/ACTIONS 只作为平台装配声明。
- 就绪接口移除固定的开发阶段 stage 和全为 true 的平台模块 capabilities；前端移除相应必填校验，并校验页面实际使用的 storage 状态。依赖检查、各存储源状态、Flow 启用配置及模型模式继续返回。
- README 合并过期测试基线，部署文档改为从当前示例、源码镜像和空库空卷准备本地 live 环境，清除早期镜像探针/密码规则补丁说明及本机状态、端口、凭据文件记录。架构、身份、审计、迁移说明和设计索引同步当前约定。

Windows/psycopg 适配、源码与容器安装布局定位、当前供应商协议差异，以及 Flow 检查点、执行版本和持久契约保留，分别承担现有运行、协议和恢复职责。

验证：最终冻结源码后端全量 **211 项通过**（190.03 秒），包含目录遗漏/空目录拒绝继承平台权限与动作、应用隔离、就绪契约、本地/S3 协议夹具和真实 DBOS 执行恢复。前端 **47 项**、TypeScript 检查与生产构建通过；最终真实 API/PostgreSQL/Worker/Chromium **3 项**通过（27.9 秒）。Ruff、格式及维护文档的本地文件链接/章节锚点检查通过。临时测试数据库容器已移除；未修改现有部署、凭据或调用真实供应商。 当时快照未包含并行的 Flow 最终输入大小修改；包含两路最终代码的联合验收见[4.28](#428-清理与-flow-最终代码联合回归)。

### 4.27 Flow 最终输入大小检查

2026-10-19：保留统一 64 KiB 限制，在默认值填充、字段序列化和模型选择解析完成后、创建 FlowRun 前，复用 small_json 检查最终 inputs。生产逻辑仅增加一次检查，超限沿用 422 flow_value_too_large；原始请求检查、幂等摘要和失败审计机制不变。不新增业务、配置、字段或迁移。

回归覆盖默认值、序列化器和真实模型选择解析三种输入扩张，使用多字节文本验证恰好 65536 字节受理、超出 1 字节拒绝；同时检查失败审计、无 Run/引用残留、修正后复用请求键和成功请求重复受理。

验证：业务接入与 Flow 契约相关测试 52 项全部通过（15.68 秒），包含真实 PostgreSQL API 和上述 6 项输入大小边界用例。Ruff 全量检查、修改文件格式与文档文件链接检查通过。随机测试库已清理，本机临时 PostgreSQL 已停止；未启动容器或 DBOS Worker，未发起真实渠道调用，未重跑全量执行恢复测试。 包含真实 Worker 执行恢复及首版清理的最终联合验收见[4.28](#428-清理与-flow-最终代码联合回归)。

### 4.28 清理与 Flow 最终代码联合回归

2026-10-19：确认另一路 Flow 修改及其复查已完成后，对包含文件多源、显式应用目录、就绪接口清理、Flow 契约/文件输入修复和最终输入大小检查的代码进行统一验收。本轮仅更新验证记录，无产品代码、依赖或迁移变更。

后端、前端、浏览器脚本、迁移和部署配置共 242 个文件冻结到同一验收副本；前端复用已安装依赖，测试代码和配置均来自副本。验收结束逐一核对 SHA-256 及文件集合，与工作区完全一致，没有未覆盖的并行源码改动。

| 检查 | 最终结果 |
|---|---|
| 后端全量 pytest | **217 项通过**，206.18 秒，无跳过或失败 |
| 前端 Vitest | **47 项通过** |
| 真实 API/PostgreSQL/DBOS Worker/Chromium | **3 项通过**，21.7 秒 |
| TypeScript 与 Vite 生产构建 | 通过 |
| Python 源码包与 wheel 离线构建 | 通过 |
| Ruff 与格式检查 | 通过，含后端、测试及浏览器/部署脚本 |
| Compose 配置解析 | 通过，使用当前示例配置 |
| 维护文档本地文件链接与章节锚点 | 通过 |

后端覆盖新增的 6 项最终输入 UTF-8 大小边界，以及缺少/空目录时拒绝继承平台权限和动作、应用隔离、审计事务、六领域初始迁移与回滚、本地/S3 协议、Flow 输出契约、非法文件输入、取消、崩溃恢复和防重复发送。浏览器覆盖身份管理、文件/字典及三家模拟渠道与真实 Flow。

本机产物保存在 `.artifacts/combined-final-regression-ff91d1e171/`：`source-manifest.json`、`verification-result.json`、后端/前端/浏览器日志及 Python 构建产物。使用 PostgreSQL 17.11 专用测试实例与随机数据库，结束时确认随机测试库为 0 并停止实例；API、Worker、Vite 和浏览器进程已随脚本退出。本轮未启动部署容器、修改现有部署/凭据或访问真实供应商。

## 5. 验证与复用测试

| 验证类型 | 优先来源与检查 |
|---|---|
| 身份与审计 | [认证测试](M:/code/ether-workspace/ether/tests/user/test_auth.py)、[凭据测试](M:/code/ether-workspace/ether/tests/user/test_credentials.py)、[管理员保护](M:/code/ether-workspace/ether/tests/user/test_admin_protection.py)、[审计测试](M:/code/ether-workspace/ether/tests/user/test_audit.py)；改写为 Cookie/PAT 与当前撤销规则 |
| 文件与存储 | [文件测试](M:/code/ether-workspace/ether/tests/files/test_files.py)、[保留测试](M:/code/ether-workspace/ether/tests/files/test_retention.py)、[存储契约](M:/code/ether-workspace/ether/tests/storage/test_contracts.py)；重点覆盖重试旧写入、引用竞争和删除确认 |
| 模型与 Flow | [模型运行时测试](M:/code/ether-workspace/ether/tests/ai/test_runtime.py)、[调用记录测试](M:/code/ether-workspace/ether/tests/ai/test_invocations.py)、[恢复测试](M:/code/ether-workspace/ether/tests/flow/test_recovery.py)、[授权测试](M:/code/ether-workspace/ether/tests/flow/test_authorization.py)、[Worker 测试](M:/code/ether-workspace/ether/tests/flow/test_worker.py)；迁移适用场景，新增稳定请求 ID 和未知结果防重发测试 |
| 前端与部署 | 参考 Ether admin 的组件与浏览器用例、[部署验收](M:/code/ether-workspace/ether/tests/deploy/verify.py)；按一个 API/Worker 与最少依赖改写 |

快速测试不依赖真实渠道；数据库事务与恢复测试使用真实 PostgreSQL 和 Worker 进程。模拟传输必须覆盖 SDK 实际发送路径，不能只模拟 Agent 最外层返回值。真实付费调用不进入默认 CI，单独配置测试账户与有界预算。存储契约同时覆盖文件系统和本机 S3 HTTP 协议夹具，使用真实 OpenDAL 客户端；真实对象存储账户另行验收。[OpenDAL Python](https://opendal.apache.org/docs/python/) 是存储适配依据。

模型与 Flow 阶段至少区分三类恢复：已有检查点直接复用；已登记且发送结果未知时拒绝重发；已成功记录费用但响应检查点缺失时仍拒绝重发。普通业务写入另按稳定操作键去重，不能把模型发送保护误认为覆盖所有工具副作用。优先迁移 Ether 已有场景，只针对裁剪、接口适配和 Keystone 新规则增加检查，不重复建立通用技术验证项目。

## 6. 当前结论与下一项工作

真实账户联调的[准备清单与最小验收](domains/model-access.md#79-真实账户联调准备与最小验收)已整理到模型领域文档：独立环境、专用限额 Key 和 Management Key；先普通/流式各一次，核对费用补查与余额，通过后再以独立预算验收一次 Flow。当前仅完成文档，不配置真实密钥、不修改运行配置、不发起真实账户请求；该清单不是验收通过记录。

本地联调从当前示例配置、源码镜像和空库空卷初始化，步骤见[本地环境准备](deployment.md#11-本地-live-联调准备环境)。真实账户最小验收仍待执行。

阶段 6 已完成三批后台整理及单宿主部署、冷启动和联合备份恢复验收，补齐了阶段 1 的容器验证。OpenRouter live 基础能力已实现，下一步在独立环境配置账户并另行授权有界联调，核对真实回执、流式结束、费用补查和 Management Key 余额；本批不发起付费调用。也可开始首个具体业务的结果采用与业务前端，DeepSeek/阿里云按实际需求后续接入。部署到目标环境时配置 TLS 和异地加密备份，暂不增加新的底座领域或重新开展 DBOS/Agent 选型。

当前已完成六领域最小实现、三批后台整理、单宿主部署/恢复验收，以及 OpenRouter live 的本地协议验证，设计统一维护在 keystone/docs/。Git 已初始化，当前依赖已安装并锁定；未访问 Ether 数据或调用真实模型。沿用 Ether 已有验证作为基础，不表示 Keystone 尚未编写的适配已通过测试；真实账户与目标部署环境仍单独验收。
