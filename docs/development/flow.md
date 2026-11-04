# 编写业务 Flow

本文使用 examples/document_review 展示实际接入，契约与限制见 [Flow](../domains/flow.md)，后台设施见[任务执行](../execution.md)。模型和文件操作通过公开 Context/Service，不拼渠道协议。示例只展示定义与执行代码，业务自己提供授权和受理入口。

## 业务执行接入

所有 Flow 都由业务 Service 通过 start_in_transaction 在身份事务内受理。调用方授权业务对象与精确文件 ID；平台不提供通用发起 API 或业务入口开关。

| 装配入口 | 使用场景与约束 |
|---|---|
| composition.EXECUTION_GUARD | 业务确需在每次操作前附加状态检查时设置；默认 None |
| composition.WORKFLOW_BINDINGS | 注入业务 Service 工厂，bind(runtime) 在 Worker 准入后、执行模块注册前调用；API 只校验声明 |

guard 签名 `(session, auth, run_id, definition_key, inputs)`，在平台核原身份/PAT、Run 和取消后执行，只能收紧权限。复用当前事务，不提交或做长 I/O；401/403/404 转 execution_not_authorized，503 等暂时故障保留暂缓语义。它不用于重新解析已提交任务的输入。

bind 内再导入执行模块，用 Runtime 和 ExecutionIdentity(runtime, run_id) 构造原身份 Service；不在导入时建连接或第二 Runtime。guard/句柄不进快照。实例见 test_business_flow_hooks.py，无需修改平台授权或 Worker 主入口。

## 1. 接入与目录

可复用示例与测试共用一份代码：

```text
document_review/
├── __init__.py
├── contracts.py             # 轻量输入输出、工具结果 DTO、业务验收、FlowDefinition 与 PACKAGE
├── workflow.py              # 预算、Agent、工具及 DBOS workflow/step
└── skills/review-text/SKILL.md
```

默认目录为空，在 composition.py 显式加入示例：

```python
from keystone.examples.document_review.contracts import PACKAGE

FLOW_PACKAGES = (PACKAGE,)
```

API 只加载轻量契约，Worker 按 execution_modules 导入执行；外部包/资源通过 FlowPackage 声明。业务模块按 schemas/services/models/controllers/workflows 分层，catalog.py 仅声明权限和审计。

上传可读 UTF-8 文本、配置可调用模型后，现有示例的发起结构为：

```json
{
  "definition_key": "document.review",
  "definition_revision": "1",
  "request_key": "review-unique-request",
  "input": {"source_file_id": "11111111-1111-4111-8111-111111111111"},
  "model_bindings": {"review": "your-model-key"}
}
```

模型绑定在 input 外且必须完整。新输入/角色/显式 revision 用新请求键，响应未知原样重试；省略 revision 使用当前唯一安装版本。请求可能产生真实费用。

常用导入：

```python
from keystone.platform.flow.schemas.definitions import FlowDefinition, ModelRole
from keystone.platform.flow.schemas.packages import FlowPackage, PackageResource
from keystone.platform.flow.workflows.context import flow_context
```

每个 Step 内重建 flow_context(run_id)，inputs/read_file/upload 等接口见领域文档。Context、临时路径、句柄和 SDK 不跨 Step 持久化；文件限本 Run 登记且当前可读范围。

