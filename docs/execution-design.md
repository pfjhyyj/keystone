# 任务执行

API、Worker、CLI 共用[模块化单体](architecture.md)。core/execution/ 管理公共 DBOS 生命周期、队列、计划和投递；领域 Service 管理授权、状态和事务；tasks/、workflows/ 只负责执行适配。

## 1. 选择执行入口

| 需求 | 入口 |
|---|---|
| 查询、配置、普通写入或手动有界操作 | 同步 Service，由 HTTP/CLI/后台复用 |
| 独立投递、定时维护或持久后台处理 | Task，领域保留待办与幂等 |
| 独立受理、查询、取消并有输入/结果契约 | FlowRun，由原生 Workflow/Step 执行 |
| 心跳、连接和进程监控 | 进程生命周期管理，不包装成持久任务 |
| 人工决定 | 业务 Service；需要后续自动化时受理新的独立 Flow |

耗时、步骤数或是否调用 AI 均不是分类标准。维护任务不创建 FlowRun；Flow 内每个 Step/工具不注册成 Task。Flow 的定义、模型角色、预算及结果契约集中在[Flow 领域](domains/flow.md)。

## 2. 声明与装配

轻量声明位于 core/execution/contracts.py，导入不注册执行器：

| 类型 | 字段 |
|---|---|
| TaskDefinition | key、entrypoint、input_model、queue、timeout_seconds=None |
| TaskBinding | definition、workflow、enabled=True、monitor_key=None |
| QueueDefinition | name、worker_concurrency、global_concurrency |
| ScheduleDefinition | name、tick_entrypoint、task_key、cron、payload、timezone、max_lateness_seconds、run_on_startup、backfill、overlap |
| ExecutionRef | workflow_id |

input_model 是禁止额外字段的 Pydantic 对象模型，无参数用 NoInput；序列化仍须为对象，最大 64 KiB JSON。名称不为空或带首尾空格，限额为正整数，时区与迟到/重叠选项在声明时校验，cron 在安装时由 DBOS 校验。

各模块 tasks/workflows.py 通过工厂创建 TaskBinding，注入窄 Service 依赖。composition 显式汇总，queue_budgets 检查任务/队列/计划名称、任务与 tick/业务入口唯一性、目标任务、计划输入、DBOS 注册名称和预算；只监听有启用任务的队列。

运行时 Service、Session、SDK、凭据、临时路径及完整 Runtime 不作为持久输入。普通 Service 不导入 DBOS，执行模块不直接查询领域表。FlowPackage 的轻量声明与实际执行模块分开装配；具体注册示例见[模块扩展](extending.md)。

## 3. 投递与合并

```python
async def enqueue_task(
    task: TaskBinding,
    payload: BaseModel | dict,
    *,
    execution_key: str,
    coalesce_key: str | None = None,
) -> ExecutionRef:
    ...
```

投递检查启用、输入、执行键及版本，使用 DBOS 原生队列。在 Workflow 中保持确定性顺序，不从 Step 拆分投递。合并时返回已有执行引用。

| 标识 | 生命周期 |
|---|---|
| execution_key | 同一次逻辑执行，完成后仍指原执行；调用方保证相同输入 |
| coalesce_key | 合并活动中的同类工作，完成后允许新一轮 |
| flow:<run_id> | 同一个业务 Run 的补投和恢复 |
| ModelInvocation.id | 原模型尝试的发送前登记；未知结果不换 ID 重发 |

只有提供 coalesce_key 时才设置 deduplication_id/duplication_policy，活动键带 execution_version。overlap=allow 使用独立执行键逐次处理；扫描类 coalesce 在结束后重新读取待办。不同业务请求不能仅按类型合并。队列去重、领域唯一约束、范围锁及外部副作用标识分别负责不同保证。

业务事务先保存可靠待办，Worker 可重复发现并投递；不能用提交后临时协程连接两个独立提交。时间触发业务 Flow 仍须通过受权 Service 受理，不绕过 Run 授权和幂等。

## 4. 持久计划

