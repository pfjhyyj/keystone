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
├── workflow.py              # Agent、工具及 DBOS workflow/step
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
- accept_judgment 校验归属与计数；可公开的输出错误最多反馈模型修正一次，使用同一运行和原身份。
- save_report 上传前再次验收，使用稳定 owner/slot/request_key；协调器另按原 JSON Schema 验证最终结果和引用。

业务规则放自己的验收函数，output validator 不执行上传、审批或采用。授权失败/结果未知不触发模型修正重发。元数据校验不能证明判断准确，needs_attention 表示待复核，内容质量需真实人工样例验证。

## 3. 单次操作与执行观测

Workflow 使用普通 Python 组合 DBOS Step，不登记实际用量统计或预留工具次数。模型调用通过 FlowModel/FlowContext 使用原身份和稳定调用 ID；次数、token、状态和费用从实际调用记录统计，模型未知结果不自动重发。DBOS 历史记录步骤、工具执行及重试，业务日志使用 run_id、节点、状态和耗时关联。

工具和业务 Service 在每次新副作用前授权，发布时复核对象版本和原任务归属。模型输入/响应、工具返回 DTO、单文件和每个工作区仍须有界；文件引用与产物采用稳定键，不依赖额度记账实现幂等。

ctx.workspace(max_bytes=...) 返回统一临时目录，单文件使用 directory / "output" 等目录内路径。ctx.run_process(output=..., output_limit=...) 只允许当前 Task 活跃工作区内的文件路径。进程保持超时和输出上限，取消后等待子进程和原生 I/O 完成再清理。并发由实际资源和执行队列控制，不使用每 Run 操作许可。

Agent 示例保留单次输出参数、输出修正次数和流程超时，并设置 request_limit=100；不使用 None 关闭请求次数上限。工具仅读取已授权文件与有界片段，不返回任意路径或原始异常。

## 4. 工具摘要与按需读回

工具返回明确小型 DTO：status、summary、可信引用、计数、有界 preview/text、truncated；分段含 offset/next_offset。

inspect_document 返回摘要，read_document 仅接受本次输入，按 Unicode 字符偏移并双重限长。偏移可有界修正，身份/文件错误继续抛安全异常，不包装成成功摘要。

长日志或检测结果由业务存受控 File，工具只返回摘要和 ID，再有界读回；读取使用当前授权。普通工具 I/O 放 Step，不把整个 Agent 包成可重试大 Step，也不建设通用 ToolResult 表。

## 5. 验证

复用现有单元测试验证业务验收、输出修正及有界工具返回。真实 PostgreSQL/DBOS 集成验证授权、调用登记与取消收尾、原调用恢复、产物引用和新基线与 ORM 一致。统计断言来自实际调用记录，不维护第二份累计账本，不访问付费模型。

## 运行诊断

控制台独立加载运行步骤、关联模型调用、文件用途及费用。失去运行或资源权限后清空对应详情，迟到结果不覆盖当前代次；接口与授权见 Flow 领域文档。

FlowDefinition.queue 默认 keystone-flows；额外队列在 composition.FLOW_QUEUES 中用现有 QueueDefinition 声明。队列名固定到 Run 快照，API 与 Worker 使用同一声明。timeout_seconds 默认 3600，范围 1～86400，不接受 None。
