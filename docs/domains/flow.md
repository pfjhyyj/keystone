# Flow

Flow 提供独立流程的受理、原身份授权、幂等、取消、恢复与结果查询。业务资源、决定和版本由业务 Service 保存；DBOS 保存工作流、Step、队列与检查点。普通 Service 不依赖 DBOS，也不把每个后台 Task 注册成 FlowRun。

接入示例见 [Flow 开发](../flow-development.md)，执行设施见[任务执行](../execution-design.md)，部署切换见[部署说明](../deployment.md)。

[渠道代码适配](model-access.md#8-多渠道模型接入设计)已集中到显式声明，Flow 继续通过公开 Runtime 调用，固定原渠道/模型选择并保存必要回执；数据库不配置协议行为。模型角色、旧快照、image_result 原任务查询及显式等待方式保持原契约。代码整理会改变执行版本，原在途运行仍须保留原制品和身份收尾；视频尚未开放。

## 1. 定义与装配

### FlowDefinition

可信 Python 代码声明契约，每个 key 只安装一个 revision：

| 字段 | 契约 |
|---|---|
| key / name / description | 流程标识、目录名称与说明；key 在装配中唯一 |
| revision / entrypoint | 契约版本、原生 DBOS Workflow 名；入口不能重复 |
| input_model / output_model | Pydantic 对象模型；input 禁止额外字段，验证和序列化均须为对象 |
| required_permissions | 显式功能权限；角色需要 model:invoke，图片输入需要 file:content_read，生成/编辑还需要 file:create |
| file_fields / output_file_fields | 序列化字段名构成的文件路径 |
| model_roles | 最多 16 个独立模型用途；角色不是用户权限角色 |
| timeout_seconds | 1～86400，默认 300；恢复沿用原截止 |
| limits | 不可变 FlowLimits，受理后固定在 Run 中 |

ModelRole 包含 key、label、operation、delivery。key 为 `[a-z][a-z0-9_]{0,63}`，label 去除首尾空白后长 1～128；操作及交付能力见[模型接入](model-access.md#6-模型操作与产物)。角色必须全部绑定，不提供默认模型、可选角色或自动回退；多个角色可以使用同一模型。

### FlowPackage

FlowPackage 包含 key、host_api_version=1、definitions、execution_modules，以及可选 execution_packages / required_resources。PackageResource 指定顶层 Python package 与包内相对文件；拒绝绝对路径、越界、符号链接、缺包、缺文件及不兼容宿主。

composition 的 FLOW_PACKAGES 显式生成目录、归属和执行清单；API 读取轻量 Schema，Worker 只导入 execution_modules。外部执行模块须声明顶层 execution_package。没有在线安装、动态扫描或多版本路由。

制品指纹覆盖实际加载的 Keystone 代码、uv.lock、规范化声明和声明包内资源，包括二进制资源；不使用工作目录的影子源码冒充已安装代码。重复装配不重复注册入口。普通 SDK/资源对象不进入持久参数。

## 2. FlowRun

| 字段 | 含义 |
|---|---|
| id / definition_key | Run ID 与流程 key |
| definition_snapshot | 原契约、权限、入口、角色选择、文件路径及限额 |
| execution_version | 原制品指纹 |
| actor_id / personal_access_token_id | 原用户与可选原 PAT；执行不改用当前会话扩大权限 |
| request_key / request_digest | 同一用户请求去重与原请求摘要 |
| input / result | 规范化业务输入、成功结果；各不超过 64 KiB JSON |
| status | pending、running、succeeded、failed、cancelled |
| cancel_requested_at | 取消意图；不等于执行已取消 |
| error_code | 安全白名单错误分类 |
| observed_at / finished_at | 最近执行端观察、终态时间 |
| created_at / updated_at | 受理与最近投影时间 |

状态只能从 pending/running 进入终态；已确认终态不被旧观察覆盖。pending 可能等待投递，running 不证明所有外部操作均已完成。succeeded 表示输出契约和引用校验通过，不代表业务批准。只有 succeeded 保存 result；取消与完成竞争时保留真实终态。

format=2 快照包含 package_key、host_api_version、revision、entrypoint、required_permissions、timeout_seconds、input/output_schema、file/output_file_fields、model_roles、model_bindings 和 limits。绑定固定 model_entry_id、channel_id、provider_model、provider、api_base、operation、delivery；没有密钥。公开快照排除内部入口/权限/选择，另返回 role→model key 的白名单绑定。

## 3. 受理与幂等

POST /api/v1/flow-runs 接收 definition_key、request_key、input，以及可选 definition_revision 和 model_bindings。模型选择独立于业务 input；不接受客户端提交内部 Selection、调用 ID 或限额。

受理在同一事务内完成当前授权、请求查重、输入规范化、全部角色解析、预算初始化、输入引用和成功审计。任何一项失败全部回滚。锁序为身份/Run → 全部 Entry → 全部 Channel → dispatch → Run 预算/claim/调用 → 按 ID 排序的 File；没有外部模型 I/O。

查重先于当前定义、默认版本和模型解析。同键不同业务输入、角色、显式版本或原 PAT 冲突；相同请求返回原 Run，不重新选择模型。新格式规范化输入、完整请求和快照分别检查 64 KiB。历史请求按原摘要解释，旧边界请求先命中原 Run，再应用新 envelope 限制。

FlowService.start_in_transaction(session, auth, data) 供已持 AccessContext 身份锁的上层业务使用；不另开事务或自行提交。Worker 从持久 pending 记录发现并投递，提交后临时协程不是可靠投递依据。

## 4. 文件路径与引用

路径使用 `serialization/by_alias=False` 字段名；数组用 `*`，例如 `documents.*.file_id`。可空字段、对象联合和终点 UUID/UUID 列表按编译规则处理；拒绝数字下标、重叠路径、非法类型、递归与远程 Schema 引用。

输入、输出各最多 32 条路径，每条最多 8 段；每次提取最多访问 4096 个位置、最多 100 个唯一 File ID。空集合和 null 不产生引用，重复 ID 去重。受理与结果协调使用同一解释规则。

输入保留为 flow:<run_id>/input，产物为 artifact.<稳定 key>，成功输出为 output。结果只能使用本 Run 已登记且有效的文件；输入可作为输出，任意其他 File ID 被拒绝。输出引用和结果原子保存，失败不留下部分新引用。业务长期采用结果时在业务事务中保留自己的引用。

引用表示使用关系，不授予文件内容、元数据或管理权。每次受保护文件 I/O 仍复查当前授权、Run 使用范围和内容完整性。

## 5. FlowContext 与 Agent

flow_context(run_id) 返回短期能力对象；不保存到检查点，不暴露 Runtime、Session、存储位置或 SDK 客户端。

| 接口 | 行为 |
|---|---|
| inputs() | 原输入的独立副本 |
| check(permissions=...) | 复查原身份和当前权限 |
| execution_scope(permissions=...) | 受控操作许可、持续授权监控与退出清理 |
| read_file(file_id, max_bytes=...) | 有界读取当前 Run 文件 |
| materialize_file(file_id) | 作用域内的受控临时路径 |
| upload(key, name, content, content_type=...) | 稳定 key 上传产物，返回 File UUID |
| upload_file(key, path, name=..., content_type=...) | 固定打开句柄校验长度与摘要后上传 |
| artifact(key, max_bytes=...) | 查询并验证已登记产物，缺失返回 None |
| workspace(max_bytes=...) | 受额度约束的临时工作区 |
| run_process(argv, **bounded_options) | 有界原生进程；输出只接受当前 Task 的活跃托管工作区及 output_limit |
| invoke_text / invoke_structured / invoke_vision | 在原生 Step 中使用匹配角色，返回明确 DTO |
| invoke_image / edit_image | 生成/编辑并返回受控 File 引用 |
| submit_image / image_result | 提交或查询原异步图片任务，pending 由业务显式用 DBOS sleep 轮询 |
| consume_tool(key, request_digest=...) / consume_iteration(key) | 在原生 Step 内预留累计工具/迭代次数 |

Context 不隐式创建 Workflow/Step。模型调用 ID 由 Run、DBOS workflow_id/step_id 稳定派生，同一 Step 不能换角色获取新身份；FlowModel 让每个真实 Agent 请求经过相同 ModelRuntime。Agent deps 只包含可序列化标识，同一 Agent 多轮共享 agent_call_id，每轮调用分别记账。

每次新发送复查原用户/PAT、Run、角色操作及固定目标。模型 key 改映射不重选；Entry 的渠道/模型、Channel 的 provider/api_base 或能力改变则拒绝。已发送请求在有界窗口内保存回执、用量和费用，取消/撤权阻止新发送及后续文件发布。

## 6. 累计预算与资源

| FlowLimits | 默认值 |
|---|---:|
| model_requests | 64 |
| tool_calls / iterations | 各 256 |
| concurrent_operations | 4 |
| file_count | 100 |
| file_bytes | 268435456 |
| temporary_bytes | 67108864 |
| input_tokens / output_tokens | 1000000 / 65536 |

限额为严格整数；模型次数最高 4096，工具/迭代最高 16384，并发 1～16，文件数最高 10000，文件/临时字节最高 32 TiB，token 限额最高 2⁶³−1。零预算禁止相应操作；只有可信定义能设置限额。

FlowRunBudget 保存原 limits/counters，FlowBudgetClaim 以 run_id/kind/key 唯一保存预留摘要。相同 key、摘要和额度重入不双扣，不同内容冲突；失败、未知发送及取消不返还已承诺消耗。恢复、创建新 Agent 或换执行进程不会重置预算。

模型在唯一发送登记事务内预留次数和 max_tokens；输出额度不按实际用量退款。input_tokens 是供应商已报告值，响应后可达上限，调用及费用事实仍保存，后续文本请求停止。无输入用量报告的已结束文本调用阻止后续文本发送，正在进行的并发调用不会被误作已结束未知调用。unreported_text_requests 单独表示未结清请求；图片生成和原任务 GET 不虚构 token 或第二次生成。

工具/循环使用 workflow_id/step_id/逻辑 key 的稳定 claim；工具摘要只保存 SHA256。FlowModel 自动计入每轮 Agent 迭代；业务 Service 的额外 I/O 由 Workflow 在操作前显式 consume_tool 并持 execution_scope，平台不拦截任意可信 Python 代码。

同一 Run 的并发操作使用 PostgreSQL 会话容量锁。同 Task 嵌套复用并复查许可，子 Task 独立申请；不靠 TTL/PID 猜测回收 active_count。取消与失锁先等待 I/O/原生进程及文件清理，再释放许可。模型有界发送/响应窗口的监控保留回执收尾权，不向子 Task 传递豁免。

临时空间由 Files TransferScope 管理；全局物理容量和 Run 承诺同时检查，顺序刷盘不减少该 scope 的完整承诺。记录读兼容 version=1/2，崩溃只在 OS 所有权释放后回收。临时路径、句柄和许可不进入检查点；可信进程自行访问其他路径不属于托管空间保证。

## 7. 取消与恢复

取消接口只保存意图；协调器核实入队情况并调用 DBOS，观察确认后同步 cancelled。未开始的 Run 不再投递。取消不撤回已发送请求、实际费用或已完成文件写入，也不倒改成功结果。

| 失败窗口 | 恢复行为 |
|---|---|
| 尚未发送登记 | 原身份和配置复查后才可登记 |
| 已登记、无正文检查点或可恢复证据 | 保留 unknown/已知调用事实，以 model_result_unavailable 停止，不重发 |
| 文本响应已保存费用、Step 未保存正文 | 费用不等于可恢复正文，不重发 |
| Step 已保存结果 | DBOS 重放原结果，继续后续步骤 |
| 原图片 Manifest/任务回执已保存 | 查询原任务或验证原产物，按原 ID/key 继续；具体边界见[模型操作](model-access.md#6-模型操作与产物) |
| 文件已保存、结果未落库 | 验证原文件及引用，恢复复用同一 File ID |

技术检查点不替代业务幂等或第三方 exactly-once。业务写入使用自身稳定标识、唯一约束及事务；未知结果不通过换 Run/调用 ID 绕过。原制品、原 Worker 身份、原契约共同约束执行恢复，改目录或代码也会改变版本。

无版本旧快照仅有 output_schema/output_file_fields，旧 Selection-in-input、旧摘要和原 input 保持原义，不重写历史。旧点路径保留 Unicode 字段、原数量/深度和 UUID|list[UUID] 联合，不套用新路径复杂度上限，仍检查 UUID 与唯一文件数。format=2 缺失 limits 的历史行明确为未记录共享预算，不回填当前默认；新快照缺预算账本则失败关闭。未知/损坏格式返回 flow_snapshot_unavailable。旧结果按原快照同步，即使当前定义下线或入口改名。

## 8. API、授权与诊断

| API（/api/v1） | 规则 |
|---|---|
| GET /flow-definitions | flow:start；只列出当前身份满足 required_permissions 的定义 |
| POST /flow-runs | flow:start 与定义权限；同事务受理 |
| GET /flow-runs、GET /flow-runs/{id} | flow:read，默认本人；flow:manage 才能读取他人 Run |
| POST /flow-runs/{id}/cancel | flow:cancel 且本人或 flow:manage |
| GET /flow-runs/{id}/steps | 前后复查 Run；最多 32 个引擎关联流程、500 条步骤，仅名称/位置/时间 |
| GET /flow-runs/{id}/costs | Run 可见且有模型记录权限；费用汇总为最近 30 天，未知不是零 |
| GET /flow-runs/{id}/diagnostics | section=all/budget/models/files，offset=0、limit=50（最高 100） |

诊断在同一短身份事务中组合三个领域的公开 Service，返回前复查 Run。模型和文件各自先按 SQL 对象范围过滤再分页；无板块权限返回 available=false，未请求板块为 null，返回 truncated/next_offset。

模型项仅含调用 ID、operation、status、角色/节点/Agent、原 workflow_id/step_id 及受控错误码。需要 model_invocation:read，默认本人，读取他人/系统记录还需 read_all；flow:manage 不扩大模型范围。原异步提交位置不被轮询 Step 覆盖，历史位置不补造。调用收尾可先于 Step 历史可查，步骤名称暂缺不等于未发送。

文件项仅含 file_id/slot/可确认的 invocation_id，需要 file:inspect 与当前元数据范围；内容读取另行授权。来源还须同 Run、可见图片调用、原 Manifest 操作/产物键，以及登记摘要/大小/MIME 全部匹配；每个 File 单独验证。缺失/损坏证据、未知索引或同键错误文件不获得来源。input/output/report 保持 Run 用途，不按时间或文件名猜测。

预算返回 recorded/limits/counters，诊断整数用十进制字符串，控制台用 BigInt 计算；原快照和账本仍保存整数。费用独立来自模型账务。诊断不返回 checkpoint、result/cost_context、供应商任务/下载地址、原始消息/异常、凭据或本地路径。

控制台独立加载 Run、步骤、预算、调用、文件和费用；单块故障不覆盖已确认运行状态，迟到响应不覆盖新代次。失去 Run 可见性清空详情，失去资源权限清空对应板块。仅活动 Run 自动轮询，终态暂缺的步骤/费用可手动刷新。

发起、取消写入审计，步骤留在 DBOS，模型费用留在 ModelInvocation；不逐步骤复制审计。人工决定属于业务 Service：保存采用的结果版本及文件引用，批准与后续独立 Flow 的受理可在同一业务事务完成。业务唯一约束负责跨用户幂等，Run 的 actor/request_key 不能替代它。
