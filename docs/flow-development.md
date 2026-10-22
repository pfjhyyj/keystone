# 编写业务 Flow

可复用示例位于 `backend/src/keystone/examples/document_review/`。它使用现有
Pydantic AI、Harness、DBOS、文件服务和 ModelRuntime，不引入额外执行框架或业务表。
集成测试通过 `backend/tests/support/flow_fixture/` 直接执行这份代码，不维护第二份示例。

## 1. 接入与目录

```text
document_review/
├── __init__.py
├── contracts.py             # 轻量输入输出、工具结果 DTO、业务验收与 Definition
├── workflow.py              # 预算、Agent、工具及 DBOS workflow/step
└── skills/review-text/SKILL.md
```

默认流程目录仍为空。若要在开发环境试运行，在 `composition.py` 中显式接入：

```python
from keystone.examples.document_review.contracts import definition as document_review

FLOW_DEFINITIONS = (document_review,)

def register_workflows():
    from keystone.examples.document_review import workflow  # noqa: F401
```

API 只加载 `contracts.py`，Worker 加载 `workflow.py`。启动方式沿用 README；在控制台
创建可调用的真实模型、上传 UTF-8 文本，再以文件 ID 和模型 key 发起 `document.review`。
实际模型请求可能计费。发起权限包含 `flow:start` 和 Definition 声明的能力；查询及取消
分别需要原有权限。文件内容访问仍逐对象检查，文件引用不授予访问权。

正式业务复制该目录后修改业务标识、入口名称、契约、工具及 Skill，在 composition 中注册
自己的契约和入口。若放入独立顶层 Python 包，同时在 `EXECUTION_PACKAGES` 声明包和
必需资源；不要扫描目录或在线安装代码。提示词与 Skills 必须随执行制品发布。

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

示例的局部 `ReviewBudget` 仅服务该流程，不是平台 Agent 注册或管理系统。

| 限制 | 示例值 | 执行位置 |
|---|---:|---|
| 模型请求次数，包含输出修正 | 4 | Pydantic AI UsageLimits |
| 成功工具调用次数 | 3 | Pydantic AI UsageLimits |
| 累计输入与输出 token | 16000 | 按模型响应报告的 usage 检查 |
| 每次请求的输出 token 上限 | 2048 | model_settings，经 FlowModel 检查 |
| 输出修正次数 | 1 | Agent retries |
| 流程时限 | 180 秒 | Definition/DBOS 与 workflow 局部 timeout |
| 源文本 | 16 KiB UTF-8 | 文件读取边界 |
| 首次预览 / 单次分段读取 | 512 / 2048 字符 | 工具 DTO 与读取边界 |

FlowModel 默认输出上限仍为 1024，允许可信业务代码设置 1～4096 的整数；越界或无效值
在发送前拒绝。不从公开 Flow 输入接收预算，不接受模型自行提高额度。
总 token 限额依赖上游报告、在响应后检查，不是精确预付费或金额上限；费用事实仍由
ModelInvocation 和原查费机制维护，不按 token 估算成已确认费用。

示例只有一个 Agent，UsageLimits 约束其完整循环。模型/工具尝试及修正共用预算；业务
不得捕获预算异常后创建新的 Agent 循环继续尝试。多个 Agent 的业务先按步骤预分配额度，
合计控制在流程预算内；当前示例不提供跨 Agent 的共享计数器或新账本。

局部 timeout 在恢复时重新进入；跨恢复的执行超时由既有 DBOS workflow timeout 管理，
不能仅靠局部计时器。DBOS 在首次出队执行时确定截止时间，排队等待不计入执行时限，
恢复沿用原截止时间。DBOSDurability 重放已完成模型步骤，现有稳定调用 ID 与发送登记
继续阻止恢复时重复计费；模型响应未知或缺少可恢复结果时停止，不另起循环。

安全错误码：`flow_budget_exceeded`（请求/工具/观测 token 预算耗尽）、`flow_timeout`
（局部流程超时）、`flow_invalid_output`（输出不合契约或修正耗尽）。DBOS 自身取消/超时
仍按原引擎状态投影。向外不返回供应商正文、异常堆栈和原始模型消息。

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
uv run --locked pytest --require-postgres -q tests/integration/test_flow.py tests/integration/test_model_live.py tests/integration/test_review_recovery.py
```

单元检查覆盖错误源/计数及空白摘要禁止上传、真实 Agent 修正与预算、Unicode 摘要和
分段边界、输出 token 上限与安全失败。原 Flow 集成检查继续验证 Worker/SDK 链路、
逐轮调用记录，以及登记、模型响应、产物上传后的崩溃恢复。test_review_recovery.py 还在
模型检查点已完成、Agent 尚未消费响应的位置中断进程，验证恢复后请求、工具、token、
输出修正预算与原截止时间继续生效，且不会多发请求或生成报告。测试使用确定性上游替身或
本机协议服务，不访问真实模型，也不将这些结果视为业务质量评测。
