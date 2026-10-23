# 后端测试

测试按依赖边界分三层，避免在单元测试里启动数据库或后台执行器：

| 目录 | 用途 | 运行条件 |
|---|---|---|
| `unit/` | 配置、HTTP 契约、权限/审计、Flow 声明、迁移、架构、磁盘与进程边界 | 无需数据库 |
| `integration/` | 身份、文件、模型、Flow、Worker、计费、媒体和迁移的真实 PostgreSQL 场景 | 需要专用 `keystone_test` 数据库 |
| `support/` | 共享夹具：模型桩、S3 协议桩、本地媒体桩、测试 Flow 业务、AST 导入图 | 不作为测试用例收集 |

`support/flow_fixture/` 是测试专用 Flow 装配，复用 `keystone.examples.document_review` 的示例代码；`support/local_media_stub.py` 提供本地媒体环境。它们不属于生产包，也不进入迁移链。

`integration/test_flow.py` 的 `worker()` 启动真实执行器。普通模型桩场景将依赖/身份存活检查和心跳观察周期缩为 0.2 秒，减少场景无关的周期等待；DBOS 队列轮询、持久计划的 cron、身份锁超时、恢复期限和关闭预算保持原值。验证默认周期时传 `fast_health=False`；单实例有序停止、多 Worker 空闲健康测试使用默认周期，自定义 `stub=False` 启动也保持默认值。慢清理使用显式文件门闩，核对跨调度周期的合并、隔离和不健康状态后再放行，不靠固定长 sleep 构造竞争窗口。

同配置的成功纠正/预算耗尽使用一个 Worker，但保留独立 Run、调用计数、Agent 身份和产物断言。部署冲突在一个子进程内分别调用真实 `main()`，每项新建并关闭运行时，逐项确认失败退出、计划不变且没有加入成员。崩溃恢复仍为真实进程退出与重新启动：发送前登记、响应后、产物后，以及请求数、工具次数、token、输出重试和期限的检查点覆盖均保留。

模型错误码、官方地址/路径/方法白名单和业务输出校验的输入矩阵在 `unit/` 验证；重复无效输出使用真实 Agent 验证纠正次数、安全错误码和不发布报告，无需数据库或 Worker。集成测试保留发送前拒绝且不创建调用记录、真实 Worker 成功/429 拒绝、纠正成功与预算耗尽的落库；`integration/test_review_recovery.py` 继续验证崩溃后输出重试和各项预算不会重置。

普通规则与数据库行为分别覆盖，扩充输入组合时优先放入对应单元测试：

| 规则或边界 | 无数据库覆盖 | 保留的集成保证 |
|---|---|---|
| 执行权限声明、进程安全错误码、取消清理 | `unit/test_execution_rules.py` | 当前身份、撤权、取消及版本在执行边界重新查询 |
| artifact key 与 run ID 规范化 | `unit/test_artifact_keys.py` | 当前文件授权、完整性、引用唯一性及事务内采用 |
| 本地文件锁与原生 I/O 取消 | `unit/test_file_leases.py`（保留真实子进程） | 读写、删除与维护任务在真实事务和文件锁间协调 |
| Flow 默认值、序列化扩张、文件 ID 与数量 | `unit/test_flow_input_validation.py`（调用真实 Service/Schema） | 模型选择扩张后的 64 KiB 边界、422 失败审计、无 Run/引用残留及请求键可重用 |
| 文件引用释放参数 | `unit/test_file_reference_validation.py` | 无效批次不释放已有引用、缺失文件与业务/审计失败整体回滚、并发锁顺序 |
| 费用查询网络错误、quota、policy、元数据、深度及模型名 | `unit/test_billing_validation.py`（真实 HTTP 解析和适配器） | 可重试/阻断状态安全落库、费用与检查点原子提交、共享锁、恢复及不重复推理 |

`unit/test_architecture.py` 通过 `support/module_checks.py` 递归检查平台和业务模块：根目录只允许包说明与权限/审计声明，一级职责目录固定，MVC + Service、协议适配和 DBOS 执行边界不可反向依赖。负例覆盖相对导入和嵌套执行文件；轻量 Flow 定义及模块声明另用独立进程验证不会加载 ORM/执行器。脚手架生成结果接受同一结构检查，`unit/test_catalog.py` 检查声明合并时拒绝覆盖权限或审计动作。

跨模块归属检查拒绝其他模块 Model/Adapter 导入、平台反向导入具体业务和 core 反向导入领域/组装代码；负例覆盖包别名、相对导入和嵌套文件。DBOSClient 仅在 platform/flow/adapters/dbos.py 放行；同名业务 Adapter 和其他 Flow Adapter 均拒绝。

