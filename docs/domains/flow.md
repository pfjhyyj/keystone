# Flow

Flow 管理需要独立受理、查询、取消及输入/结果契约的运行。业务 Service 保存业务对象和决定，FlowRun 保存原身份、受理契约与结果，DBOS 保存 Workflow/Step 和检查点。普通后台 Task 不创建 FlowRun。

## 核心主流程总览

业务预先确定流程和模型用途，从业务入口提交；运行时不要求先查询通用流程目录。

提交与执行：

```mermaid
sequenceDiagram
    participant B as 业务服务
    participant R as Flow
    participant W as 执行 Worker
    B->>B: 授权业务操作，确定输入
    B->>R: 提交流程、输入和模型绑定
    R->>R: 同事务受理 Run、固定契约并保存输入引用
    R-->>B: 返回 Run ID
    W->>R: 取得已提交任务
    R-->>W: 返回固定输入、契约和模型绑定
    W->>W: 执行流程，保存产物
    W->>R: 提交结构化结果
    R->>R: 同事务保存结果、输出引用及 succeeded
```

结果查询与业务使用：

```mermaid
sequenceDiagram
    participant B as 业务服务
    participant R as Flow
    B->>R: 按 Run ID 查询
    R-->>B: 返回任务状态及成功结果
    B->>B: 按业务操作使用结果，保存自己的记录、引用和审计
```

查询只读。生成 Flow 返回产物，调用方另行保存业务记录、引用和审计；Flow 不反向登记业务或等待回执。业务登记明确失败可由用户重新发起完整操作，通信结果未知时仍沿原键核实。

| 当前状态 | 事件/前提 | 下一状态 |
|---|---|---|
| 无记录 | 受理事务提交 | pending |
| pending | 执行开始 | running |
| pending/running | 执行成功，结果和输出引用提交 | succeeded |
| pending/running | 确认执行失败 | failed |
| pending/running | 确认停止并完成 I/O 收尾 | cancelled |

不变量：受理后输入、契约和模型绑定固定；执行使用原身份/PAT；结果和引用原子保存，终态不被旧观察覆盖；已发生的调用、费用和文件按事实保留。

## 1. 定义与装配

可信 Python 声明 FlowDefinition，通过 FlowPackage 显式装配。由业务入口发起，业务决定是否让用户选择模型；平台仅提供运行查询与管理。

### FlowDefinition

| 字段 | 契约 |
|---|---|
| key/name/description | 唯一流程标识及说明 |
| revision/entrypoint | 每 key 安装一个版本，稳定且不重复的 DBOS 入口 |
| input_model/output_model | Pydantic 对象；输入禁止额外字段，验证/序列化均为对象 |
| required_permissions | 显式功能权限；当前模型/文件权限按所用能力声明 |
| file_fields/output_file_fields | 输入/输出文件路径 |
| model_roles | 最多 16 个独立模型用途 |
| timeout_seconds | None 或 1～86400；默认 300，None 表示不设置整体超时，恢复沿用现有引擎语义 |
| queue | 已配置的队列名，默认 keystone-flows |

