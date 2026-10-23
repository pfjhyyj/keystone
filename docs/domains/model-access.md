# Keystone 模型接入领域

自动费用核对已接入 Worker 和 DBOS 定时计划，职责见[整体架构第 3 节](../architecture.md#3-api-与-worker-的分工)。本模块拥有待核对状态、渠道查询和幂等保存，不依赖 FlowRun，也不重发模型请求。[第 3.6 节](#36-自动费用核对)说明 Yunwu/Openlux 分页查费及 OpenRouter 请求 ID 补查规则。支持同宿主同版本的固定 Worker，查询预算和分页跨进程共享，启动只收尾本槽位旧实例的模型调用。

adapters/billing/、adapters/inference/ 和 Service 已按职责分层，费用任务的 DBOS 接入位于 model_access/tasks/。实际结构、依赖边界和原文件迁移映射见[第 8 节](#8-代码结构与职责映射)。模型调用、自动查费和原身份权限规则保持一致；跨宿主及自动接管未实现。

[范围与边界](#1-范围与边界) · [领域模型](#2-领域模型) · [主要流程](#3-主要流程) · [权限与审计](#4-权限与审计) · [设计取舍](#5-设计取舍) · [待确定事项](#6-待确定事项) · [当前实现](#7-当前实现约定) · [代码结构](#8-代码结构与职责映射)

## 1. 范围与边界

为后续业务提供统一的模型调用入口，记录每次调用的用量、上游报告的费用，并展示渠道账户的余额。采用“成熟调用 SDK＋少量渠道适配代码”，不要求独立网关。

当前实际发送开放 OpenRouter、Openlux 和 Yunwu，复用 OpenAI SDK 的文本对话及流式协议。OpenRouter 可保存响应费用并按请求 ID 补查，Openlux/Yunwu 使用原 Token 分页核对日志。DeepSeek、阿里云百炼保留适配声明，尚未开放发送。阿里云暂按百炼/DashScope 按量付费服务设计，不把订阅额度混为账户余额。图片查费样本用于核对账务协议，Keystone 本阶段未增加图片生成入口。

只保留渠道、模型配置、调用记录三个对象。不做用户钱包、充值、配额分配、计费倍率、价格表、自动渠道切换或智能路由。这里记录的是供应商侧成本，不是向 Keystone 用户收费。

## 2. 领域模型

| 对象 | 职责 |
|---|---|
| Channel（渠道） | 配置如何连接某个上游账户，保存最近查询到的余额 |
| ModelEntry（模型配置） | 给业务一个稳定的模型 key，映射到某个渠道的上游模型 |
| ModelInvocation（调用记录） | 记录一次本地调用尝试的归属、结果、用量和可获得的费用；不证明上游已收到请求 |

```mermaid
erDiagram
    Channel ||--o{ ModelEntry : provides
    Channel ||--o{ ModelInvocation : receives
    ModelEntry ||--o{ ModelInvocation : records
```

采用三张表。余额快照和用量是结构化字段，不单独建表；渠道适配器是代码实现，不是领域实体。

### 2.1 渠道 Channel

| 字段 | 含义 |
|---|---|
| `id`、`name` | 稳定 ID、展示名 |
| `provider` | `deepseek`、`openrouter`、`aliyun_bailian`、`openlux`、`yunwu`，决定适配实现 |
| `api_base` | 经管理员配置的服务地址，包含相应地域/站点差异 |
| `api_key_encrypted` | 推理 API Key 密文，接口不返回 |
| `billing_api_key_encrypted` | 可空，账务 API Key 密文，接口不返回 |
| `upstream_account_ref` | 上游账户的非秘密标识，用于说明余额归属；无法获得时可空 |
| `is_active` | 是否允许新的模型调用 |
| `balance_snapshot` | 可空，最近成功查询的余额结构 |
| `balance_checked_at` | 最近成功采集余额的时间 |
| `balance_last_attempt_at`、`balance_check_status` | 最近查询时间及结果：`never`、`ok`、`error`、`unavailable` |
| `created_at` | 创建时间，服务端生成，UTC，必填 |
| `created_by` | 创建操作的用户 ID，系统操作可为空 |
| `updated_at` | 渠道配置最近修改时间，UTC，创建时与 created_at 相同 |
| `updated_by` | 渠道配置最近修改操作的用户 ID，系统操作可为空，创建时与 created_by 相同 |

`balance_snapshot` 包含 `scope`、`items`；每项包含 `kind`、十进制 `amount`、`unit`。`scope` 说明是供应商账户或阿里云账户；`kind` 区分总余额、现金、赠送等，`unit` 明确为 CNY、USD 或供应商 credits 等单位。来源由渠道适配实现确定。

各渠道字段映射见本页[渠道能力](#33-渠道能力)。不同币种和余额分项不直接相加。成功刷新原子替换快照与采集时间；失败只更新本次查询结果，不清空旧余额。未查询/不可用不能显示成零。

保留旧余额仅适用于同一查询配置下的失败。影响余额查询来源的配置（地址、账务凭据，或余额适配实际使用的推理凭据等）变更时，同事务清空余额快照与成功/尝试时间，将 balance_check_status 重置为 never；只改名称或启停不清空。provider 和账户归属创建后固定，不能通过更改 upstream_account_ref 将原渠道改作其他账户；该标识未知时可补录同一账户标识。替换秘密也必须保持同一账户，无法确认时新建渠道。

updated_at、updated_by 只追踪渠道配置变更；余额刷新使用 balance_checked_at、balance_last_attempt_at 表达，不覆盖最后的配置修改人。

余额刷新保留同渠道会话锁。快照、查询结果与审计写回使用持锁连接上的短事务，外部 HTTP 不保持业务事务；连接丢失后禁止换连接提交，即使已收到余额响应也必须回滚，保留原快照。不能以一次写前检查代替持锁连接内的原子提交；查费的真实 Token 范围锁另按第 3.6 节处理。

同一供应商可以配置多个渠道。首版不建立独立供应商账户或余额流水表，也不计算跨渠道余额总和。允许同账户内轮换密钥；换供应商、换账户应新建渠道，避免历史调用与余额归属混淆。余额查询配置不完整时仍可保留模型调用能力。

自动查费遵守适配器声明的查询范围：账户相同不代表不同 Token 能互查日志。Yunwu/Openlux 的配置接口禁止更换 Token 或站点，返回 channel_query_scope_fixed；相同 Key 重存、名称和启停修改不受影响。换 Token 必须新建 Channel，旧渠道保留核对凭据。OpenRouter 凭据变更会产生新范围，无法验证的历史待办阻塞，不能自动转交新 Key。

### 2.2 模型配置 ModelEntry

| 字段 | 含义 |
|---|---|
| `id` | 稳定 ID |
| `key` | 业务使用的唯一 key，创建后固定，例如 `general-chat` |
| `channel_id` | 所属渠道 |
| `provider_model` | 上游接受的模型标识 |
| `is_active` | 是否允许新调用 |
| `created_at` | 创建时间，服务端生成，UTC，必填 |
| `created_by` | 创建操作的用户 ID，系统操作可为空 |
| `updated_at` | 最近修改时间，UTC，创建时与 created_at 相同 |
| `updated_by` | 最近修改操作的用户 ID，系统操作可为空，创建时与 created_by 相同 |

一条配置只映射一个渠道和一个上游模型。界面直接展示 key，不单独保存模型配置的展示名。业务不接触渠道密钥；映射修改不需要业务改 key，但模型能力和行为变化仍需业务验证。历史调用保存当时的渠道和模型，不能通过当前配置反推。

普通直接调用在本次尝试开始时解析 key；Flow 在受理时固定模型选择，映射修改只影响之后受理的运行，规则见[固定模型选择](#34-flow-的固定模型选择)。

首版不引入模型家族、模型版本、路由池、价格方案或通用能力注册表。模型与渠道都启用才允许发起新调用；停用不修改已有调用记录。

Channel、ModelEntry 首版都只停用、不删除，统一使用已有 is_active，不增加 deleted 状态、deleted_at 或回收站。停用 Channel 阻止该渠道的新调用，但不批量修改其 ModelEntry；重新启用渠道后，仅模型配置自身也启用时才能调用。单独停用 ModelEntry 只阻断该配置的新调用。

停用配置仍可由有权限的管理者查询、修改或重新启用；ModelEntry.key 继续被原配置占用，不回收给另一条配置。允许误建配置以停用记录留存，省去物理删除与引用清理分支。旧 Run 仍关联原配置，但原模型或渠道停用时明确拒绝新调用；已发送请求继续保存结果、用量和费用，历史数据不改写。

停用是 Keystone 的调用控制，不等于撤销供应商密钥，也不自动清除已保存的渠道凭据。管理员可在控制台替换 API Key、清除可选账务 Key；供应商侧的撤销由操作者处理，配置与历史记录继续保留。

### 2.3 调用记录 ModelInvocation

| 字段 | 含义 |
|---|---|
| `id` | 本地调用尝试 ID；Flow 内每次持久模型请求使用可在恢复时复现的稳定 ID |
| `user_id` | 发起用户；系统调用可空，由可信服务端确定 |
| `flow_context` | 可空的小型 JSON：flow_run_id、node_key、agent_key、agent_call_id，按实际调用场景填写；普通调用为空 |
| `model_entry_id`、`channel_id` | 本次使用的模型配置与实际渠道 |
| `provider_model` | 本次上游模型标识快照 |
| `upstream_request_id` | 上游返回的请求/generation ID；未收到时可空 |
| `status` | `running`、`succeeded`、`failed`、`unknown` |
| `finished_at` | 可空，本地调用处理结束时间，开始时间使用 `created_at` |
| `usage` | 可空的结构化用量，保存上游提供的输入、输出、缓存等计量信息 |
| `cost_amount`、`cost_unit` | 可空，上游报告的本次账户费用及对应单位 |
| `cost_source` | `response`、`query` 或 `unknown` |
| `cost_note`、`error_code` | 可空，费用缺失/补查失败的简短原因及脱敏错误分类 |
| `created_at` | 本地调用尝试登记时间，服务端生成，UTC，必填；不是精确网络发送时间 |
| `updated_at` | 状态、用量或费用最近补全时间，UTC，创建时与 created_at 相同 |

`running` 表示本地尝试尚未结束，不证明请求已经发送或被上游接受；`succeeded` 表示收到正常完成结果，`failed` 表示已知失败，`unknown` 表示无法确认是否已发送或上游是否完成。状态都不代表是否收费。进程中断遗留的 running 只在确认执行已结束后标记未知，不能仅因经过一段时间就把仍在执行的请求改成 unknown；未知结果不自动重发。

`usage` 缺失的分项保持缺失；缓存、推理 token 可能是其他分项的子集，不重复求和。费用已知时必须同时有金额、单位和来源；未知时金额和单位为空、来源为 `unknown`，原因可写“上游未提供”或“等待查询”。

费用补查只补全同一记录，不新增调用、不累加原金额；失败保留已知费用。费用来源表示上游报告口径，不保证与最终发票、税费、退款或外部 BYOK 账单完全一致。首版不建立账单对账或结算状态机。

原记录内保存费用核对状态、调度信息、原查询范围及必要证据，仍是调用事实记录。调用结果与费用核对状态彼此独立，字段、迁移边界和保存规则见[自动费用核对](#36-自动费用核对)。

一条记录对应一次本地调用尝试，不等于一次聊天会话或一个业务任务，也不证明上游实际收到请求。对同一尝试的结果补全以本地 ID 更新；历史渠道/模型归属不因配置变更而改变。调用用户归属由 user_id 表达，Flow 关联由 flow_context 表达，状态和费用由服务端补全，不增加 created_by、updated_by。updated_at 用于判断记录最近是否得到补全，不表示一次人工编辑。

Agent 一次运行可能发起多次模型请求，每次实际请求都独立记录，不能只在 agent.run() 外面记一条总调用。flow_context 由可信执行上下文生成，只用于追踪，不是权限凭据，也不保存消息或 SDK 对象；相同 Agent 调用中的多轮请求共用 agent_call_id，但各有自己的调用记录 ID。普通业务按需在自身数据中保存调用 ID，模型接入模块不反向保存通用业务关联号。

## 3. 主要流程

### 3.1 SDK 与适配器

业务通过稳定的模型配置 key 发起调用，模块解析渠道和上游模型，再交给 SDK。

渠道适配代码仅处理必要差异：

- 从响应中提取请求 ID、用量和上游报告的费用。
- 支持时，按上游请求 ID 查询费用，补全已有记录。
- 使用渠道对应接口查询账户余额。

不支持的能力直接返回“不可用/不支持”，不为了统一接口编造金额。沿用普通适配类和显式 provider 注册，cost_query_mode 区分请求直查、日志分页和不支持；不增加插件系统或独立账务服务。OpenAI 兼容只说明推理格式相似，不能据此推断查费路径、凭据范围或金额单位相同。

调用协议位于 adapters/inference/、费用和余额协议位于 adapters/billing/，Service 保留业务用例、权限与事实保存。多个中转站复用调用协议、分别选择账务适配；adapters/billing/ 不再复制一套 Controller/Service/Model，也不直接持久化费用。详细边界见第 8 节。

### 3.2 调用与余额刷新

1. 校验输入、当前身份、调用权限及模型/渠道启用状态，在即将发送前创建并提交调用记录；记录失败则不发起上游请求。Flow 还需遵守下文的原子登记与防重发规则。
2. 发起请求，保存上游请求 ID、用量、结果状态和可获得的费用。流式请求在流结束后更新，缺失末尾用量时不补造数据。
3. 可信响应费用直接确认；缺少费用且具有原查询范围及合法请求 ID 时，同事务保存 pending，Worker 后续自动核对。授权管理者也可显式补查，复用同一服务。没有请求 ID 或不支持查询时保留未知原因。
4. 有管理权限的用户可手动刷新渠道余额；API 直接调用本模块，在有界超时内查询并保存结果，不返回需要 Worker 接续的后台任务。不在每次模型调用前后查询余额，首版不安排定时同步。

每次实际发往上游的尝试必须对应一条记录，但记录可能在发送前的中断窗口内已创建，不能反过来把每条记录都算成上游已接受请求。首版关闭 SDK 的自动重试；收到响应后记录更新失败也不能触发模型重试。以后若允许新的重试尝试，必须逐次使用不同 ID 记录，不能覆盖原尝试；Flow 的同一请求恢复不属于新的获准尝试。Flow 调用通过已有 flow_context 关联，普通业务自行保存相关调用 ID，不新增通用业务关联号字段。

超时、断流或进程中断可能无法确认上游结果，记为 `unknown`，不能推断没有收费。硬退出或结果写回失败可暂时遗留 running，只有确认原执行已结束后才能收尾；普通 API 调用使用下述离线命令，Flow 调用由持有排他锁的 Worker 启动恢复。调用结果和费用是否已知相互独立；补查失败不改变已经成功的调用结果。调用记录不是持久化任务或业务会话。

请求发出后用户停用、权限收回或原令牌失效，不阻断该次调用的响应处理和记录补全。已有尝试的状态、用量和费用继续由可信执行上下文或受限系统维护补齐，user_id 仍保留原始归属；费用补查只核实原请求，不重新推理。撤权后的新请求仍拒绝，用户查询记录也必须通过当前权限检查。

余额刷新失败时保留上次成功快照，显示采集时间和本次失败状态。余额只是有时间戳的上游观察值，不用于保证下一次调用一定成功，也不用余额前后差计算单次费用。多个渠道共享同一上游账户时不能重复汇总，首版仅逐渠道展示。

余额查询分为读取当前配置、事务外请求、短事务写回三段；同渠道刷新串行协调，并与影响查询的配置变更协调。写回前确认仍对应原查询配置，过期结果丢弃，不能覆盖新配置的快照；不为此增加刷新请求表、任务状态或版本字段。API 中断时不承诺请求已完成，保持最后已确认信息，可再次手动查询。渠道停用只禁止新推理，不禁止授权管理者查询历史、补查费用或刷新余额。已检查的 Ether 模型连接模块没有对应的渠道余额刷新实现，这部分是 Keystone 的简化规则，不声称直接迁移现成代码。

### 3.3 渠道能力

OpenRouter 等官方能力依据下列文档；Yunwu/Openlux 另有 EPStudio 实测依据。Keystone 回归使用隔离数据库和本地协议桩，没有发送付费模型请求。

| 渠道 | 单次调用费用 | 账户余额 | 首版处理 |
|---|---|---|---|
| DeepSeek | 已核对的对话响应提供用量，未提供单次金额 | `/user/balance` 提供分币种的总余额、赠送余额和充值余额 | 保存用量；费用未知；支持余额刷新 |
| OpenRouter | 响应 `usage.cost`；也可按 generation ID 查询 | `/api/v1/credits` 返回累计 credits 和使用量，需要 management key | 保存上游报告费用；必要时查询补全；支持余额刷新 |
| 阿里云百炼 | 尚未确认可按推理请求 ID 查询单次实际金额的接口 | `QueryAccountBalance` 查询阿里云账户，区分可用额度和现金余额 | 保存用量；费用未知；有账务凭据时支持账户余额刷新 |
| Yunwu/Openlux | 原 Token 分页拉取日志，本地精确匹配 `other.request_id` 与 `x-api-request-id`，按站点口径解析 quota | 不提供余额查询，Token 用量不视为账户余额 | 已接入自动核对和手动补查；详见第 3.6 节 |

DeepSeek 保留接口返回的币种，总余额已经包含赠送和充值部分，不重复相加。费用未知仅表示当前接入能力不足，不断言供应商永远没有相应能力。依据：[余额接口](https://api-docs.deepseek.com/api/get-user-balance/)、[对话响应](https://api-docs.deepseek.com/api/create-chat-completion/)。

OpenRouter 的 `usage.cost` 表示该账户被收取的 credits；流式用量在最后的 SSE 消息中。不要用 `upstream_inference_cost` 替换账户费用，尤其 BYOK 还涉及外部供应商收费。余额由 `total_credits - total_usage` 得到，保留 credits 口径，不能当作某个 API Key 的独享额度。依据：[用量与费用](https://openrouter.ai/docs/cookbook/administration/usage-accounting)、[余额与凭据要求](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits)。

阿里云的 `AvailableAmount` 是可用额度，`AvailableCashAmount` 是现金余额，展示时分别标明。它们属于阿里云账户，可能同时供其他云产品消费，并非百炼专属余额。查询需要阿里云 OpenAPI 账务授权，不能直接使用 DashScope API Key。依据：[账户余额接口](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-queryaccountbalance)。

百炼账单存在出账延迟和聚合口径，出账也不等于即时扣款。首版不抓取控制台、不把聚合账单摊分成单次“实扣”；后续确认能够精确关联请求的官方数据后再补全。依据：[账单与费用管理](https://help.aliyun.com/zh/model-studio/bill-query-and-cost-management)。

### 3.4 Flow 的固定模型选择

用户提交 model_key，Flow 受理时由服务端解析为 `model_entry_id`、`channel_id`、`provider_model`、`api_base`，作为明确的模型选择值存入已有 FlowRun.input。这是小型的非秘密选择快照，不新增 FlowRun 字段或模型版本表；流程输入契约及受理代码必须明确其位置与转换规则，不扫描任意 JSON 猜测模型字段，也不接受客户端伪造解析结果。

同一 Run 始终使用保存的选择，不在每次执行或恢复时重新解析当前 key 映射。每次真正发送前仍检查当前用户、原个人令牌、原 ModelEntry 与原 Channel 是否有效；原配置停用或不存在则明确拒绝。ModelEntry 后续映射变化不替换旧 Run 的目标，旧目标仍有效时可继续。

允许原渠道在同一账户内轮换凭据，发送时读取当前秘密。原渠道 api_base 与保存值不一致时拒绝发送，不能静默改用新地址；更换供应商或账户必须新建 Channel。ModelInvocation 记录这次实际采用的渠道和模型。

Token 级查费适配固定原查询范围，换 Token 按第 2.1 节新建 Channel。已有 Run 的固定选择不改到新渠道，不能为完成旧查费而放宽 Flow 的新请求授权或目标校验。

Flow 的 request_digest 依据用户原始提交计算，不包含服务端解析结果。重复发起先按原请求检查幂等并返回原 Run，不因当前映射变化而重新解析或改写其 input。

### 3.5 Flow 模型请求的恢复与防重发

复用 ModelInvocation.id 作为每次持久模型请求的稳定标识，由执行上下文确定，恢复同一请求时必须得到同一 ID。不能只使用 agent_call_id：一次 Agent 调用中的多轮模型请求各有自己的 ID。

1. DBOS 已有可恢复的模型结果检查点时，直接复用结果，不新增调用记录、不再次发送。
2. 需要执行请求时，在发送边界原子创建 ModelInvocation，利用 ID 唯一约束取得首次发送资格。只有首次成功登记的执行者可以发送；冲突或不确定是否登记成功都不能取得发送资格。
3. 已有调用记录但没有可恢复的结果时，停止该次执行并报告明确错误，例如 `model_result_unavailable`；即使调用状态为 succeeded 也不能重发，因为费用记录不能替代丢失的模型响应。Flow 的恢复失败不覆盖已经确认的调用成功状态。

该规则宁可在“已登记、尚未发出”时中断，也不在恢复时猜测可以重发。可靠的上游查询可以核实原请求结果或费用，但不能借查询重新发起推理。首版不增加发送意图表、额外状态或保存模型响应的字段，也不使用 File 保存防重发标记。

这是 Keystone 复用现有调用表的设计选择，并非声称 Ether 已有同样实现，也不等于供应商侧 exactly-once 保证。实现时必须验证稳定请求身份、原子登记以及 DBOS/Agent 内部恢复边界；仍用于恢复防重发的调用记录不能提前清理。

### 3.6 自动费用核对

本节对应当前实现。复用 ModelInvocation、Channel、现有 Worker 和 DBOS，不新增业务 Flow、通用 JobRun、账本或独立查费进程。费用是上游报告的账户消耗；不根据 token 数、公开价格或账户余额差估计单次实扣。

#### 3.6.1 通用适配与结果分类

| 适配能力 | 渠道适配器负责 | 公共核对服务负责 |
|---|---|---|
| 响应直接提供费用 | 提取明确的账户扣费、单位及可信编号，声明其费用口径 | 保存可信费用并确认，通常无需补查 |
| 请求 ID 直查 | 查询路径、参数、鉴权、返回编号校验、金额解析，例如 OpenRouter | 按原调用查询和幂等补全 |
| 日志列表查询 | 时间筛选、分页、凭据可见范围及日志解析，例如 Yunwu/Openlux | 按原查询范围合并拉取，逐条精确匹配 |
| 不支持查询 | 明确能力边界 | 保留未知原因并停止自动查询，不编造金额 |

适配器返回已找到、尚未生成、暂时失败、需要修复配置或不支持等明确结果；保留安全原因码和有效 Retry-After。HTTP 200 还须校验业务 success、响应结构和记录内容；404 只有在已确认的查询契约明确表示“该请求尚无记录”时才归为等待，错误路径不能伪装成未出账。端点、方法、鉴权位置和编号来源由代码显式声明，不接受客户端任意导入路径、URL 或解析脚本。

#### 3.6.2 Yunwu/Openlux 的已验证契约

依据为 2026-10-21 EPStudio 的实际图片调用及只读查费探针；2026-10-22 复核了站点公开金额配置。Keystone 使用该协议实现并通过本地回归；上游真实出账时效和大流量容量仍需部署后观测。

该渠道实际使用 `https://api.openlux.ai`。已验证查询如下，路径从受控账务 origin 构造，不能直接拼在推理 `/v1` 后面：

```http
GET https://api.openlux.ai/api/log/token?key=<原调用API_KEY>&start_timestamp=<开始秒>&end_timestamp=<结束秒>&p=1&page_size=20
```

| 项目 | 已验证行为及约束 |
|---|---|
| 鉴权与范围 | key 查询参数可查日志，仅用 Bearer Header 的探针未成功；按原 Token 查询，不假定同账户另一枚 Key 可见旧日志 |
| 返回与分页 | 读取 data.items 和分页信息；p 从 1 开始，page_size、时间范围可用，other 可能为 JSON 字符串 |
| 请求编号 | 日志 other.request_id 精确匹配响应头 x-api-request-id；同次响应的 x-oneapi-request-id 是另一编号，不能直接替代 |
| request_id 参数 | 传入不存在的 request_id 仍返回普通日志列表，不能依赖该参数完成服务端精确筛选 |
| 日志 id | 同条日志随分页/页大小出现不同 id，不用作稳定账单编号、递增水位或幂等键 |
| 扣费依据 | 读取明确的消费记录及其 quota、单位口径；不从日志 content 中的模型倍率重新估价 |

适配器使用 site、原查询范围和查费 request ID 确定匹配对象；时间、模型和组等元数据只用于缩小范围或校验，不能通过相近 token 数、时间或“只有一条记录”代替请求 ID。重复页只幂等补全同一调用；多条记录互相矛盾，或同一外部请求已绑定另一条调用时，转为阻塞，不擅自求和或重复计费。

样本为 `quota=1328`、`quota_per_unit=500000`、`currency_type=USD`，得到 `0.002656 USD`。金额按 Decimal 精确计算；样本中的人民币换算只是展示参考，不是实际充值成本。复核时 [Openlux 公开配置](https://api.openlux.ai/api/status)为 USD，而[旧 Yunwu 公开配置](https://yunwu.ai/api/status)为自定义额度，不能统一视为美元。原站点和换算口径须随调用保存；历史口径不明时保留额度证据和未知原因，不套用当前配置重解释旧费用。旧 Yunwu 和 Openlux 不通过修改原 Channel 地址混用历史。

实现只允许 Openlux `https://api.openlux.ai/v1`、Yunwu `https://yunwu.ai/v1`。调用保存适配器口径；每轮日志查询先读取公开 `/api/status` 校验 quota_per_unit=500000、对应币种。Openlux 为 USD，Yunwu 要求 CUSTOM 且 custom_currency_exchange_rate=1，落库单位为 yunwu_credits。变化时 blocked/cost_policy_changed，不用新口径解释旧调用。只解析 type=2、非负整数 quota，并校验匹配日志的模型名。

#### 3.6.3 回执、查询范围与凭据

回执观测由适配器明确选出可查费编号，尽早保存到调用记录；可保留少量其他响应编号供诊断，不能被响应体模型 id 无条件覆盖。Yunwu/Openlux 优先使用已验证的 x-api-request-id，OpenRouter 保留其 generation ID 规则。没有合法编号时费用保持未知，不生成猜测编号；硬中断发生在编号持久化之前时不能承诺自动找回费用。

查询范围由实际账务站点和原凭据可见范围确定。原 Token 与其他 Token 不能只因账户相同就混查；首版换 Token 新建 Channel，旧渠道保留必要密文凭据。范围未变的轮换仍须可验证，无法确认则阻塞，不静默替换历史范围。相同真实 Token 重复配置为多个 Channel 时也属于同一协调范围，不能靠不同 channel_id 绕过防重或限流。

任务参数只包含调用、渠道、范围标识和受控分页位置，不包含秘密或完整日志。凭据在受控服务内执行时解密，查询前后复核原范围和配置；变化后的旧结果丢弃。key 位于 URL 查询串时，日志、HTTP 异常、追踪和 DBOS 保存的错误均须脱敏，不保存完整 URL 或上游原始错误；只保存费用所需的规范化证据，不保存日志里的账号、IP 或完整 content。只访问显式允许的 HTTPS origin、路径和方法，不自动跳转。

#### 3.6.4 调度与分页推进

调用结束的短事务同时保存回执和费用待办，API 不负责即时投递。只有已结束且费用核对 pending、next_check_at 到期的记录可被扫描；运行中的调用沿用原收尾机制，不能为了查费按时间改判 unknown。调用 succeeded、failed、unknown 均可能产生费用。

model_access/tasks/workflows.py 由 composition 显式装配：启动投递一次，此后 DBOS 每 5 秒扫描；关闭 Flow 不关闭该能力。稳定计划名为 keystone.model_access.reconcile_costs。扫描使用 keystone-model-maintenance 串行队列，每轮最多投递 20 个到期范围；外部查询使用 keystone-system，默认全部 Worker 与 API 手动入口共享查费并发 2，同一查询范围串行，并与推理容量隔离。Worker 仅提供 DBOS 执行包装，费用扫描和核对规则由本模块 Service 提供，计划与容量语义保持不变。

请求直查按调用处理；日志查询按原 Token 范围和相近时间窗口合并，单页可确认多次调用。每次执行限制页数和总耗时，单次 HTTP 使用明确超时；未完成则保存固定起止窗口、下一页和本轮匹配范围，后续继续。该小型 checkpoint 归原 Channel，费用和进度协调写入，不新增分页任务表。下一页只在本页匹配结果提交后推进，崩溃后可安全重读同一页。

当前每批最多 20 条调用、每页 20 条日志、每次最多 2 页/25 秒，单次 HTTP 最长 10 秒。窗口从本批最早登记时间前 120 秒到本轮开始时间；后续分页不修改边界。未扫完约 5 秒后继续，完整覆盖后才确认费用。相同站点/Token 通过服务端 HMAC 生成不含秘密的 scope，选最早创建的 Channel 保存共享 checkpoint、阻塞和冷却；API 与 Worker 共用 PostgreSQL session advisory lock，查询期间不持有业务事务。

每进程最多 2 个查费流程，取得数据库协调连接之前即受限，避免大量手动补查占满连接池。日志响应上限 256 KiB、公开口径响应上限 1 MiB；只保存有界请求 ID、模型名、额度及单位等规范化证据，拒绝过深 JSON 或异常结构。

分页未完成只表示本轮未覆盖完，不判定未找到，不消耗一次完整“未找到”核对轮次。完成一轮仍未匹配时，才安排下一轮退避。后续轮次从窗口起点重新扫描，允许重叠以捕获延迟写入或分页移动的记录；不能仅凭最后一个日志 id 或最高时间永久跳过旧窗口。新待办不能不断改写正在扫描的窗口，扫描顺序须避免少数渠道或历史大窗口饿死后续待办。

同一执行重复投递沿用稳定 ID，运行中的同范围任务按范围及 execution_version 合并；下一轮使用新执行 ID，避免复用过期查询检查点。API 退出、Worker 重启或技术任务丢失后仍可从领域待办继续发现；DBOS 恢复可能重复只读 HTTP，因此费用写入和进度推进都须幂等。扫描器不占住受限队列等待自身派生查询完成。

#### 3.6.5 等待与重试

以下为当前默认值，不是供应商的账单时效承诺。自动窗口从调用结束起计 24 小时；停机不重新起算。首次约在结束后 10 秒到期，完整扫描仍未找到时依次约在结束后 30 秒、2 分钟、10 分钟、1 小时、6 小时和 24 小时核对，调度粒度允许数秒延迟。

| 结果 | 处理 |
|---|---|
| 找到唯一可信扣费 | 保存 confirmed，清除 next_check_at，停止自动轮询 |
| 本轮完整扫描无匹配 | 保持 pending，按上述时间安排下一轮；空列表不代表零费用 |
| 超时、连接失败、5xx | 保存暂时失败原因并有界退避，可从 30 秒逐步增至最长 1 小时并加少量抖动；等待不持有 workflow/数据库锁 |
| 429 | 优先遵守有效 Retry-After，持久化整个查询范围的下次允许时间，不逐条待办继续冲击同一 Token |
| 401/403、缺少凭据、范围不匹配 | blocked，暂停该范围，修复并确认仍属原范围后才重新触发 |
| 响应结构、金额或匹配冲突 | blocked，保留原已知费用和安全原因，等待人工核实或修复适配器 |
| 无可用编号或无查询能力 | unavailable，保留未知原因；不能无限自动重试 |
| 到达自动窗口或有界错误重试预算 | exhausted，保留未知及证据，停止自动查询，不标记免费 |

传输失败与未出账等待分开计数，暂时性错误最多自动重试 12 次，不含首次请求，并与 24 小时窗口共同约束；查询成功不重置累计错误预算。退避从 30 秒倍增至约 1 小时，附加 ±20% 抖动；有效 Retry-After 优先，支持秒数与 HTTP 日期。分页继续不重置截止时间，也不作为完整未找到轮次计数。24 小时是截止上限，不保证调度延迟后补跑最后一轮；迟于截止时间时直接 exhausted，晚于截止时间的 Retry-After 不引起越窗查询。SDK 不做隐藏重试；下一次业务查询由持久 cost_next_check_at 驱动，不为每笔费用建立长期 sleep 循环。所有重试仅查询账务，禁止重新推理。

手动补查保留现有 model:manage 和记录读取权限，与自动核对共用范围互斥、适配器和保存规则，不另建一条可绕过凭据范围的路径。日志查询超出同步预算时保存待办并返回尚未确认的状态，不在 API 请求中扫描无限页。对 blocked/exhausted 的重新触发由授权管理者显式执行并记录原因，不因普通页面轮询无限重置预算。

#### 3.6.6 费用状态与落库

费用字段、到期索引及发送归属直接包含在 model_access 基线迁移中，不再单独追加。内部 JSON 和范围不通过接口自动暴露。

| 归属 | 需要持久化的事实 |
|---|---|
| ModelInvocation | cost_check_status：pending、confirmed、blocked、unavailable、exhausted；cost_check_reason、cost_check_rounds、cost_check_errors、cost_checked_at、cost_next_check_at、cost_deadline_at，显式返回调用详情与列表 |
| ModelInvocation | cost_context 保存原站点/凭据范围和口径，receipt_ids 保存必要响应编号，cost_evidence 保存规范化额度、单位、口径和证据时间；三者为内部字段 |
| Channel | cost_scope、cost_context、cost_block_reason、cost_next_allowed_at、cost_checkpoint 为内部字段；凭据仍使用原受控密文，不进入 checkpoint |

cost_check_status 与调用 status 分开，查到账务不改变 succeeded/failed/unknown，也不把已结束 Flow 重新置为运行中。已知费用仍使用 cost_amount、cost_unit、cost_source；查询写入 source=query，明确可信的响应费用使用 response。未确认金额和单位保留 null，只有上游明确报告零消耗才记录 0。查费错误另存，不覆盖模型调用 error_code。

重复结果只补全同一调用，不创建新 ModelInvocation、不累加金额或用量。保留 Numeric(38,18) 和 Decimal，不能经过二进制浮点估价；按渠道及单位汇总，不跨旧 Yunwu 自定义额度、Openlux USD、OpenRouter credits 或人民币参考值相加。查询失败不清空已知金额；新的可信实际查询替换原响应费用时保留前后证据，矛盾记录不能静默覆盖。

外部 HTTP 在短业务事务之外；写回前检查原查询范围、配置、调用状态及本轮 checkpoint。费用、证据、进度和需要的审计同事务保存，旧查询或失去协调所有权的执行不得推进新轮次。常规自动确认保存在调用记录及必要证据中，不为每页或每次未找到复制管理审计；人工重启核对、异常覆盖等操作保留实际操作者和原因。

确认后停止自动轮询，本阶段不自动推断退款、负数费用、冲正或长期账单修订。不明历史口径、矛盾记录和窗口耗尽均保留未决；不能把已知费用汇总称为全部调用的最终实扣。

#### 3.6.7 多 Worker 的协调与恢复

查费允许重复只读查询，费用确认保持幂等。所有 Worker 和 API 手动补查通过 PostgreSQL 共享查询容量、真实 scope 会话锁、Channel 分页 checkpoint 和持久冷却。准备与写回在持锁连接的短事务内校验所有权，连接失效后禁止换连接提交。写回比较原配置、轮次和 checkpoint revision，每次推进递增 revision，防止旧页结果覆盖新进度；不同 Channel 指向同一真实 Token 时也不能并行推进同一范围。

DBOS 投递去重不代替上述保护，进程内 Semaphore 只控制本地资源。查询总容量默认 2，可通过 model_cost_concurrency 调整；拿不到容量时自动待办保留、手动入口返回忙碌，不消耗错误预算。Retry-After 在实例间共享，不能将预算乘 Worker 数。固定身份、按旧实例收尾及原身份重启恢复见[整体架构第 3.5 节](../architecture.md#35-多-worker恢复与发布)，不自动接管故障槽位。

#### 3.6.8 实施与验收边界

当前已实现回执选择及及时保存、分类查询结果、Yunwu/Openlux 账务适配、领域 CostService、费用状态和分页迁移；代码按第 8 节的 adapters/billing/adapters/inference/Service 分层，由 Worker 任务入口接入 DBOS。调用详情显示费用核对状态、原因及查询时间，费用汇总额外返回 cost_checks 分类数量。Worker 将费用扫描首次成功及持续推进纳入就绪，供应商待出账/限流/Token 失效保留为费用状态，不直接让整个 Worker 未就绪。最长未决时长和全局限流观测留待后续。

费用状态沿用既有语义：旧已知金额为 confirmed，缺少原范围的未知记录为 unavailable/legacy_scope_unknown，不把历史 unknown 改成零或默认为当前 Key。已有 Channel 在配置保存或下一次真实调用时绑定非秘密范围，不启动历史批量回填。可信发送归属和共享维护观测直接包含在基线结构中，不扩大图片推理或自动余额刷新范围。

验收至少覆盖：真实样本得到 0.002656 USD；错误 request ID 不匹配；other 为 JSON 字符串；分页 id 变化和重复页不双记；未扫完不误判未找到；延迟写入由下一轮发现；查后写前中断可重读；新 Key 不误查旧 Token；429/鉴权/结构错误/窗口耗尽各有明确状态；失败和取消后仍保存原账务事实；手动与后台并发不覆盖新结果；整个过程不新增推理请求。固定多 Worker 还须验证共享并发、原身份重启、查询崩溃续页和旧响应拒绝写回；自动接管及 RPM/TPM 仍未实现。

## 4. 权限与审计

管理操作日志采用 [AuditLog](audit.md)，ModelInvocation 继续负责每次调用的用量与费用记录。

沿用现有用户、个人令牌和 Casbin。模型调用、渠道管理与调用记录读取分别授权；模型调用权限不自动授予历史记录读取权。普通用户只能查看可调用模型的非敏感信息；首版不增加逐模型授权表，需要按业务对象限制时由业务模块检查。

首版将调用及普通可用模型目录明确为 model:invoke；配置管理、管理配置查询与账户余额查看/刷新统一使用 model:manage，不再细分余额权限。invoke 不授予 manage 或历史记录权限；manage 也不隐含 invoke。普通目录仅返回启用且所属渠道启用的模型 key 和必要能力说明，不返回内部地址、密钥配置状态或余额。配置管理接口仅返回非秘密值，普通调用者不能指定渠道密钥。功能权限由代码声明，不增加逐模型授权关系。

配置接口允许 model:manage 管理者录入或更新 API Key，不提供读取已保存密钥内容的接口；api_base 不允许内嵌凭据，Flow 选择快照只保存非秘密地址。调用记录视图也不通过关联 Channel 泄露凭据状态或账务配置。凭据配置失败以脱敏错误返回，不把 SDK 原始异常作为错误详情。

| 调用记录权限 | 范围 |
|---|---|
| model_invocation:read | 读取本人调用记录与本人费用汇总，按 user_id=当前用户过滤 |
| model_invocation:read_all | 在已有 read 权限的基础上，扩大至其他用户与 user_id 为空的系统记录 |

read 与 read_all 都属于代码权限目录，不增加授权表。会话与 PAT 都检查权限，PAT 使用“用户当前权限 ∩ 令牌权限范围”。获得 read 的 PAT 可读取所属用户的历史记录，包括其他凭据发起的调用；仅获得模型调用权限的 PAT 可以发起请求，但不能读取历史记录或费用汇总。归属统一按 user_id 判断，不新增凭据归属字段或逐令牌记录隔离。

查看他人或系统调用记录及费用汇总同时需要 model_invocation:read 与 model_invocation:read_all，flow:manage 不替代这些权限。Flow 页面展示 Run 下的调用明细或费用时，入口同时检查 Run 可见性与模型模块的记录查看规则；从模型记录页面查询仍使用同一套模型授权判断，不要求模型模块反向调用 Flow。权限过滤在分页与汇总前完成，无权限不能返回其他用户数据或把费用展示成零，也不能用 Flow 接口绕过限制。

模型记录可见不自动授予对应 Run 或文件的访问权。查看 Run 仍按 Flow 权限与归属判断，下载关联文件仍按文件功能权限、FileGrant 和状态判断。不新增跨模块授权表或另一套 Flow 专属调用记录。

渠道凭据由管理者在控制台录入，只允许服务端发送时解密使用；数据库保存密文，接口只返回 has_api_key、has_billing_api_key 两个状态。推理凭据与账务凭据分别配置，账务凭据使用相应接口所需的最小权限；没有账务凭据不影响已有推理能力。API 返回、异常和日志都不得泄露凭据。

金额用十进制定点值或十进制字符串，带币种/计量单位和来源。只有上游明确报告零费用才记录 `0`；缺失使用 `null`。按用户统计时分别展示已知费用和费用未知的尝试数，同币种、同口径才可汇总，不能把不完整统计叫总实扣。调用次数按状态区分，本地尝试总数不等于上游接受次数；未知不折算为零费用。

默认不保存提示词、模型输出或完整 HTTP 报文。保留必要用量和关联信息；配置、启停、凭据变更等管理操作记录审计日志。

时间由服务端生成，统一使用 UTC；操作人或调用归属来自可信身份，不能接受客户端任意指定。Channel、ModelEntry 创建时 updated_at 与 created_at 相同，updated_by 与 created_by 相同；系统配置操作允许操作者为空，来源写入审计日志。行上字段不能代替完整管理操作历史。

Channel、ModelEntry 是管理配置，保留四个审计字段。ModelInvocation 是调用事实记录，保留 user_id、created_at、finished_at、updated_at 和费用来源即可，不增加 created_by、updated_by。正常的调用完成、用量与费用补全记录在调用记录内，不为每次自动补全再复制一条通用管理审计。

Channel、ModelEntry 不论是否已被引用，首版均不提供物理删除或软删除；管理变更、启停和凭据变更记录审计。配置只停用的规则不改变调用记录的保留策略：ModelInvocation 不提供普通用户修改/删除接口，仍承担 Flow 恢复防重发作用的记录必须保留，确认对应运行不再有恢复或协调义务后才能按保留期清理，不在这里扩展成归档领域。

不为这三个对象增加 deleted_at、deleted_by。调用记录的保留期清理由任务日志记录范围、数量和结果，不模拟用户逐条删除，也不逐条增加删除字段；已有审计独立保留。Ether 的 模型连接删除 会保留连接行、设置 deleted 并清除秘密和授权；Keystone 首版不搬入该删除状态或相关协调机制。

管理操作的审计日志与调用记录用途不同：前者追踪谁改了配置，后者记录调用发生及费用信息。两者都不保存秘密明文，调用记录默认不保存提示词和输出正文。

## 5. 设计取舍

- 一个模型 key 只映射一个渠道及上游模型；首版不做路由池或自动切换。
- Channel、ModelEntry 只停用、不删除；不增加配置删除状态、引用清理或恢复机制，接受误建配置留存。
- 余额和用量使用结构化字段，不单独建表；余额逐渠道展示，不做跨账户汇总。
- 费用采用上游报告值，未知保留为空；不维护价格表，也不把聚合账单摊成单次实扣。
- 复用身份与权限，不新增租户、模型授权表、钱包、预算、账单或余额流水对象。
- 调用记录记录本地尝试，发送前登记；复用 ID 防止 Flow 恢复重发，不扩展成持久化任务、聊天会话或业务流程。

## 6. 待确定事项

当前开放 OpenRouter、Openlux、Yunwu；DeepSeek 与阿里云尚未开放发送。其他渠道扩展时先明确地址、账务权限、编号、分页及金额契约，再注册适配。依赖版本以锁文件为准。

费用采用上游报告值，无法确认的发送结果、余额或费用保留未知；不通过估价或新增本地账本掩盖。调用记录与恢复规则见下节。

## 7. 当前实现约定

本节记录当前实现约定，代码职责见第 8 节；自动核对的完整状态、分页 checkpoint、重试和迁移规则见第 3.6 节。手动与自动核对共用 CostService，不分别维护账务写入逻辑。

代码位于 backend/src/keystone/platform/model_access/，model_access 领域初始迁移建立 model_channels、model_entries、model_invocations，渠道直接使用凭据密文字段。复用 Ether ai/runtime.py、dispatch.py、invocations.py 的受控发送、运行时凭据注入、响应观测和安全错误分类思路，按 Keystone 的发送前登记规则改写；没有复制路由池、配额、聊天会话或其他领域实体。

### 7.1 配置

模型始终使用真实服务，不提供 disabled/mock/live 模式和发送开关。渠道创建时填写 api_key，可选 billing_api_key；编辑时省略或传 null 保留原密钥，clear_billing_api_key=true 清除账务密钥。输入采用 SecretStr，响应、审计和日志都不返回明文或密文。API 与 Worker 读取同一数据库，变更立即生效，无需重启。配置步骤见[模型接入](../../README.md#模型配置手动)。

加密使用 Fernet，绑定渠道 ID 与推理/账务用途，防止凭据密文跨渠道或用途替换。首次保存时在现有 FILE_COORDINATION_ROOT 自动原子生成 `.model-credentials.key`（Linux/macOS 文件权限为 0600），不增加配置项或初始化命令。API 与 Worker 共用该协调目录；已有密文而密钥丢失时拒绝密钥写入和调用，不静默重新生成。备份应保留完整协调目录。实现依据 [Fernet 文档](https://cryptography.io/en/latest/fernet/)。

当前支持 OpenRouter、Openlux、Yunwu 的固定地址，普通目录只返回启用且支持发送的渠道模型；DeepSeek/阿里云既有配置仍可管理，但不能发送。缺少或撤销凭据时在发送前拒绝。启动或保存配置不会自动发起模型调用。

确定性响应只通过测试目录中的 HTTP 传输替身注入，不随应用包或部署镜像发布。旧开发模拟数据不得用作真实调用的费用或恢复依据，应使用独立数据库和文件卷。

### 7.2 接口与权限

以下路径统一带 /api/v1。配置 PATCH 接收完整非秘密可编辑字段；凭据字段可省略以保留原值。Channel.provider 和 ModelEntry.key 创建后不可编辑；已填写账户标识不可更换，原为空时可补录。没有删除接口。

| 接口 | 要求与用途 |
|---|---|
| GET /model-access/options | model:manage；返回支持的服务商列表 |
| GET/POST /model-channels；PATCH /model-channels/{id} | model:manage；查询、创建、编辑和启停渠道 |
| POST /model-channels/{id}/balance | model:manage；同步刷新，失败保留旧快照，返回查询状态 |
| GET/POST /model-entries；PATCH /model-entries/{id} | model:manage；查询、创建、编辑和启停模型选择 |
| GET /models | model:invoke；只返回可用 key 和调用能力，不暴露渠道凭据配置 |
| POST /models/invoke | model:invoke；消息文本调用，stream=true 返回 start/delta/done/error SSE 事件 |
| GET /model-invocations；GET /model-invocations/{id}；GET /model-invocations/summary | read 限本人，read + read_all 扩大范围；明细、分页与汇总使用相同归属过滤 |
| POST /model-invocations/{id}/cost | 同时要求 model:manage 与该记录读取权限；仅补查已结束记录，更新费用，不改变调用状态 |

渠道和模型列表默认 50 条、最多 100 条；调用历史默认最近 30 天、范围最多 366 天，支持时间、渠道、FlowRun 过滤，列表另可按状态过滤。汇总在一个 SQL 快照内按状态计数，已知费用按渠道/单位分别求和，未知费用单独计数。数值以十进制解析，费用用 Numeric(38,18)，API 返回十进制字符串；只保留上游明确报告的用量字段，不补算缺失 token 或价格。

管理页提供配置、启停、余额、普通/流式调用、记录和费用汇总；所有权和 PAT 范围仍由服务端当前授权决定。只有历史权限时不会取得配置；只有调用权限时不会取得历史。当前分页选择、筛选及详情交互见 7.7。

### 7.3 发送与故障规则

- 每次进入 SDK 的实际 POST 发送边界，重新校验身份、model:invoke、原模型/渠道启用状态、固定地址与当前秘密。短事务原子插入 ModelInvocation 并提交后才允许发送；登记失败不发送。
- SDK max_retries=0，禁止自动跳转和环境代理；相同可信 invocation_id 的并发或重放拒绝再次发送，包括原调用已成功但没有可恢复响应的情况。公开 HTTP 只接收 key/messages/max_tokens/stream，不能提交 user_id、Flow 上下文、固定目标或内部调用 ID。
- 已明确拒绝的 4xx 记录 failed；402 映射为 model_insufficient_credits。超时、5xx、断流、中断或无法解析的结果记 unknown。HTTP 200 中的 error 或 finish_reason=error 同样按未知结果处理，返回安全错误码，保留已经收到的编号、用量和费用，不暴露上游原始错误。SSE 必须同时收到完成原因与结束标记；完整响应但没有 usage/cost 仍可成功，费用保持未知。失败与费用是否已知分别判断。
- 已发送请求即使身份撤销或配置停用，也补全原记录；不因此再发请求。补全写入失败返回 model_result_unavailable，可能遗留 running，不自动重发或按时间猜测失败。稳定 ID 的防重发记录仍保留。
- 文本消息最多 32 条，内容总计不超过 64 KiB；max_tokens 默认 1024、上限 4096。单进程模型调用/查询并发 4，调用执行最长 60 秒，SDK I/O 超时 30 秒；响应观测最多 4 MiB，单条流事件缓冲 64 KiB。取消后的事实收尾最多等待 5 秒，不留下后台发送重试任务。
- 默认不保存消息或输出正文。记录必要用量、上游请求 ID、费用来源及稳定错误代码；OpenAI/httpx2/httpcore2 日志不随应用 DEBUG 输出请求体和认证信息。

余额查询外部 I/O 最长 10 秒；每进程最多 2 个余额刷新，同渠道通过 PostgreSQL 会话锁协调，协调连接退出时释放，业务事务不跨外部 I/O。写回前核对配置，旧查询结果丢弃；失败保留上次成功快照，配置修改使余额来源变化时清空。余额采集不改 Channel.updated_at/updated_by；查询发生及结果进入 AuditLog。费用补查失败保留已知费用，重复查询只替换同一次费用，不累加。

### 7.4 Flow/Agent 接入

FlowModel 通过 ModelRuntime.invoke_sdk 内部入口，直接传入固定 Selection、稳定 invocation_id、FlowContext 和 sdk_request 回调，把 Pydantic AI 的协议适配置于受控客户端内；不构造公开 Invoke 的占位消息，普通模型调用与 Agent 每轮请求仍共享发送登记和响应后记录。公开模型 API 不因此增加工具、身份、稳定调用 ID 或固定选择参数。实际请求体超过 256 KiB 在发送前拒绝。

真实 DBOS Worker 使用 workflow/step 身份派生稳定 UUID。Agent 的每轮模型请求保存独立 ModelInvocation，同属一次 Agent 调用的记录共享 agent_call_id。已登记未发送、已收到响应但缺少 DBOS 检查点这两个崩溃窗口均阻止重发；已确认成功和费用保留，未知仍为 unknown。取消发生在发送后也会保存已返回的用量/费用，新请求被阻断。没有新增费用快照、账本、定价或消息表。

### 7.5 普通 API 调用离线收尾

API 硬退出或结果写回失败后，操作者先停止连接同一数据库的全部 API 进程，并在命令执行期间保持停止，再运行：

```shell
keystone recover-model-calls --confirm-api-stopped
```

该标志是操作者对停止状态的明确确认，命令不会探测本机或远端 API 进程，也不会自动停止它们；不能在仍有普通调用执行时使用。缺少确认直接拒绝，数据库迁移不匹配时也拒绝。确认完成后才能重新启动 API。若有自定义普通调用进程，也须一并停止。

入口调用 model_access/services/maintenance.py，仅处理 status=running 且 flow_context 中没有 flow_run_id 的普通调用。每批最多 100 条，在锁定后将状态置为 unknown、error_code 置为 model_interrupted，填充 finished_at/updated_at；保留原 ID、请求编号、用量、费用及归属。每条 model.invocation.recover 系统审计与该批更新同事务；批次失败回滚，之前已提交批次保留，重跑不重复修改或审计。成功结束记录总处理数。

命令不发起任何上游请求、不重发推理、不删除记录；已经结束的调用及 Flow 调用均不处理。Worker 可以继续处理 Flow。收尾后，具备原费用补查权限的用户可通过已有 cost 接口查询；能否确认费用仍取决于上游编号和供应商能力。保持原有三张表，不增加心跳、租约或恢复任务表。

### 7.6 控制台

/console/models 按管理、调用和历史读取权限分别加载配置、调用和记录区域。渠道与模型配置使用独立编辑弹窗和 50 条分页；渠道选择器、可用模型选择器也支持分页，不只查询前 100 条。当前所选渠道不在这一页时保留 ID，提交后由 API 校验。服务商、模型 key 及已确定的账户标识不可修改，配置只停用不删除；推理与账务 Key 通过密码输入框提交，保存后仅显示已设置状态，不回显内容。

余额同时显示最近查询状态、最近尝试和成功采集时间；本次查询失败时保留的旧快照明确标为历史成功结果。未知余额、未知调用费用都不视为零，不跨渠道或单位合并。调用详情显示当前结果、错误码、上游请求编号、费用来源/说明、用量和 Flow 关联，不包含消息正文。费用补查仍只调用现有账务查询接口，不重发模型请求。

历史列表与费用汇总共享渠道 ID、Flow Run ID、开始/结束时间筛选，默认最近 30 天、最多 366 天；应用筛选时固定同一组时刻。首版不新增用户筛选或状态汇总接口；本人/全部可见范围由当前权限决定。分页只影响列表，汇总覆盖筛选范围；两者分别显示加载失败，避免误把汇总失败当作没有调用。

首次加载也固定相同的起止时间，翻页沿用该范围。刷新或真实调用完成后，默认结束时间同步推进，列表回到第一页；显式填写的结束时间保持不变。刷新使用已提交的筛选条件，不应用仍在编辑的输入。

普通和流式响应的按钮统一为“发送真实请求”，提示可能计费，打开页面或保存配置不会自动调用。Flow 发起表单同样提示真实调用。停止接收只中断浏览器接收，不表示上游撤销或没有费用，不自动重发；离开页面中断接收并清空输入输出。HTTP 错误和流内 error 事件共用会话失效处理。配置变化后可手动刷新模型目录，实际发送仍复查当前配置和权限。

参考 Ether ModelProbe 的操作隔离和失效结果处理，复用本后台 Element Plus 弹窗/详情抽屉、分页及请求代次控制；不搬入 Ether 的通用连接授权或多模态探测功能。

### 7.7 OpenRouter 接入

沿用现有 OpenAI SDK、受控 transport 和 OpenRouter 费用适配，不增加网关、SDK 依赖或领域表。实际发送使用 httpx2.AsyncHTTPTransport(retries=0, trust_env=False)，SDK 同样禁止重试；忽略 Retry-After，不自动重发计费请求。只允许 `https://openrouter.ai/api/v1`，发送前逐次核对完整地址、POST /chat/completions 和固定模型；账务查询只允许 GET /generation、GET /credits，拒绝跳转和自定义代理地址。

| 能力 | 凭据与口径 |
|---|---|
| 普通/流式推理、Agent 工具调用 | api_key 使用普通推理 API Key；工具调用仅由可信 Flow 适配器使用，公开 API 不增加工具参数 |
| 响应费用 | 使用 usage.cost，单位 credits；不把 cost_details.upstream_inference_cost 当作账户扣费，不根据 token 估价 |
| 费用补查 | 使用原渠道推理 Key 请求 /generation?id=上游编号，验证响应 ID 后读取 total_cost；失败保留原费用，不重发推理 |
| 账户余额 | billing_api_key 使用同一账户的 Management Key；/credits 的 total_credits − total_usage，作为账户剩余额度快照，不是单 Key 限额 |

账务凭据可不配置，此时余额 unavailable，不影响推理；凭据不足或查询失败不能清零旧快照。账户归属由部署者核对，当前没有自动认证两个 Key 属于同一账户的能力。控制台直接录入密钥，保存后仅返回已配置状态，不回显内容。

依据 OpenRouter 官方[用量与费用](https://openrouter.ai/docs/cookbook/administration/usage-accounting)、[账户 credits](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits)、[Generation 查询](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation)及[错误处理](https://openrouter.ai/docs/api_reference/errors-and-debugging)实现协议适配。配置步骤见[OpenRouter 接入](../../README.md#模型配置手动)。

### 7.8 当前代码组织

三个实体分别位于 models/channel.py、model_entry.py、model_invocation.py。渠道、模型配置、调用记录分别由 services/channels.py、entries.py、invocations.py 处理；services/runtime.py 管理调用用例，委托 inference 执行受控发送。Flow 调用 select_model(session, key, lock=True) 获取固定选择，保留共享行锁和原事务；services/recovery.py 提供共用的分批收尾，Worker 入口要求持有自身身份锁；maintenance.py 保留普通调用离线恢复的显式确认。响应在 schemas/ 中明确列出字段，不随表新增列自动扩展。 统一规范见[整体架构](../architecture.md#54-模块分层与代码规范)。

调用历史与汇总和审计共用 core/time_range.py：since/until 为闭区间，未传 until 时取数据库当前时间，未传 since 时取 until 前 30 天，最多 366 天，端点必须带时区；仅传历史 until 同样有效。ModelInvocation.created_at 使用数据库时间，默认查询也使用数据库时钟，避免刚写入的记录被应用时钟偏差排除。显式边界原样执行，错误保持 invalid_time_range。

接入能力在 adapters/inference/client.py 的 SUPPORTED_TARGETS 声明推理渠道、固定地址和方法；日志适配器另声明受控账务 origin，只允许 GET /api/status 和 /api/log/token。模型目录按可推理渠道过滤，实际发送与账务查询再次校验，不增加动态插件注册。

恢复共用每批 100 条锁定、unknown/model_interrupted 更新及逐条系统审计；保留费用、编号和归属，不重发模型请求。普通入口要求全部 API 已停，只处理无 flow_run_id 的 running；Worker 启动持有身份锁，只处理本 worker_id 的旧 worker_instance_id，不触碰其他槽位。Flow 发送归属列为内部字段，不进入公开调用投影或客户端输入；实际执行时生成，不从旧检查点复制。

无归属历史 Flow 调用会阻止启动；迁移后在全部 API/Worker 停止期间使用 --legacy-flow --confirm-api-stopped --confirm-workers-stopped 专项收尾。该入口取得原全局排他锁，只处理无归属 Flow 调用并重置旧观测，不能当作在线恢复。审计仍为 model.invocation.recover，以 source=offline_api_recovery / flow_worker_recovery / offline_flow_recovery 区分；更新与日志同事务，重复执行不重复审计。

错误分类统一由 adapters/inference/errors.py 的 classify_error 沿异常因果链处理；显式 ServiceError 优先，SDK APIStatusError 和 Pydantic AI ModelHTTPError 使用同一 HTTP 错误码映射。HTTP 402/429 在普通调用与 Agent 中均为 failed，并分别保留 model_insufficient_credits / model_rate_limited；上游 5xx、超时、取消及无法确认结果仍为 unknown。分类同时提供安全响应和调用状态，不从原始异常文本推断、不透传上游内容；费用未知仍为未知，失败不代表免费。参考 Ether ai/runtime.py 的安全包装，保留 Keystone 的发送前登记和拒绝重发规则。

adapters/billing/openrouter.py 负责 generation 直查；adapters/billing/openlux.py 和 yunwu.py 各自声明站点与单位，共用 token_logs.py 解析有界日志页。services/cost_context.py 绑定原账务范围，cost_policy.py 管费用状态与等待策略，costs.py 管互斥、查询、重试及原子写回；model_access/tasks/workflows.py 只负责 DBOS 扫描和投递。InvocationService 保留授权和手动入口。DeepSeek/阿里云仍不补算调用价格。UTC 应用时间统一使用 core/time.py，数据库生成的 created_at 和历史窗口规则不变。

## 8. 代码结构与职责映射

本节描述当前已实现的代码结构。保留现有三张表、API、权限和费用规则，不增加独立账务领域或进程。统一采用 Controller/Service/Model/Schema 基本骨架，同时按职责组织 adapters/inference/ 与 adapters/billing/ 两个能力包；它们不承担业务授权、ORM 持久化或后台调度。跨模块规则见[整体架构分层](../architecture.md#541-model-access-分层)。

### 8.1 目录与装配

```text
platform/model_access/
├── catalog.py                 # 本模块权限及审计字段白名单
├── controllers/               # HTTP 参数、响应、SSE 与服务入口
├── services/
│   ├── channels.py            # 渠道配置、启停与原查询范围约束
│   ├── entries.py             # 模型配置和固定选择
│   ├── invocations.py         # 历史、汇总、手动补查授权
│   ├── runtime.py             # 授权、发送前登记和事实收尾
│   ├── costs.py               # 扫描、范围核对、分页进度和原子写回
│   ├── cost_policy.py         # 等待、重试、截止和费用状态规则
│   └── cost_context.py        # 原账务范围和非秘密口径快照绑定
├── models/                    # Channel、ModelEntry、ModelInvocation
├── schemas/                   # API 输入输出与内部可信调用契约
├── adapters/
│   ├── inference/
│   │   ├── client.py          # SDK 与调用协议、受控目标声明
│   │   ├── transport.py       # 发送边界、流式观测和必要回执
│   │   └── errors.py          # SDK/Pydantic AI 与平台错误安全分类
│   ├── billing/
│   │   ├── contracts.py       # 账务能力、查询上下文及规范化结果
│   │   ├── registry.py        # 显式选择 provider 适配器
│   │   ├── openrouter.py      # 响应费用、generation 直查、余额协议
│   │   ├── openlux.py         # Openlux 站点及 USD 口径
│   │   ├── yunwu.py           # Yunwu 站点及自定义额度口径
│   │   ├── token_logs.py      # 已验证的共用 Token 日志协议
│   │   ├── http.py            # 账务 HTTP 失败分类与 Retry-After
│   │   ├── deepseek.py        # 未开放的余额能力声明
│   │   └── aliyun_bailian.py  # 未开放的余额能力声明
│   ├── clients.py             # 技术依赖的不可变组合
│   ├── http.py                # 有界 HTTP 读取机制
│   ├── receipts.py            # 有界请求编号校验
│   ├── credentials.py         # 凭据加解密及非秘密范围标识
│   └── coordination.py        # 查询范围锁、连接所有权
└── tasks/
    ├── definitions.py         # 轻量费用任务、输入和稳定计划
    └── workflows.py           # 扫描 Step、范围 Step 与子任务投递

files/tasks/                   # 文件清理声明与执行适配
flow/tasks/                    # Flow 补投/同步声明与执行适配
core/execution/                # 声明、投递、计划、Step 保护、生命周期及版本
composition.py                 # 显式装配，不扫描或自动加载目录
```

上表列出主要职责文件，controllers/、services/、models/、schemas/ 内其他已有职责文件继续保留，包括普通离线收尾和 Flow 中断收尾。DeepSeek、阿里云等已有费用/余额能力声明按实际职责迁入对应 billing 文件，未开放的能力仍保持不可用，不因文件拆分扩大支持范围。

调用协议与账务适配分别选择。例如 provider=openlux 使用受控 OpenAI 兼容客户端和 Openlux 账务适配器；provider=yunwu 可复用相同调用客户端，但使用自己的站点与额度口径。Channel.provider 继续作为显式映射入口，当前不新增调用协议、账务类型等数据库配置列。注册表只声明已支持的映射，不保存秘密、查询数据库、初始化 SDK 请求或注册 DBOS 任务；composition/bootstrap 显式传入服务需要的客户端、凭据和协调能力，不让业务方法依赖整个应用 Runtime。ModelClients 仅组合 inference、billing、credentials、coordination 及 HTTP 技术依赖，不转发业务方法、不隐式选择供应商；Service 直接使用对应能力。

### 8.2 billing 的职责与结果契约

adapters/billing/ 负责供应商账务协议。它可以通过受控 HTTP 客户端查询，也可以独立解析必要回执；业务 Service 只依赖它声明的能力和规范化结果。

| 能力 | 适配器负责 | 返回给 Service 的信息 |
|---|---|---|
| 绑定原账务上下文 | 声明固定站点、凭据用途、Token/账户可见范围和口径版本 | 非秘密绑定信息、单位与换算口径；由 Service 结合凭据范围标识保存快照 |
| 响应费用与编号识别 | 解释白名单回执，选择可用于查费的编号，验证响应金额含义 | 合法请求编号、可信费用或未提供、必要规范化证据 |
| 请求 ID 直查 | 路径、鉴权、参数、编号校验及金额解析 | 找到的费用、尚未报费或安全失败分类 |
| 日志分页 | 站点筛选参数、页码/游标、结构及金额口径校验 | 规范化记录、下一页位置和窗口覆盖是否完整 |
| 余额 | 声明是否支持、凭据用途与账户范围，解析余额分项 | 原范围、采集结果、带单位的结构化余额；不冒充单次费用 |
| 查询失败 | 识别站点契约内的等待、暂时失败、鉴权/配置/结构错误及不支持 | 安全原因码、可重试性、有效 Retry-After；不透传原始错误或含 Key 的 URL |

contracts.py 的类型用于模块内部协议，区别于 schemas/ 中的 API 投影，也不直接充当 ORM 或 DBOS 参数。金额保持 Decimal/十进制字符串及原单位；证据只保留必要编号、额度、换算口径和来源信息。不保存完整上游日志、消息、账号或网络报文；秘密只在受控请求执行时使用，不进入规范化结果、注册表或持久任务输入。

能力覆盖响应直接报费、请求直查、日志列表和不支持。按实际能力提供小型契约或普通适配类，不强制每个渠道实现一组无效接口。查询结果不直接等于 cost_check_status：适配器返回协议事实，Service 决定 pending/confirmed/blocked/unavailable/exhausted 及其持久化。

日志页未匹配、页面为空和分页尚未结束均不自动代表整轮未找到。适配器报告分页事实，Service 保持固定窗口、累计目标匹配并检查冲突；只有完整覆盖后才确认或进入下一轮等待。相同请求的重复记录不相加，矛盾记录仍阻塞。HTTP 404 的等待含义必须由对应查询契约确认，不能在通用客户端里一概视为未出账。

Openlux 与 Yunwu 各自声明账务 origin、原 Token 范围、quota 换算及公开口径校验；可共享经验证的 token_logs 处理。Yunwu 与 Openlux 均直接复用 TokenLogs，不再互相继承站点适配，不假定两家站点所有行为一致。币种、custom_currency_exchange_rate 等站点判断移入对应适配器；公共 CostService 不再包含 provider == yunwu 之类分支。原快照的校验及禁止按当前口径重解释历史费用规则继续生效。

### 8.3 inference 与 Service 的交接

adapters/inference/ 封装 SDK、调用格式、受控地址、HTTP 方法、流式读取和必要回执观测。多个中转站共用已验证的调用协议；普通目录和临发送检查使用同一份显式推理能力声明，账务接口使用独立的受控声明。不自动发现端点，不允许任意代理地址、跳转或环境代理，也不恢复 SDK 隐藏重试。

inference 和 billing 可复用受控 HTTP 请求、超时、响应大小及错误脱敏等小型技术函数，复用部分不包含站点账务语义、SDK 模型对象或 ORM。billing 不依赖 inference 的具体 SDK 实现，站点路径、鉴权位置及业务响应含义仍归对应账务适配器；技术复用不再形成一个同时处理数据库锁和所有供应商协议的 Gateway。

services/runtime.py 继续拥有调用授权、固定模型选择校验、稳定调用 ID、发送前原子登记和发送后的事实保存。SDK 请求实际离开进程前必须经过 Service 的授权与已提交登记边界；inference 不得自行创建 ORM 记录或绕过该边界发送。编号到达后仍由 Service 尽早落库，避免只在流结束后才保存；响应体编号不得覆盖 billing 选出的可信查费编号。inference 提供必要的白名单回执，billing 解释其账务含义，Service 保存原调用事实和费用待办。

SDK/Pydantic AI 错误和网络异常在适配边界转换为安全分类，Service 按分类保存结果；普通与 Agent 请求继续共享同一受控发送核心和一致的状态规则。4xx 已知拒绝、5xx/断流/中断未知、已发送请求在撤权或取消后仍收尾等行为不变。这里的 transport 是 SDK/HTTP 传输实现，不是 FastAPI Controller 的 Request/Response。

services/costs.py 负责扫描到期范围、加载原查询快照、选择账务能力、固定窗口、跨页匹配、冲突检查、写前配置与所有权复核，并以短事务提交费用、证据和进度。HTTP 查询留在业务事务外。原 Token 不能被当前新 Key 替代，相同真实 Token 的多个 Channel 共用协调范围；这些规则由 Service 执行，不交给账务适配器更新渠道记录。

services/cost_policy.py 保留纯规则：首次等待、未出账间隔、暂时失败预算、Retry-After 与截止时间的约束、费用状态转换。它不查询供应商、不访问数据库、不注册定时任务，也不按供应商名称决定口径。adapters/credentials.py 只提供加解密与 HMAC 范围标识等技术能力，共享卷身份校验使用 core/local_leases.py，不依赖 Files 内部 Adapter；adapters/coordination.py 提供会话锁和所有权检查。Service 决定何时取得锁、如何处理范围变化及何时写回，不把这些数据库协调实现继续放在 HTTP gateway 中。

### 8.4 Worker、DBOS 与手动入口

| 路径 | 调用关系 |
|---|---|
| 模型调用 | Controller/可信 Flow 适配 → 调用 Service → inference；Service 结合 billing 解析账务回执并保存事实 |
| 手动查费 | Controller → InvocationService 授权和记录范围检查 → 共用费用核对 Service → billing |
| 自动查费 | DBOS 计划/队列 → model_access/tasks/workflows.py → 同一个费用扫描/核对 Service → billing |
| 手动余额刷新 | Controller → 已授权 Service → billing 余额能力 → 原子替换成功快照或保留旧值 |

model_access/tasks/workflows.py 只负责扫描与核对入口的 DBOS 装饰器、稳定执行标识、队列/计划、技术去重和执行健康包装。扫描入口调用 Service 获取到期 scope 并合并投递；核对入口传入 scope 调用 Service。任务输入只传必要非秘密标识，原凭据、原口径和分页进度执行时从领域记录加载，不把 Service、应用 Runtime、秘密或完整日志序列化进任务。

费用待办、next_check_at、截止时间、错误预算和分页 checkpoint 继续由 ModelInvocation/Channel 保存。Worker 不决定费用金额、是否匹配或何时视为未出账；不直接读写这些 ORM 实体。DBOS 负责已投递执行的恢复，业务重试由持久费用状态驱动，不为每笔费用创建长期 sleep workflow。手动补查继续在有界同步预算内执行，分页未完成时保存进度并返回待核对状态，Worker 后续发现待办；不要求手动入口先投递 DBOS。

本模块的 services/、models/、schemas/、adapters/billing/、adapters/inference/ 均不直接导入 DBOS，也不反向导入 Worker 任务或 Controller。core/execution/runtime.py 只提供技术生命周期和薄调度接入，不导入 model_access、files 或 flow 的业务服务；由 composition 装配。Flow 的引擎与 Agent 持久适配、业务 workflow/step 仍按原约定使用 DBOS，目录整理不重写 Flow 表达方式。

文件清理和 Flow 协调的 DBOS 包装分别位于 files/tasks/workflows.py、flow/tasks/workflows.py，调用原所属模块的服务。清理规则、取消与同步、原执行身份和模型中断收尾不搬到 Worker；Flow 开关仍不关闭费用扫描或已启用文件维护。Worker 按固定身份互斥并共享预算；每次真实 Flow 模型发送登记当前实例归属，服务与适配器不因此导入 DBOS。

### 8.5 原文件与现行职责映射

| 原代码 | 现行位置与职责 | 保持的约束 |
|---|---|---|
| providers.py | 费用/余额解析及站点口径分别进入 `adapters/billing/<provider>.py`；调用用量/协议观测进入 inference；抽取已验证的 token_logs 共用处理 | 原单位、合法编号选择、能力边界与安全校验，未支持能力仍明确不可用 |
| gateway.py | SDK/推理目标进入 adapters/inference/client.py，账务路径/鉴权/业务响应分类进入 billing；共用受控 HTTP 机制保留明确技术边界，范围锁移到 adapters/coordination.py | 固定 HTTPS 目标、方法白名单、无跳转/环境代理/隐藏重试、超时、响应上限及脱敏 |
| transport.py | 移入 adapters/inference/，负责传输与必要回执观测，账务语义通过 billing 解释 | 发送前回调、尽早保存编号、有限响应观测和安全失败处理 |
| billing.py | 状态与等待规则进入 services/cost_policy.py；绑定原账务范围由 Service 调用 billing 声明和 credentials 技术能力后持久化 | 原范围及口径快照、缺少编号/能力的原因、截止和状态语义；adapters/billing/ 不兼任 ORM 更新工具集 |
| services/runtime.py | 保留调用用例与受控登记，将 SDK/传输细节委托 inference，可信费用与查费编号解释委托 billing | 授权复查、固定选择、原子登记、一次发送、撤权/取消后的事实收尾 |
| services/costs.py | 保留公共核对算法与事务，站点判断交给 billing，等待规则调用 cost_policy，锁调用 coordination | 完整分页后确认、重复不累加、矛盾阻塞、旧配置结果丢弃和金额/进度同事务 |
| services/channels.py、invocations.py | 保留配置、历史、汇总及入口授权；通过明确账务能力和共用核对服务协作 | 原 Token 更换约束、本人/全部范围、PAT 权限、手动重启审计及已知费用保留 |
| credentials.py、gateway.refresh_lock | 凭据能力仍就近保留，SQL 协调独立到 adapters/coordination.py | 密文用途绑定、共享卷密钥、同范围串行及失去所有权时拒绝旧写回 |
| model_access/jobs.py、files/jobs.py、flow/jobs.py | 分别迁移到所属模块 tasks/definitions.py 与 workflows.py；共用 core/execution/ 的计划和投递 | 稳定计划/队列名；仅保留最新工作流入口，保留去重及同版本恢复语义、健康检查及能力开关 |
| composition.py、bootstrap.py、entrypoints/worker.py | 显式装配协议、账务适配、服务和 Worker 入口；核心生命周期继续由 core/execution/runtime.py 管理 | API 不启动执行器、Worker 独占锁、Flow 启动恢复顺序和制品版本保护 |

这些是职责迁移，不要求把每个现有函数逐一变成一个类或增加通用 Repository、BaseService、多引擎接口。文件大小按实际职责控制；表、API 和配置不因目录调整而扩展。

### 8.6 后续接入与边界验证

新增中转站先确认调用协议能否复用，再实现账务能力、范围、编号、分页、金额及错误契约并显式注册。新增 provider 时同步维护现有配置校验和数据库约束，确需新增枚举值时追加迁移；不能只加解析类就绕过目标白名单。共享协议仅复用已验证部分，站点单位和权限差异分别声明，公共 Service 和 Worker 不增加供应商名称分支。

后续修改继续遵守第 3.6 节既有查费验收，并验证以下边界：

- 普通 Service、billing、inference、Model 和 Schema 不直接导入 DBOS；Worker 执行包装不直接操作领域 ORM，适配器不反向调用 Service。
- 同一调用协议可配不同 billing 适配；单位、原 Token 和请求编号分别校验，新增站点不改公共费用确认算法。
- 手动与自动核对共用同一事务和协调实现，等待/重试结果在独立于 DBOS 的服务测试中可验证；真实 Worker 仍验证定时发现、去重和重启续查。
- 移动任务包装保持稳定名称和业务待办，不重复注册计划或新增推理；版本摘要变化仍按原制品发布/恢复流程处理，不跳过 execution_version 检查。
- 普通调用、Agent、Flow 取消/恢复、当前权限与原调用归属、费用 API 的十进制和显式投影保持一致；目录重排不改变 SDK 重试禁令和未知结果不重发规则。

同宿主同版本的固定多 Worker 按整体架构部署；图片生成、自动余额刷新、跨宿主及自动接管不在当前实现范围内。