导入例外逐条判断，负例还覆盖任务包入口、近似 definitions 名称、同文件混入合法契约、core ORM/HTTP 包装及模块反向依赖装配层。Flow Service 用独立进程验证无需导入应用装配或执行器，版本查询注入另验证应用隔离及每次授权的实时复查。

`unit/test_file_settlement.py` 验证重复取消必须等待已知的上传失败事务；`integration/test_disk_capacity_api.py` 通过延迟落库稳定触发磁盘监控取消，检查 failed 状态、原文件 ID 重试及容量释放。

integration/test_file_boundaries.py 验证空权限角色的文件授权、PAT 范围、角色撤销、停用主体及缺失主体语义和授权审计回滚，并通过公开传输接口验证独立于 Flow 的落盘、引用上传幂等及作用域资源释放。现有媒体、取消与恢复测试继续使用相同的 host 接口。

`unit/test_migrations.py` 使用 Alembic 原生发现检查文件平铺、模块前缀、全局四位序号与单一依赖链一致，并验证不同工作目录和安装后部署。`unit/test_new_business.py` 验证跨业务连续编号、类型注解元数据、表名长度，以及生成前拒绝重复初始建表、重复编号/revision、断链、合并及错误路径；`integration/test_migration_layout.py` 验证从每个基线 revision 升级、降级后由独立脚本目录继续升级、数据及 DBOS 表保留，以及生成迁移的重复升级和回滚。

## 运行

数据库测试先在仓库根目录启动独立实例；只跑 unit 时无需此步骤：

```bash
docker compose -f tests/postgres/compose.yaml up -d --wait
export KEYSTONE_TEST_DATABASE_URL='postgresql+psycopg://keystone_test:isolated-test-only@127.0.0.1:55433/keystone_test'
```

```bash
cd backend

# 日常快速检查：不连接数据库
.venv/bin/python -m pytest tests/unit

# 集成测试：使用专用测试库，不能指向开发库
.venv/bin/python -m pytest tests/integration --require-postgres

# 全部测试
.venv/bin/python -m pytest --require-postgres
```

仓库统一入口仍为根目录的 `backend/.venv/bin/python scripts/check.py`；只跑非数据库检查时使用 `--unit`。

数据库不可用时按入口区分：`pytest --require-postgres` 和 `scripts/check.py` 全量会在启动阶段直接报错；默认 `pytest` 会统一跳过 `postgres` 用例，unit 照常运行。`scripts/check.py --unit` 和 `pytest tests/unit` 不探测数据库。

`scripts/check.py` 的后端测试默认使用最多 8 个 `pytest-xdist` worker 并行执行。全量模式使用 `--dist load --maxschedchunk=1` 小批分配；xdist 仍会给每个执行槽保留至少两个用例以维持流水，避免把整组长 Worker/恢复测试预留给同一个执行槽。`--unit` 保持默认批次；设置 `KEYSTONE_TEST_WORKERS=1` 可串行运行，便于排查并发相关失败。

随机数据库隔离用例数据，但共享 PostgreSQL 实例的连接配额。`tests/postgres/compose.yaml` 专供本地与 CI 测试，默认端口 55433、`max_connections=512`，与开发实例分开。容量预检按每个 pytest worker 48 个连接（API、DBOSClient、两个真实 Worker 及临时连接）加 16 个保留连接计算；默认 8 路要求至少 400 个普通连接，扣除 PostgreSQL 的保留槽位。容量不足直接报错，不降并发、不跳过用例；unit 检查不探测数据库。额外部署/恢复栈仍使用 `tests/deploy/compose.yaml`。

## 组织约定

- 新测试先判断是否需要数据库：不需要放 `unit/`，需要放 `integration/` 并使用现有 `client`、`empty_database_url` 夹具。
- 跨测试的桩和辅助函数放 `support/`；不要让 `unit/` 依赖 `integration/` 的模块。
- 同一校验函数的不同输入用一次测试循环覆盖，不重复创建应用、数据库或子进程。
- 领域状态提交与 DBOS Workflow 完成是不同事实；断言 Workflow 成功次数前，须等待对应的 `SUCCESS` 状态，不能只以领域终态作为完成屏障。
- Flow/Worker 的子进程测试通过 `support.*` 导入夹具；E2E 和部署验证也使用同一份 support 目录。
- 迁移测试使用随机隔离库；回滚测试只发生在测试库。