ModelRole 含 key、label、operation、delivery；key 为 `[a-z][a-z0-9_]{0,63}`，label trim 后 1～128。所有角色必须绑定，不自动默认/回退，多个角色可用同一模型。操作与交付见[模型接入](model-access.md#6-模型操作与产物)。

队列及其并发沿用现有 QueueDefinition 和 composition 装配。受理时检查队列已配置并将名称固定到 Run 快照，之后按该名称投递；未配置的名称报错。

### FlowPackage

包含 key、host_api_version=1、definitions、execution_modules，可选 execution_packages/required_resources。PackageResource 指定顶层包和相对资源，拒绝绝对路径、越界、符号链接、缺失资源及不兼容宿主。

composition.FLOW_PACKAGES 显式装配；API 只读轻量契约，Worker 导入执行模块。制品指纹覆盖实际安装代码、uv.lock、声明及资源，目录变化也影响版本。无在线安装、动态扫描或多版本路由。

## 2. FlowRun

| 字段 | 含义 |
|---|---|
| id/definition_key | Run 与流程标识 |
| definition_snapshot/execution_version | 原契约和制品指纹 |
| actor_id/personal_access_token_id | 原用户及可选原 PAT |
| request_key/request_digest | 同用户请求去重及原摘要 |
| input/result | 规范化输入/成功结果，各最多 64 KiB JSON |
| status/cancel_requested_at | 运行状态与独立取消意图 |
| error_code/observed_at/finished_at | 安全错误、最近观察、终态时间 |
| created_at/updated_at | 受理/投影时间 |

只有 succeeded 保存 result；成功表示输出契约和引用通过，不代表业务批准。Run 快照保存包/宿主版本、权限、超时、队列、Schema、文件路径、角色与绑定。绑定固定 Entry、Channel、provider/model/base、operation/delivery，无秘密；公开只投影白名单及角色→模型 key。

## 3. 受理与幂等

当前 POST /api/v1/flow-runs 接收 definition_key、request_key、input，可选 definition_revision/model_bindings。选择在 input 外，不接受内部 Selection、调用 ID 或限额。

一个事务完成当前授权、查重、规范化、模型绑定、输入引用和审计。查重先于当前定义/模型解析；同键同请求返回原 Run，不同输入、角色、显式版本或原 PAT 冲突。规范输入、完整请求和快照各检查 64 KiB，历史按原摘要先查重。

锁序：身份/Run → 全部 Entry → 全部 Channel → dispatch → 调用记录 → 按 ID 排序 File，不包含外部 I/O。Worker 从已提交 pending 发现投递。

| 事务内入口 | 行为 |
|---|---|
| start_in_transaction(session, auth, data) | 受理及幂等回执 |
| get_in_transaction(session, auth, run_id) | 当前授权下读取，刷新当前行，不使用旧缓存 |
| statuses_in_transaction(session, run_ids) | 关联 Run 的有界批量状态读取，返回 {run_id: status}，供已授权业务展示 |
| cancel_in_transaction(session, auth, run_id) | 记录取消意图和审计，重复不重记 |

由上层持 AccessContext 身份事务，入口不提交、不开新事务、不访问执行引擎。完整 Run 读取及取消仍要求 flow:read/cancel 和本人/管理范围。回执丢失以原键原请求重试；业务唯一约束仍负责跨用户幂等。

## 4. 文件路径与引用

文件职责与完整交接流程见[文件设计](files.md#15-flow-文件使用与产物返回契约)。受理时固定输入并保留引用，执行不回查来源业务当前附件，不提供原地替换；新输入需新 Run。

路径按 serialization/by_alias=False 字段名，数组用 *（documents.*.file_id）；支持可空、对象联合及 UUID/UUID 列表终点，拒绝数字下标、重叠、非法类型、递归/远程引用。

输入/输出各最多 32 路径，每条 8 段；提取最多访问 4096 位置、100 唯一 File ID，空/null 不引用、重复去重。受理和结果验收用同一解释规则。

引用槽位为 flow:<run_id>/input、artifact.<稳定键>、output。结果只接受本 Run 已登记有效文件，输入可作为输出；引用与结果原子保存。引用不授权，文件 I/O 仍检查执行身份、范围和完整性。

### 批量产物读取

FlowArtifactService.find_many(run_id, keys) 批量返回产物键到 File ID 的映射，复用现有单项查询的授权、文件完整性和引用复核。未登记的键返回 None；已登记但文件异常时明确报错。输入键集合有界，单项 find 内部复用批量实现。

## 5. FlowContext 与 Agent

flow_context(run_id) 在 Step 内重建短期能力，不进入检查点、不暴露 Runtime/Session/SDK。

| 接口 | 用途 |
|---|---|
| inputs()/check(permissions=...) | 原输入副本、当前授权检查 |
| execution_scope(permissions=...) | 操作期间的授权监控及退出清理 |
| read_file(file_id, max_bytes=...)/materialize_file(file_id) | 有界内容或临时路径 |
| upload(...)/upload_file(...) | 稳定键保存产物及引用 |
| artifact(key, max_bytes=...)/artifacts(keys, max_bytes=...) | 单项/批量核验原产物 |
| workspace(max_bytes=...) | 统一目录工作区，见[文件契约](files.md#110-统一临时工作区) |
| local_file(file_id) | 作用域内使用本 Run 的已授权本地文件 |
| run_process(argv, **bounded_options) | 受限进程，输出仅当前 Task 的活跃托管空间 |
| invoke_text/structured/vision/embedding | 匹配角色的明确 DTO |
| invoke_image/edit_image | 当前生成/编辑组合，返回 File 引用 |
| submit_image/image_result | 原异步图片任务，pending 由 Workflow 显式 sleep |

Context 不隐式创建 Workflow/Step。稳定调用 ID 由 Run、workflow_id/step_id 派生，同 Step 不能换角色取得新身份；每个 Agent 请求分别登记，同 Agent 调用共享 agent_call_id。deps 只存小型可序列化标识。

每次外部操作复查原身份/PAT、Run、取消、角色及固定目标。已发送响应可有界收尾，取消/撤权不开始新发送或产物发布。接口用法见[Flow 开发](../development/flow.md)。

execution_guard(session, auth, run_id, definition_key, input) 在原身份和权限校验通过后检查当前业务资格；业务可拒绝执行或报告临时不可用，执行权限始终取原用户当前权限与原 PAT 范围的交集。

## 6. 执行统计与单次操作限制

Flow 不维护累计模型次数、token、工具调用、循环次数或文件总量配额，也不获取每 Run 操作许可。模型调用和 token/费用统计查询 ModelInvocation，按 Run、节点、角色和 Agent 聚合；上游未报告的用量保持未知。步骤、工具执行和重试查询 DBOS 历史，业务可记录带 run_id 的结构化阶段日志，不保存正文或凭据。

每次请求仍校验协议、目标、输入大小和输出参数；文件保留单文件限制与引用幂等，workspace(max_bytes=...) 保留明确的单次工作区上限，原生进程保留超时、stdout/stderr 和输出文件上限。模型、媒体及后台队列继续按实际资源控制并发。取消须等待 I/O、进程和文件清理完成。调用登记、未知结果保护与统计不依赖预算账本。

## 7. 取消与恢复

### 7.1 取消与已生成产物

```mermaid
sequenceDiagram
    actor U as 用户
    participant B as 业务服务
    participant R as 任务执行
    U->>B: 取消任务
    B->>B: 检查当前取消权限
    B->>R: 登记取消意图及审计
    R-->>B: 返回取消请求已受理
    B-->>U: 返回取消请求已受理
    R->>R: 停止新操作，等待已启动 I/O 收尾
    R->>R: 确认执行已取消，记录 cancelled
```

| 当前状态 | 事件/前提 | 结果 |
|---|---|---|
| pending/running | 取消意图提交 | 记录时间，暂不改终态，停止新投递/操作 |
| 已请求取消 | 确认未启动或已停止，I/O 收尾结束 | cancelled |
| 与完成竞争 | 已确认成功/失败 | 保存真实 succeeded/failed |
| 任一终态 | 重复取消/旧观察 | 保持原终态 |

取消不撤回远端请求、真实费用或已完成写入，不自动撤销业务已提交结果。部分产物不作成功 result，已有文件和引用按保留规则处理；清理见[文件回收](files.md#14-无引用文件回收规则)。

### 7.2 恢复窗口

| 已保存事实 | 行为 |
|---|---|
| 尚无发送登记 | 原身份/目标检查后可登记 |
| 已登记，无结果检查点/证据 | model_result_unavailable，保留原调用事实，不重发 |
| Step 已保存结果 | 重放原结果，继续后续步骤 |
| 原图片任务/Manifest | 仅查询原任务、核原文件和摘要 |
| 文件已保存、结果未落库 | 复用原 File ID 和引用 |

费用记录不能替代结果正文。检查点不保证第三方 exactly-once；恢复使用原制品、身份、契约及稳定 ID，不能换 Run 绕过未知结果。

只接受当前制品支持的完整快照，固定原模型选择、输入输出 Schema 和执行身份；损坏或不支持的格式返回 flow_snapshot_unavailable。新制品不能冒充旧执行版本接管检查点。

## 8. API、授权与诊断

| 当前 API（/api/v1） | 规则 |
|---|---|
| GET /flow-runs、/{id} | flow:read，本人；flow:manage 扩大范围 |
| POST /flow-runs/{id}/cancel | flow:cancel＋本人或 manage |
| GET /flow-runs/{id}/steps | Run 可见；最多 32 关联流程/500 步骤，只含名称/位置/时间 |
| GET /flow-runs/{id}/costs | Run＋模型记录权限；当前费用窗口最近 30 天 |
| GET /flow-runs/{id}/diagnostics | section=all/models/files，默认 50、最高 100，offset 分页 |

诊断在短身份事务组合公开 Service，前后复查 Run；模型调用先按独立读取范围过滤，文件仅列当前 Run 的引用，再分页。模型调用不可见时返回 available=false。未请求的板块返回 null，列表通过 truncated/next_offset 分页。

- 模型：read/read_all 独立判断，返回调用、操作、角色/节点/Agent、原 Step 位置及安全错误，不因 flow:manage 扩权。
- 文件：Run 可见后列出关联 File ID/slot，不授予内容读取权；同 Run 可见图片调用与原 Manifest 的产物键、摘要、大小、MIME 全部匹配才补 invocation_id，不按名称/时间猜测。

不返回检查点、私有 result/cost_context、供应商任务/下载地址、原文异常、秘密或本地路径。控制台各板块独立加载，失权清相应数据，迟到结果丢弃；仅活动 Run 自动轮询，终态缺步骤/费用可手动刷新。

人工决定和采用版本由业务保存，批准与后续独立 Flow 可在同事务受理，不提供通用人工等待/唤醒。平台不提供通用发起入口；业务 Service 在已授权的事务中调用 start_in_transaction，业务页面自行查询输入和模型选项。
