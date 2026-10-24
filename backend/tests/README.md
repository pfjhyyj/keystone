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
- 合并相同装配和数据构造，保留每个独立保证的断言。共享辅助函数放 support，unit 不导入 integration。
- `support.flow_packages` 提供轻量快照、install_package 和关联断言。安装后立即清理制品缓存，测试间由全局夹具再次清理；契约模块导入不加载执行器。
- `test_flow.worker()` 启动真实执行器。fast_health 只缩短依赖/心跳观察周期为 0.2 秒；DBOS 队列、持久计划、身份锁、恢复期限及关闭预算不变。验证默认周期使用 fast_health=False，自定义无桩启动保持默认。
- 并发使用事件、门闩和明确状态，不依赖短 sleep 的偶然顺序。反复取消须等待原生 I/O/进程/文件清理。
- 领域终态、调用收尾与 DBOS 检查点完成是独立事实；需要 Workflow 完成时等待 SUCCESS，取消中的步骤历史可以暂缺，保留真实发送位置。
- `test_model_flow_migrations.py` 使用原始隔离库验证含数据的旧 revision，不导入自动升级 head 的应用夹具。历史迁移不重写，回滚只发生在测试库。
- 架构检查递归覆盖平台/业务、相对导入、嵌套任务与例外绕过；脚手架输出遵守相同规范。