ScheduleDefinition 默认 cron=`*/5 * * * * *`、timezone=UTC、max_lateness_seconds=10、run_on_startup=True、backfill=False、overlap=coalesce。迟到仅限制首次触发受理，不是执行超时。

计划按稳定名称安装/更新。多个 Worker 注册同一份定义，不选主或按 Worker ID 复制计划。目标 TaskBinding 禁用时暂停计划，不消费仅属于该能力的队列，但保留入口的禁用检查，防止残留 tick 执行；共享队列仍有启用消费者时继续监听。

短 tick 只投递实际处理，不占受限队列等待自己的子任务。当前三项维护计划是文件清理、Flow 协调、模型费用扫描；每轮有界，停机后读取当前领域待办，不补全部旧时间片。

## 5. Step、失败与恢复

原生 Workflow 负责分支、循环、子执行和显式等待；外部 I/O、扫描等非确定性操作位于 Step。safe_step 是单层 DBOS.step 的薄包装，负责异常安全化，不创建另一套执行引擎，也不把整个 Agent 循环包装成可重试的大 Step。

Step 返回经领域控制的小型数据，不持久化凭据、供应商原始正文、运行句柄或未控制的异常；safe_step 不自动清洗返回值。仅在外层 Workflow 捕获异常不足以保护内部检查点。错误使用代码声明的稳定分类，业务事实仍由所属 Service 保存。

Flow 投递/取消前的实时观察和授权有意在重放时再次执行，不以缓存计划授权新的副作用；不增加或重排业务 Step，不改变稳定执行 ID。Agent 保留 DBOSDurability 的原生模型检查点，自定义 I/O 工具显式建立 Step。

进程中断由原版本恢复原执行；失败不等于无限重试。明确允许的只读操作可以有界重试；费用等待、分页、截止和下一轮由领域持久状态驱动。模型发送不隐藏重试，检查点不保证供应商只计费一次。

## 6. 当前任务与事实

| Task | 领域 Service 负责 | 执行适配负责 |
|---|---|---|
| 费用扫描/范围核对 | 到期范围、原 Token、冷却/分页、证据、金额及同事务提交 | 有界扫描、按范围合并投递、Step 和进度 |
| 文件清理 | 删除/保留规则、引用、存储锁、失败状态和重试待办 | 有界清理轮次及观测 |
| Flow 协调 | 领取、授权、原契约、取消意图、结果验收、引用与终态 | 实时读计划、幂等投递/取消、再观察并写回 |

没有待办是正常结束，不表示所有费用已确认、文件已清理或 Run 已结束。DBOS 与领域状态是独立事实，不能以领域终态推定 Workflow 检查点已完成。

## 7. Worker 与资源

Worker 准入先核对身份、协调卷、制品和公共配置，收尾本槽位旧调用，再持久化队列预算，之后启动恢复、安装计划并就绪。所有实例使用一致的计划及两级预算；新增进程不扩大系统上限。

队列隔离文件、Flow 协调、费用扫描与慢查询；实际 HTTP 另用覆盖 API/Worker 的 PostgreSQL 共享容量和真实 Token 范围锁。运行中丢失所有权必须停止并清理，不能换连接提交。并发、重试、等待、批次和输出有界。

执行版本覆盖代码、锁文件、声明与资源。发布保留原制品/身份处理在途，不能移动目录后冒充旧检查点兼容。健康状态区分进程/依赖与共享任务进度。默认预算见[架构](architecture.md#34-定时任务与队列)，配置和发布命令见[配置](configuration.md)、[部署](deployment.md)。

官方语义：[Workflow 确定性](https://docs.dbos.dev/python/tutorials/workflow-tutorial#determinism)、[Step](https://docs.dbos.dev/python/tutorials/step-tutorial)、[队列](https://docs.dbos.dev/python/tutorials/queue-tutorial)、[定时计划](https://docs.dbos.dev/python/tutorials/scheduled-workflows)。实际行为以锁定版本 SDK 和原生集成测试为准。
