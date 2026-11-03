# 后端测试

| 目录 | 边界 |
|---|---|
| unit/ | 无数据库的契约、规则、架构及本地进程/文件语义 |
| integration/ | 真实 PostgreSQL、DBOS、授权/事务、并发、取消与恢复 |
| support/ | 协议桩、测试业务、共享装配、断言和 AST 检查；不作为测试收集 |

模型、S3 和媒体协议使用本地确定性替身，不访问付费服务。崩溃恢复使用真实子进程退出/重启，保留实际请求数、调用/文件 ID、原身份、用量/费用及清理断言。

## 运行

在仓库根目录启动专用测试实例：

```bash
docker compose -f tests/postgres/compose.yaml up -d --wait
export KEYSTONE_TEST_DATABASE_URL='postgresql+psycopg://keystone_test:isolated-test-only@127.0.0.1:55433/keystone_test'
backend/.venv/bin/python scripts/check.py
```

快速检查使用 `scripts/check.py --unit`；后端单独运行：

```bash
cd backend
.venv/bin/python -m pytest tests/unit
.venv/bin/python -m pytest tests/integration --require-postgres
```

`--require-postgres` 和统一全量入口在测试库不可用时失败；普通 pytest 会跳过 postgres 用例，unit 不探测数据库。测试每例创建并移除随机 keystone_test_* 库，不能指向开发/业务库。

统一入口最多 8 路 xdist，KEYSTONE_TEST_WORKERS=1 可串行排查；全量小批分配，保持长恢复用例的调度公平。专用实例默认端口 55433、max_connections=512，容量预检按每路 48 加 16 个保留连接计算；不足时失败，不悄悄降并发或跳过测试。容器部署/恢复检查另用 tests/deploy/compose.yaml。

## 组织与断言

- 同一规则的输入矩阵放 unit；真实授权、事务、锁或失败窗口留在 integration。相同字段的纯校验不重复启动数据库/Worker。
- 图片中转的供应商/编辑/响应格式矩阵在 unit 验证 SDK 报文；Worker 保留代表性完整链路和各持久化失败窗口。费用凭据冲突通过真实媒体 API/SQL 核对，异步任务的非法结果和查询上限通过真实授权/SQL/SDK 服务验证，不为这些规则重复启动 Worker。Flow 的 DBOS 初始化仅用于执行器和历史检查，发送中取消用明确屏障同步。
- 合并相同装配和数据构造，保留每个独立保证的断言。共享辅助函数放 support，unit 不导入 integration。
- `support.flow_packages` 提供轻量快照、install_package 和关联断言。安装后立即清理制品缓存，测试间由全局夹具再次清理；契约模块导入不加载执行器。
- `support.flow_engine.flow_engine` 显式初始化测试 DBOS Schema 与历史客户端；复用 Flow 配置的执行器测试须请求该夹具，普通授权/事务用例不依赖它。结构化输出的供应商报文矩阵在真实 SDK 单元测试中核对，Worker 用例覆盖代表性路由、自定义 API 地址和执行前撤销模型能力。
- `test_flow.worker()` 启动真实执行器。fast_health 只缩短依赖/心跳观察周期为 0.2 秒；DBOS 队列、持久计划、身份锁、恢复期限及关闭预算不变。验证默认周期使用 fast_health=False，自定义无桩启动保持默认。
- 并发使用事件、门闩和明确状态，不依赖短 sleep 的偶然顺序。反复取消须等待原生 I/O/进程/文件清理。
- 领域终态、调用收尾与 DBOS 检查点完成是独立事实；需要 Workflow 完成时等待 SUCCESS，取消中的步骤历史可以暂缺，保留真实发送位置。
- `test_model_flow_migrations.py` 使用原始隔离库验证模型/Flow 完整基线、默认值与约束、含数据的升级/回滚，以及不可解析的旧 revision 拒绝后数据与版本不变；不导入自动升级 head 的应用夹具。回滚只发生在测试库。
- 架构检查递归覆盖平台/业务、相对导入、嵌套任务与例外绕过；脚手架输出遵守相同规范。
- `test_operations.py` 使用真实身份、领域表、DBOS 原生客户端和 Worker，验证状态明细认证、积压口径、预览与清理、终态 Run 的 PAT 引用、未知调用、活动父任务及历史分叉保护，以及自动清理默认关闭/显式启用。`test_business_flow_hooks.py` 验证业务入口限制、原身份先行的执行守卫、依赖故障暂缓和准入后的 Runtime 绑定。
- `test_scaffold_business.py` 在临时目录生成本人资源模块及追加迁移，显式装配目录、模型和路由，在随机库初始化并检查 ORM 一致，通过真实 HTTP 验证部分更新、对象范围、PAT 交集、撤权、审计摘要与事务回滚。`test_identity_locks.py` 验证真实共享/排他锁的有界等待、取消后清理、身份变更失败不落库及重试；不以 mock 替代数据库锁语义。

`test_relay_image_contracts.py` 用真实 SDK 校验两站共用的 JSON/multipart 报文、默认值、超时、旧摘要和有界下载。`test_relay_images.py` 使用真实 PostgreSQL/DBOS Worker 验证九图编辑、加密结果恢复、unknown 不重发及原 Token 实际费用核对；全部 HTTP 为确定性协议桩，不触发付费生成。

`unit/test_cloud_billing_contracts.py` 验证官方 RPC/HMAC-SHA256 签名向量、签名目标、账务响应白名单、币种、精度、错误和分页；`integration/test_cloud_billing.py` 使用真实隔离库验证账务凭据加密、审计不含秘密、余额快照、管理权限与查询期间配置变化／撤权。全部云 HTTP 均为确定性协议桩，不查询真实账户或发起付费调用。

`unit/test_dashscope_discovery.py` 验证原生目录分页、业务空间域名、响应投影及错误边界；`integration/test_aliyun_discovery.py` 验证真实数据库中的业务空间保存、权限、配置变更／撤权后的结果丢弃，以及迁移升级与回退的数据保留。HTTP 使用确定性协议桩，不调用付费模型。

`unit/test_model_discovery.py` 覆盖火山方舟固定 GET 地址、推理 Key、ID 投影、列表大小及错误脱敏；`integration/test_model_access.py` 的火山方舟用例验证管理权限和只读查询不创建条目或调用记录。控制台 `volcengine-discovery.test.ts` 覆盖目录刷新和手填接入点保留；测试均使用 HTTP 协议桩，不调用付费模型。

## 本地媒体专项验收

安装可执行 FFmpeg、前端依赖及 Playwright 浏览器后，在专用 keystone_test 数据库上执行：

```bash
backend/.venv/bin/python scripts/accept_local_media.py
# Windows 可选系统 Edge：backend/.venv/Scripts/python.exe scripts/accept_local_media.py --browser-channel msedge
```

脚本建立并清理随机测试库，运行独立 HTTP API，验证 16/256 MiB 流式上传下载、SHA-256、HEAD/Range、API 进程峰值内存，以及浏览器 H.264 播放和跳转。视频使用合法 free box 填充到指定大小，区分传输规模和媒体解码；FFmpeg 仅是验收工具，不是平台运行依赖。结果保存在 `.artifacts/local-media/`，不接触业务库、云端或模型。NAS 性能与客户原片另在部署环境验证。
