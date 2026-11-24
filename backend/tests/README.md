# 后端测试

| 目录 | 边界 |
|---|---|
| unit/ | 无数据库的契约、规则矩阵、架构检查及本地进程/文件语义 |
| integration/ | 真实 PostgreSQL、DBOS、授权/事务、并发、取消、迁移与恢复 |
| support/ | 协议桩、测试业务、共享装配、断言和 AST 检查；不作为测试收集 |

模型、云账务、S3 和媒体协议使用本地确定性替身，不访问付费服务或真实账户。崩溃恢复使用真实子进程退出/重启，保留实际请求数、调用/文件 ID、原身份、用量/费用及清理断言。

## 运行

完成 README 的依赖安装后，在仓库根目录启动专用测试实例并执行统一检查：

```bash
(cd frontend/console && pnpm exec playwright install chromium)
docker compose -f tests/postgres/compose.yaml up -d --wait
export KEYSTONE_TEST_DATABASE_URL='postgresql+psycopg://keystone_test:isolated-test-only@127.0.0.1:55433/keystone_test'
backend/.venv/bin/python scripts/check.py      # Windows：backend/.venv/Scripts/python.exe
```

| 选项 | 范围 |
|---|---|
| `--unit` | 部分检查，跳过数据库与浏览器；仅用于 full/backend 范围 |
| `--scope frontend` | 格式、单元测试、类型和构建，不依赖 PostgreSQL 或 uv |
| `--scope backend` | Python 格式、unit/integration 和打包 |
| `--scope browser` | 隔离 API、Worker 和 Chromium，仍连接专用测试库；可直接运行 `backend/.venv/bin/python frontend/console/e2e/run.py admin.spec.ts` 排查 |
| `--deploy` | 另验证 Dockerfile、`tests/deploy/compose.yaml` 容器栈及备份恢复；仅用于完整范围 |

后端单独运行：`cd backend && .venv/bin/python -m pytest tests/unit`，或 `tests/integration --require-postgres`。`--require-postgres` 和统一全量入口在测试库不可用时失败；普通 pytest 跳过 integration，unit 不探测数据库。每例创建并移除随机 keystone_test_* 库，不能指向开发/业务库。

统一入口最多 8 路 xdist，`KEYSTONE_TEST_WORKERS=1` 可串行排查；专用实例默认 max_connections=512，容量预检按每路 48 加 16 个保留连接计算，不足时失败，不悄悄降并发或跳过。仓库不配置 CI，提交前按改动范围手动运行；每次检查记录结果与耗时，后端另输出最慢的 20 项。浏览器测试只在失败时截图。

## 组织与断言

- 同一规则的输入矩阵放 unit；真实授权、事务、锁或失败窗口留在 integration。相同字段的纯校验不重复启动数据库/Worker，供应商报文矩阵在真实 SDK 单元测试中核对，Worker 用例只保留代表性完整链路和持久化失败窗口。
- 按领域命名测试文件（如 `test_<领域>.py`、`test_<领域>_contracts.py`），新用例优先放入已有领域文件；共享辅助函数放 support，unit 不导入 integration。合并相同装配和数据构造，保留每个独立保证的断言。
- Flow 快照与安装使用 `support.flow_packages`；需要 DBOS 执行器或历史客户端的用例请求 `support.flow_engine.flow_engine`，普通授权/事务用例不依赖它。`test_flow.worker()` 启动真实执行器，fast_health 只缩短依赖/心跳观察周期。
- 并发使用事件、门闩和明确状态，不依赖短 sleep。反复取消须等待原生 I/O/进程/文件清理。领域终态、调用收尾与 DBOS 检查点完成是独立事实。
- 迁移测试使用原始隔离库验证完整基线、约束及含数据的升级/回滚，不导入自动升级 head 的应用夹具。
- 平台测试不假定业务装配为空：下游仓库会登记业务。权限、计划和迁移 head 按 `composition`、`model_registry` 的当前声明比较，测试业务追加到已有声明而不替换。
- 架构检查（`unit/test_architecture.py`）递归覆盖平台/业务分层、相对导入和渠道 adapter 约束，脚手架输出遵守相同规范；锁、事务与身份语义不以 mock 替代数据库。

## 本地媒体专项验收

安装可执行 FFmpeg、前端依赖及 Playwright 浏览器后，在专用 keystone_test 数据库上执行：

```bash
backend/.venv/bin/python scripts/accept_local_media.py
# Windows 可选系统 Edge：backend/.venv/Scripts/python.exe scripts/accept_local_media.py --browser-channel msedge
```

脚本建立并清理随机测试库，运行独立 HTTP API，验证 16/256 MiB 流式上传下载、SHA-256、HEAD/Range、API 进程峰值内存，以及浏览器 H.264 播放和跳转。视频使用合法 free box 填充到指定大小，区分传输规模和媒体解码；FFmpeg 仅是验收工具，不是平台运行依赖。结果保存在 `.artifacts/local-media/`，不接触业务库、云端或模型。NAS 性能与客户原片另在部署环境验证。