直接模型调用放持久 Step，一次 Step 只有一次直接调用，不能通过换角色获得新发送身份。Agent deps 只保存 run_id、角色/节点/Agent 标识和小型业务值，每轮由 FlowModel 登记。文本结果由外层 Step 保存；登记后但检查点前崩溃且无结果证据时明确失败，不重发。具体窗口见[恢复规则](../domains/flow.md#72-恢复窗口)。

### 1.1 结构化与图片操作

角色声明 operation/delivery，受理与发送都校验匹配。

| Context 方法 | operation/delivery | 返回 |
|---|---|---|
| invoke_structured | text.structured/sync | StructuredResult，调用 ID＋对象 |
| invoke_vision | image.understand/sync | TextResult |
| invoke_embedding | text.embed/sync | EmbeddingResult |
| invoke_image/edit_image | image.generate/image.edit，sync | ImageResult，调用 ID＋File ID |
| submit_image | image.generate/async | ImageTask，调用 ID |
| image_result | image.generate/async | ImagePending 或 ImageResult |

这些方法在持久 Step 内使用，输入不接受任意 URL/路径，图片字节与供应商私有证据不进检查点；输出声明 files.*.file_id。当前定义需声明所用模型/文件权限。结构化 Agent 使用 NativeOutput(..., strict=True) 和 text.structured，普通工具输出用 text.generate。

异步提交与查询分别声明 Step，沿用同一 node_key，只查原任务：

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

平台持久保存原截止/查询次数，重启不重置；下载失败仅取回原结果，不重新生成。同步图片的 600 秒生成、每份 120 秒下载和 Flow 建议至少 900 秒等协议限制见[媒体实现](../reference/model-access.md#媒体与恢复)，业务不保存取回 URL。

## 2. 输出验收：结构、业务事实与副作用

示例执行 prepare → Agent/工具 → 业务验收 → 保存报告：

- Judgment 校验枚举、字符数和非空白摘要。
- prepare 从原文件读取可信 source_file_id/字符数，放入 deps。
- accept_judgment 校验归属与计数；可公开的输出错误最多反馈模型修正一次，仍消耗原预算。
- save_report 上传前再次验收，使用稳定 owner/slot/request_key；协调器另按原 JSON Schema 验证最终结果和引用。

业务规则放自己的验收函数，output validator 不执行上传、审批或采用。授权失败/结果未知不触发模型修正重发。元数据校验不能证明判断准确，needs_attention 表示待复核，内容质量需真实人工样例验证。

## 3. 预算：在可信代码中集中声明

FlowLimits 约束整个 Run，局部 ReviewBudget/UsageLimits 额外约束示例 Agent，不替代累计账本。输入和模型不能提高限额，不能捕获预算异常后另建 Agent 继续。

| 示例限制 | 值/执行位置 |
|---|---|
| 模型/成功工具调用 | 4/3，UsageLimits，含输出修正 |
| 总 token | 16000，真实 usage 响应后检查 |
| 每次输出 token | 2048，可信设置允许 1～4096，默认 1024 |
| 输出修正 | 1，Agent retries |
| 流程时限 | 180 秒，DBOS 与局部 timeout |
| 源文本/预览/分段 | 16 KiB UTF-8 / 512 / 2048 字符 |

示例声明 FlowLimits(model_requests=4, tool_calls=3, iterations=4, input_tokens=16000, output_tokens=8192, file_count=2, file_bytes=24576)。多个 Agent 仍共享同一 Run 预算；token 响应后计量不代表预付费或费用上限。

DBOS 首次出队确定执行截止，排队不计入，恢复不重置；局部 timeout 重入不能替代它。安全错误为 flow_budget_exceeded、flow_timeout、flow_invalid_output，引擎取消/超时按原状态投影。

### 3.1 工具、循环、产物和临时空间

工具在自己的 Step 调 consume_tool，摘要为参数稳定 SHA-256；候选循环调 consume_iteration，同 Step 多次须不同可信 key。稳定 claim 防重计，不替代工具副作用幂等；FlowModel 自动计 Agent 每轮。

输入按唯一 File ID 计额，产物上传前按稳定 key、元数据和实际摘要预留；失败/未知不退款，原键恢复不双扣，读取不重复计文件量。

workspace 返回仅作用域内有效的 Path；run_process 只写当前 Task 活跃工作区且不超过 output_limit，子 Task 不继承许可。无文件输出使用有界 stdout/stderr，退出前完成进程、I/O 及清理。以下复制 Step 的 argv 来自可信代码：

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

此例最坏同时保留三个 16 MiB 临时副本/三个传输槽，定义至少 temporary_bytes=48 MiB、file_count=2、file_bytes=32 MiB、tool_calls=1、iterations=1。宿主磁盘限制同时生效；嵌套同 Task 复用一个操作许可，子 Task 独立申请，满额 flow_capacity_busy。

额外业务 I/O 由 Workflow 显式计工具额并持 execution_scope，Service 保持普通授权/事务。Context 不拦截任意 Python 自行写盘或创建的非托管 Task。

## 4. 工具摘要与按需读回

工具返回明确小型 DTO：status、summary、可信引用、计数、有界 preview/text、truncated；分段含 offset/next_offset。

inspect_document 返回摘要，read_document 仅接受本次输入，按 Unicode 字符偏移并双重限长。偏移可有界修正，身份/文件错误继续抛安全异常，不包装成成功摘要。

长日志或检测结果由业务存受控 File，工具只返回摘要和 ID，再有界读回；读取使用当前授权。普通工具 I/O 放 Step，不把整个 Agent 包成可重试大 Step，也不建设通用 ToolResult 表。

## 5. 验证

backend 目录执行：

```sh
uv run --locked pytest -q tests/unit/test_document_review.py tests/unit/test_flow_contracts.py
# 按 development.md 配置专用 keystone_test 数据库后：
uv run --locked pytest --require-postgres -q tests/integration/test_flow.py tests/integration/test_model_live.py tests/integration/test_review_recovery.py tests/integration/test_flow_extensions.py
```

单元验证业务验收、空白结果禁止上传、Agent 修正/预算和 Unicode 分段。集成用真实 Worker/PostgreSQL/DBOS 和本地 HTTP 替身，覆盖多角色绑定及登记、响应、产物、Agent 消费前的崩溃恢复；检查请求数、原 ID、预算、截止、引用和旧格式重放。不访问付费模型，不将协议回归作为业务质量证明。

## 运行诊断

控制台展示 Run、步骤、预算、模型、文件和费用；业务无需复制执行日志。接口和授权只在 [Flow 诊断](../domains/flow.md#8-api授权与诊断)维护，历史缺位置/预算明确显示未记录，不按当前定义补造。资源可见与 Run 可见分别判断，文件下载再次检查内容权。
