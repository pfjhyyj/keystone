# 编写业务 Flow

FlowDefinition/FlowPackage、模型角色和受控 Context 的完整契约见[Flow 领域](domains/flow.md)。本文保留实际接入与工具示例；人工决定由业务 Service 完成，前后自动处理使用独立 Flow。

本文代码示例使用现有 Context 和显式等待接口。[渠道协议与宿主接口](domains/model-access.md#84-service-与-adapter-接口)在 Model Access 内分工，业务流程仍只调用公开 Service/Context，无需新增渠道分支；数据库不配置协议组合。视频和独立模型接入库尚未提供。

可复用示例位于 `backend/src/keystone/examples/document_review/`。它使用现有
Pydantic AI、Harness、DBOS、文件服务和 ModelRuntime，不引入额外执行框架或业务表。
集成测试通过 `backend/tests/support/flow_fixture/` 直接执行这份代码，不维护第二份示例。

## 1. 接入与目录

```text
document_review/
├── __init__.py
├── contracts.py             # 轻量输入输出、工具结果 DTO、业务验收、FlowDefinition 与 PACKAGE
├── workflow.py              # 预算、Agent、工具及 DBOS workflow/step
└── skills/review-text/SKILL.md
```

默认流程目录为空。需要装配现有文本审查示例时，在 composition.py 的显式列表加入：

```python
from keystone.examples.document_review.contracts import PACKAGE

FLOW_PACKAGES = (PACKAGE,)
```

API 只加载轻量 contracts；Worker 根据 execution_modules 导入 workflow，不再手动维护另一份执行导入。流程模块可以导出显式 register 工厂，装配器每个进程只调用一次。外部包及必需资源通过 FlowPackage 声明，不扫描目录或在线安装代码。

上传可读 UTF-8 文本、创建可调用的模型后，按下列结构发起现有示例：

```json
{
  "definition_key": "document.review",
  "definition_revision": "1",
  "request_key": "review-unique-request",
  "input": {"source_file_id": "11111111-1111-4111-8111-111111111111"},
  "model_bindings": {"review": "your-model-key"}
}
```

实际请求仍可能计费。模型角色在业务 JSON 外选择并于受理时固定；缺失或多余角色被拒绝。输入、角色或显式 revision 变化使用新请求键，响应未知时保留原请求原样重试。当前 key 只安装一个 revision，省略 revision 表示请求当前版本，不提供历史版本选择。

业务模块的流程放在 workflows/<flow>/；Service、实体和接口分别归 services/models/controllers。catalog.py 只声明权限和审计，普通 Service 不导入执行上下文。工作流导入方式为：

```python
from keystone.platform.flow.schemas.definitions import FlowDefinition, ModelRole
from keystone.platform.flow.schemas.packages import FlowPackage, PackageResource
from keystone.platform.flow.workflows.context import flow_context
```

在每个 Step 内用 `ctx = flow_context(run_id)` 重建短期能力对象，通过 `await ctx.inputs()`、`await ctx.read_file(file_id, max_bytes=...)`、`await ctx.upload(key, name, content, content_type=...)` 使用资源。Context 不在 Step 间传递；临时路径和运行句柄不进入检查点。文件读取限于本 Run 登记输入或产物，同时仍要满足当前文件内容授权。

Agent 的 deps 使用 run_id、model_role、node_key、agent_key、agent_call_id 及小型业务值，FlowModel 按角色读取固定绑定。普通文本调用使用 `await ctx.invoke_text(role, TextRequest(...), node_key=...)`，必须在持久 DBOS Step 内；一次 Step 仅一次直接调用，不通过角色变化获得另一发送身份。每个角色可绑定不同模型条目和渠道，发送读取该角色的固定绑定，调用记录保留 model_role。模型发送仍由 ModelRuntime 复查原身份及固定目标并登记调用；已发送请求继续有界等待和保存既有回执，禁止用外层自动重试整体重发。

直接 invoke_text 的文本结果由外层 DBOS Step 持久化。登记后或响应返回后、Step 检查点保存前崩溃，恢复以 model_result_unavailable 结束，不重发；已保存的响应回执、用量和费用继续保留。Step 检查点保存后崩溃，DBOS 使用原结果继续后续步骤。调用账务保存成功不等于文本已有可恢复检查点，不能据此重新发送以补齐结果。

长文件/原生工具用 `ctx.execution_scope()`、`ctx.materialize_file()`、`ctx.upload_file()` 和 `ctx.run_process()` 管理取消及资源释放。Context 不自动创建 Step；原生 DBOS 工作流和 Step 仍由流程作者明确声明。内部 Runtime/身份工厂及 SDK 不作为公共 Context 能力。

### 1.1 结构化与图片操作

角色通过 operation 声明语义，通过 delivery 声明同步或异步交付，不能在执行时互换。管理员在模型配置中声明 operations；选择器按角色的两个字段过滤，后端仍在受理及发送前检查。已实现协议、DTO 和边界见[模型操作](domains/model-access.md#6-模型操作与产物)。

| Context 方法 | 角色 operation / delivery | 小型返回值 |
|---|---|---|
| invoke_structured(role, StructuredRequest, node_key=...) | text.structured / sync | StructuredResult：invocation_id、对象 content |
| invoke_vision(role, VisionRequest, node_key=...) | image.understand / sync | TextResult：invocation_id、文本 content |
| invoke_image(role, ImageGenerateRequest, node_key=...) | image.generate / sync | ImageResult：invocation_id、files[].file_id |
| edit_image(role, ImageEditRequest, node_key=...) | image.edit / sync | ImageResult |
| submit_image(role, ImageTaskRequest, node_key=...) | image.generate / async | ImageTask：invocation_id |
| image_result(role, invocation_id, node_key=...) | image.generate / async | ImagePending 或 ImageResult |

这些方法均放在持久 Step 内；图片字节与供应商任务 ID/URL 不进入 Step 参数或结果。输入只接受 Run 内有当前内容权的 File ID，image.understand 角色还须声明 file:content_read；生成/编辑再声明 file:create。图片输出通过 output_file_fields=("files.*.file_id",) 登记。结构化 Agent 使用 NativeOutput(..., strict=True) 并绑定 text.structured 角色；普通工具输出 Agent 仍使用 text.generate。

异步流程显式区分提交与查询，查询沿用同一 node_key，不用循环计数创建新生成请求。下例假定声明了 delivery="async" 的 generate 角色；输入是 ImageTaskRequest，输出是 ImageResult：

```python
from dbos import DBOS
from keystone.core.errors import ServiceError
from keystone.platform.flow.workflows.context import flow_context
from keystone.platform.model_access.schemas.images import ImageTaskRequest

@DBOS.step(name="example.images.submit", retries_allowed=False)
async def submit(run_id):
    ctx = flow_context(run_id)
    task = await ctx.submit_image(
        "generate", ImageTaskRequest.model_validate(await ctx.inputs()), node_key="generate"
    )
    return task.model_dump(mode="json")

@DBOS.step(name="example.images.query", retries_allowed=False)
async def query_result(run_id, invocation_id):
    result = await flow_context(run_id).image_result(
        "generate", invocation_id, node_key="generate"
    )
    return result.model_dump(mode="json")

@DBOS.workflow(name="example.images.run")
async def generate_images(run_id):
    task = await submit(run_id)
    for _ in range(60):
        result = await query_result(run_id, task["invocation_id"])
        if result.get("state") != "pending":
            return result
        await DBOS.sleep_async(result["retry_after_seconds"])
    raise ServiceError(504, "model_task_timeout", "原任务查询已结束")
```

平台保存原提交的截止时间和查询次数，重启不会重置。同步图片响应尚未保存或缺少原字节时不能重生成；已保存的稳定文件可继续确认。异步回执已保存后只查询原任务、重新获取原产物，文件使用原逻辑键；摘要冲突安全停止。取消、撤权或配置变化阻止新查询和文件发布。上述循环是流程自身的原生 DBOS 编排，不新增平台任务引擎。

## 2. 输出验收：结构、业务事实与副作用

执行顺序为：准备可信源信息 → Agent/工具 → 业务验收 → 保存报告。

- `Judgment` 用 Pydantic 限制枚举、字符数范围和摘要长度，并拒绝纯空白摘要。
- `prepare` 从受控文件服务读取原文件，确定本次 `source_file_id` 和精确字符数。
  这份可信元数据进入 Agent deps，不由模型输出决定。
- `accept_judgment` 验证结果所属文件和字符数；Agent 的 output validator 将可公开的
  契约错误反馈给模型，最多修正一次，修正请求也消耗同一请求/token 预算。
- `save_report` 在上传之前再次执行相同验收，拒绝未通过验收的结果；报告通过原有稳定
  owner/slot/request_key 上传，恢复不会换一个逻辑产物键。
- Flow 协调器继续按受理时保存的 JSON Schema 验证最终输出，并登记报告的文件引用。
  JSON Schema 不能替代 Python 自定义校验器、对象归属检查或具体业务规则。

更复杂的业务应在自己的验收函数检查候选集合、范围、数量和引用完整性。不要将所有业务
规则搬到平台协调器，也不要在模型 output validator 中执行上传、审批、采用等副作用。
授权失败及响应未知不作为输出修正的理由，不重复发送原模型请求。

匹配文件与字符数只能证明元数据满足契约，不能证明模型完整阅读或判断准确。
`needs_attention` 表示结果需要复核或证据/覆盖不足；Flow 成功不代表业务批准。
真实业务应另外用人工核对样例验证内容质量。

## 3. 预算：在可信代码中集中声明

FlowLimits 定义不可变、跨 Run 恢复的共享预算，当前实现及资源接入边界见[Flow 运行约束](domains/flow.md#6-累计预算与资源)。未显式指定时使用可信默认值；输入、角色绑定和 Agent 参数不能提高额度。示例的局部 `ReviewBudget` 继续约束自己的 Agent 循环，不是平台 Agent 注册或管理系统。

| 限制 | 示例值 | 执行位置 |
|---|---:|---|
| 模型请求次数，包含输出修正 | 4 | Pydantic AI UsageLimits |
| 成功工具调用次数 | 3 | Pydantic AI UsageLimits |
| 累计输入与输出 token | 16000 | 按模型响应报告的 usage 检查 |
| 每次请求的输出 token 上限 | 2048 | model_settings，经 FlowModel 检查 |
| 输出修正次数 | 1 | Agent retries |
| 流程时限 | 180 秒 | FlowDefinition/DBOS 与 workflow 局部 timeout |
| 源文本 | 16 KiB UTF-8 | 文件读取边界 |
| 首次预览 / 单次分段读取 | 512 / 2048 字符 | 工具 DTO 与读取边界 |

FlowModel 默认输出上限仍为 1024，允许可信业务代码设置 1～4096 的整数；越界或无效值
在发送前拒绝。不从公开 Flow 输入接收预算，不接受模型自行提高额度。
总 token 限额依赖上游报告、在响应后检查，不是精确预付费或金额上限；费用事实仍由
ModelInvocation 和原查费机制维护，不按 token 估算成已确认费用。

示例只有一个 Agent，UsageLimits 约束其完整循环。模型/工具尝试及修正共用预算；示例声明 FlowLimits(model_requests=4, tool_calls=3, iterations=4, input_tokens=16000, output_tokens=8192, file_count=2, file_bytes=24576)，局部总 token 16000 仍额外检查。业务
不得捕获预算异常后创建新的 Agent 循环继续尝试。多个 Agent 同时受同一 Run 的 FlowLimits 约束；局部 UsageLimits 不替代平台累计账本。工具和循环使用 consume_tool / consume_iteration 在原生 Step 内先计额；FlowModel 为每个 Agent 模型 Step 自动计一次迭代。产物、操作许可与临时空间接口见下节，验收状态集中维护在运行治理设计。

局部 timeout 在恢复时重新进入；跨恢复的执行超时由既有 DBOS workflow timeout 管理，
不能仅靠局部计时器。DBOS 在首次出队执行时确定截止时间，排队等待不计入执行时限，
恢复沿用原截止时间。DBOSDurability 重放已完成模型步骤，现有稳定调用 ID 与发送登记
继续阻止恢复时重复计费；模型响应未知或缺少可恢复结果时停止，不另起循环。

安全错误码：`flow_budget_exceeded`（请求/工具/观测 token 预算耗尽）、`flow_timeout`
（局部流程超时）、`flow_invalid_output`（输出不合契约或修正耗尽）。DBOS 自身取消/超时
仍按原引擎状态投影。向外不返回供应商正文、异常堆栈和原始模型消息。

### 3.1 工具、循环、产物和临时空间

工具在自己的 DBOS Step 内先调用 await ctx.consume_tool("tool_name", request_digest=...)；摘要为可信参数的稳定 SHA256，不记录参数正文。同一 workflow_id/step_id/key 重入不双扣，不同参数摘要拒绝。显式候选循环在每次相应 Step 内调用 await ctx.consume_iteration("candidate")；同一 Step 内多次迭代须使用不同可信 key，不能复用标识隐藏工作。预算记账不替代工具或业务 Service 的副作用幂等，不自动包装第二套工具执行器。

文件输入在受理时按唯一 File ID 计额；产物上传前以固定 key、名称、MIME、实际大小和摘要预留，未确定或失败消耗不退还，原 key 恢复不双扣。metadata 冲突在存储写入前拒绝。读取同一输入/产物不重复计入逻辑文件数量和字节；输出引用仍须声明 output_file_fields，并通过当前权限和原 Run 登记范围校验。

ctx.workspace(max_bytes=...) 是本 Task 的托管输出工作区；返回 Path 只在上下文内有效。ctx.run_process(output=..., output_limit=...) 仅允许本 Task 当前工作区，且输出上限不超过承诺；子 Task 不继承该写入许可。无文件输出的进程继续使用有界 stdout/stderr。原生 I/O、进程和清理均完成后才释放工作区及 Run 许可。

例如，以下是一个纯文件复制 Step 的接入形状，输入声明 source_file_id，输出声明 file_id；可执行代码由可信流程固定，不从输入接收 argv：

```python
import hashlib
import sys
from dbos import DBOS
from keystone.platform.flow.workflows.context import flow_context

@DBOS.step(name="example.copy", retries_allowed=False)
async def copy_file(run_id):
    ctx = flow_context(run_id)
    data = await ctx.inputs()
    await ctx.consume_iteration("copy")
    await ctx.consume_tool(
        "copy", request_digest=hashlib.sha256(str(data["source_file_id"]).encode()).hexdigest()
    )
    async with ctx.materialize_file(data["source_file_id"]) as source:
        async with ctx.workspace(max_bytes=16 * 1024**2) as target:
            result = await ctx.run_process(
                [sys.executable, "-c", "import shutil,sys; shutil.copyfile(sys.argv[1],sys.argv[2])", str(source), str(target)],
                output=target, output_limit=16 * 1024**2,
            )
            if result.returncode:
                raise ValueError("Copy failed")
            file_id = await ctx.upload_file("copy", target, name="copy.bin", content_type="application/octet-stream")
    return {"file_id": str(file_id)}
```

同一 Task 的嵌套模型/文件/工作区/进程操作复用一个 Run 操作许可，子 Task 独立取得许可；额度满返回 flow_capacity_busy，不隐式重发模型。工作区、物化输入和上传 staging 各保留自己的临时副本承诺；上例最坏同时需要三个 16 MiB 副本、三个宿主传输槽位，因此定义至少声明 temporary_bytes=48 MiB、file_count=2、file_bytes=32 MiB、tool_calls=1、iterations=1。宿主文件/磁盘限制继续同时生效，实际媒体容量与吞吐还须按宿主配置。

普通业务 Service 保持显式依赖和原授权；Workflow 在它执行额外外部操作前计额，并用 ctx.execution_scope(permissions=...) 持有许可。Context 约束这些受控入口，不承诺拦截任意 Python、任意工具自行写盘或不受管理的后台 Task。

## 4. 工具摘要与按需读回

工具成功结果返回小型、明确的 DTO：`status`、有界 `summary`、可信源引用、必要计数、
有界 `preview`/`text`、`truncated`，分段结果再包含 offset 和 next_offset。

`inspect_document` 不把完整正文塞入后续所有请求；`read_document` 只能读取本次输入文件，
不接受模型提供的任意文件 ID 或路径。偏移按 Unicode 字符计数，读取长度由 schema 和
实际 I/O 入口双重检查。模型越界的偏移可有界修正，身份、权限和文件错误继续抛出原安全
异常，不包装成一个看似成功的摘要。

长日志、检测结果等由具体业务显式存成受控 File/产物，工具只返回摘要及文件 ID/逻辑键，
再通过有界工具按需读回。读取始终走当前授权；不把本机路径、打开的句柄、密钥或完整
日志写入 FlowRun 或提示词。摘要也应按业务限制敏感信息。

这里没有新增通用 ToolResult 表、事件中心、上下文管理器或工具执行器。普通工具的
外部 I/O 显式放在 DBOS step 中；不要在整个持久 Agent 外再包自动重试的大 step。

## 5. 验证

在 backend 目录运行：

```sh
uv run --locked pytest -q tests/unit/test_document_review.py tests/unit/test_flow_contracts.py
# 按 development.md 配置专用 keystone_test 数据库后：
uv run --locked pytest --require-postgres -q tests/integration/test_flow.py tests/integration/test_model_live.py tests/integration/test_review_recovery.py tests/integration/test_flow_extensions.py
```

单元检查覆盖错误源/计数及空白摘要禁止上传、真实 Agent 修正与预算、Unicode 摘要和
分段边界、输出 token 上限与安全失败。原 Flow 集成检查继续验证 Worker/SDK 链路、
逐轮调用记录，以及登记、模型响应、产物上传后的崩溃恢复。test_review_recovery.py 还在
模型检查点已完成、Agent 尚未消费响应的位置中断进程，验证恢复后请求、工具、token、
输出修正预算与原截止时间继续生效，且不会多发请求或生成报告。测试使用确定性上游替身或
本机协议服务，不访问真实模型，也不将这些结果视为业务质量评测。

`test_flow_extensions.py` 使用真实 Worker 和确定性 HTTP 替身，核对相同模型复用，以及不同角色分别绑定不同条目、渠道、上游模型时的实际请求；对直接 invoke_text 的三个崩溃窗口执行真实退出与重启，检查调用 ID、请求次数、账务及输出引用。历史格式另验证旧输出联合类型、入口改名后的结果协调和边界请求原样重放。


## 运行诊断

控制台运行详情分别加载执行步骤、累计预算、关联模型调用、文件用途及费用。诊断接口 GET /api/v1/flow-runs/{id}/diagnostics 支持 section=budget/models/files 和 offset/limit；不需要业务额外保存执行日志。实际模型 Step 自动记录 workflow_id/step_id，Agent 另保留自身调用 ID；异步图片展示原提交位置。历史位置/预算缺失明确显示未记录，不按当前定义补造。

调用范围与文件元数据权限独立于 Run 权限，下载再次检查内容访问权。input/output 或报告文件只有 Run 级用途；仅固定图片产物键、原图片调用可见，且登记摘要/大小/MIME 完全匹配原 Manifest 时显示源调用。预留输出 token、已报告输入 token、未结清文本请求和已知/未知费用分别表达，预算不能用于推算账单。接口与历史兼容集中见[Flow 运行约束](domains/flow.md#8-api授权与诊断)。
