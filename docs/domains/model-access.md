# 模型接入

本模块保存渠道、模型配置、调用/用量和供应商费用；billing 与 inference Adapter 处理协议，Service 负责授权、事务和事实，后台 Task 负责核对编排。[Flow](flow.md) 通过公开 Service 固定角色选择与累计预算，不共享领域 ORM。

第 1～7 节描述调用、费用和恢复契约；[第 8 节](#8-多渠道模型接入设计)维护现有渠道代码适配层与数据库的边界。当前适配整理不增加渠道、数据库字段或视频能力；仍只记录、补查和统计渠道实际成本。

## 1. 范围与边界

为后续业务提供统一的模型调用入口，记录每次调用的用量、上游报告的费用，并展示渠道账户的余额。采用“成熟调用 SDK＋少量渠道适配代码”，不要求独立网关。

新建渠道提供 OpenRouter、NewAPI 和 Yunwu，复用 OpenAI SDK 的文本对话及流式协议。NewAPI 表示兼容接口类型，默认 `https://newapiai.com/v1`，可配置HTTPS API 地址及路径前缀；使用 Key 查询参数与分页日志的 Openlux、Yunwu 兼容分支统一选择 Yunwu，分别填写 `https://api.openlux.ai/v1`、`https://yunwu.ai/v1`。已有渠道的内部 provider 与历史快照保留，Openlux 在界面归为 Yunwu 类。OpenRouter 可保存响应费用并按请求 ID 补查，Openlux/Yunwu 使用原 Token 分页核对日志；标准 NewAPI 接入文本、模型列表、Bearer Token 日志查费、账户余额与 Token 额度；Yunwu 类接入 Key 参数分页日志查费。图片理解复用受控 Chat Completions 多模态输入，仍要求模型显式声明 image.understand；图片生成和原生媒体任务按已验证站点选择，不因聊天接口兼容推断支持。DeepSeek、阿里云百炼保留适配声明，尚未开放发送。阿里云暂按百炼/DashScope 按量付费服务设计，不把订阅额度混为账户余额。图片与异步任务能力见[模型操作与产物](#6-模型操作与产物)。

渠道、模型配置、调用记录是三个主要对象；第 8 节的代码适配层继续复用它们。不做用户钱包、充值、额度扣减、计费倍率、价格表、自动渠道切换或智能路由。这里记录的是供应商侧成本，不是向 Keystone 用户收费；已有 Flow 次数/token/文件预算仍属于资源限制。

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

调用、渠道和模型配置使用三张主表，另有真实凭据范围的 ModelDispatchScope 发送协调表。余额快照和用量是结构化字段，不单独建表；渠道适配器是代码实现，不是领域实体。

### 2.1 渠道 Channel

| 字段 | 含义 |
|---|---|
| `id`、`name` | 稳定 ID、展示名 |
| `provider` | 新建可选 `openrouter`、`newapi`、`yunwu`；`openlux` 作为已有渠道内部标识保留，`deepseek`、`aliyun_bailian` 保留声明 |
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
| `name` | 可修改的显示名称，创建时未填写则使用 key；改名不改变模型绑定 |
| `channel_id` | 所属渠道 |
| `provider_model` | 上游接受的模型标识 |
| `operations` | 管理员声明的明确操作集合，默认 text.generate；可发送能力还须与已实现协议匹配 |
| `is_active` | 是否允许新调用 |
| `created_at` | 创建时间，服务端生成，UTC，必填 |
| `created_by` | 创建操作的用户 ID，系统操作可为空 |
| `updated_at` | 最近修改时间，UTC，创建时与 created_at 相同 |
| `updated_by` | 最近修改操作的用户 ID，系统操作可为空，创建时与 created_by 相同 |

一条配置只映射一个渠道和一个上游模型。界面展示可修改的显示名称和固定 key；模型选择器同时标明两者。业务不接触渠道密钥；映射修改不需要业务改 key，但模型能力和行为变化仍需业务验证。历史调用保存当时的渠道和模型，不能通过当前配置反推。

普通直接调用在本次尝试开始时解析 key；Flow 在受理时固定模型选择，映射修改只影响之后受理的运行，规则见[固定模型选择](#34-flow-的固定模型选择)。

首版不引入模型家族、模型版本、路由池、价格方案或通用能力注册表。模型与渠道都启用才允许发起新调用；停用不修改已有调用记录。

Channel、ModelEntry 仅在没有引用时允许物理删除，不增加 deleted 状态、deleted_at 或回收站。有调用记录或流程引用（包括已结束运行）时拒绝删除，只允许停用；渠道仍有任何模型配置时也拒绝删除，须先删除未使用的模型配置。停用 Channel 阻止该渠道的新调用，但不批量修改其 ModelEntry；重新启用渠道后，仅模型配置自身也启用时才能调用。单独停用 ModelEntry 只阻断该配置的新调用。

停用配置仍可由有权限的管理者查询、修改或重新启用；保留的 ModelEntry.key 继续被原配置占用；未被引用的误建配置可删除，删除后才释放 key。旧 Run 仍关联原配置，但原模型或渠道停用时明确拒绝新调用；已发送请求继续保存结果、用量和费用，历史数据不改写。

停用是 Keystone 的调用控制，不等于撤销供应商密钥，也不自动清除已保存的渠道凭据。管理员可在控制台替换 API Key、清除可选账务 Key；供应商侧的撤销由操作者处理，配置与历史记录继续保留。

### 2.3 调用记录 ModelInvocation

| 字段 | 含义 |
|---|---|
| `id` | 本地调用尝试 ID；Flow 内每次持久模型请求使用可在恢复时复现的稳定 ID |
| `user_id` | 发起用户；系统调用可空，由可信服务端确定 |
| `flow_context` | 可空的小型 JSON：flow_run_id、model_role、node_key、agent_key、agent_call_id、可空 workflow_id/step_id，按实际调用场景填写；普通调用为空 |
| `model_entry_id`、`channel_id` | 本次使用的模型配置与实际渠道 |
| `provider_model` | 本次上游模型标识快照 |
| `upstream_request_id` | 上游返回的请求/generation ID；未收到时可空 |
| `operation` | 本次固定的语义操作，历史缺失按 text.generate 解释 |
| `result_context` | 私有 TaskReceipt/ImageManifest 恢复证据，公共调用视图不返回 |
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

调用协议位于 adapters/inference/、费用和余额协议位于 adapters/billing/，Service 保留业务用例、权限与事实保存。多个中转站在代码中复用调用协议、组合各自账务实现；adapters/billing/ 不再复制一套 Controller/Service/Model，也不直接持久化费用。代码组合与宿主边界见[第 8 节](#8-多渠道模型接入设计)。

### 3.2 调用与余额刷新

1. 校验输入、当前身份、调用权限及模型/渠道启用状态，在即将发送前创建并提交调用记录；记录失败则不发起上游请求。Flow 还需遵守下文的原子登记与防重发规则。
2. 发起请求，保存上游请求 ID、用量、结果状态和可获得的费用。流式请求在流结束后更新，缺失末尾用量时不补造数据。
3. 可信响应费用直接确认；缺少费用且具有原查询范围及合法请求 ID 时，同事务保存 pending，Worker 后续自动核对。授权管理者也可显式补查，复用同一服务。没有请求 ID 或不支持查询时保留未知原因。
4. 有管理权限的用户可手动刷新渠道余额；API 直接调用本模块，在有界超时内查询并保存结果，不返回需要 Worker 接续的后台任务。不在每次模型调用前后查询余额，首版不安排定时同步。

每次实际发往上游的尝试必须对应一条记录，但记录可能在发送前的中断窗口内已创建，不能反过来把每条记录都算成上游已接受请求。首版关闭 SDK 的自动重试；收到响应后记录更新失败也不能触发模型重试。以后若允许新的重试尝试，必须逐次使用不同 ID 记录，不能覆盖原尝试；Flow 的同一请求恢复不属于新的获准尝试。Flow 调用通过已有 flow_context 关联，普通业务自行保存相关调用 ID，不新增通用业务关联号字段。

超时、断流或进程中断可能无法确认上游结果，记为 `unknown`，不能推断没有收费。硬退出或结果写回失败可暂时遗留 running，只有确认原执行已结束后才能收尾；普通 API 调用使用下述离线命令，Flow 调用由持有排他锁的 Worker 启动恢复。调用结果和费用是否已知相互独立；补查失败不改变已经成功的调用结果。调用记录不是持久化任务或业务会话。

请求发出后用户停用、权限收回或原令牌失效，不阻断该次调用的响应处理和记录补全。已有尝试的状态、用量和费用继续由可信执行上下文或受限系统维护补齐，user_id 仍保留原始归属；费用补查只核实原请求，不重新推理。撤权后的新请求仍拒绝，用户查询记录也必须通过当前权限检查。

余额刷新失败时保留上次成功快照，显示采集时间和本次失败状态。余额只是有时间戳的上游观察值，不用于保证下一次调用一定成功，也不用余额前后差计算单次费用。多个渠道共享同一上游账户时不能重复汇总，首版仅逐渠道展示。

余额查询分为读取当前配置、事务外请求、短事务写回三段；同渠道刷新串行协调，并与影响查询的配置变更协调。写回前确认仍对应原查询配置，过期结果丢弃，不能覆盖新配置的快照；不为此增加刷新请求表、任务状态或版本字段。API 中断时不承诺请求已完成，保持最后已确认信息，可再次手动查询。渠道停用只禁止新推理，不禁止授权管理者查询历史、补查费用或刷新余额。渠道余额刷新是本模块的简化规则，不声称复用现成实现。

### 3.3 渠道能力

标准 NewAPI 与 Yunwu 是两种已观察到的查费协议分支，不据此断言底层架构完全不同。标准 NewAPI 使用 Bearer Key，`GET /api/log/token` 返回最多 1000 条最近记录；部分兼容实现也返回分页对象。用响应头 `x-oneapi-request-id`（其次 `x-api-request-id`）匹配日志顶层 `request_id`（其次 `other.request_id`），不把模型供应商响应体 ID 当成网关账单 ID。只保存消费记录的精确编号、模型、quota、实际 `quota_per_unit` 与金额证据；标准 NewAPI 的系统基准单位为 USD，显示币种、兑换率和充值价格不参与二次换算。依据：[日志接口](https://docs.newapi.pro/en/docs/guide/feature-guide/user/log)、[额度换算实现](https://github.com/QuantumNous/new-api/blob/main/web/src/lib/currency.ts)。

最近记录达到 1000 条时保存可见时间边界；匹配到的原请求可以确认，超出可见范围的未匹配调用标为 `cost_log_window_unavailable`，不会按空结果记零或把相同列表当分页无限重读。本轮额度比例与分页位置一起进入 checkpoint，后续页沿用同一比例。Yunwu/Openlux 的既有账务口径与历史上下文保持不变；其他 Yunwu 兼容站点以 `yunwu_credits` 保存该站额度，按其公开比例归一化，不推断为美元。

余额按钮对标准 NewAPI 使用普通 Key 查询 `GET /api/usage/token/`。接口提供 `user_quota` 时，直接读取其中的账户剩余和已用额度，按公开 `quota_per_unit` 换算，快照 `scope=provider_account`，`items` 保存账户额度，`token_items` 单独保存当前 Key 的额度；`unlimited` 仅表示 Key 不限额，不表示账户余额无限。上游未返回 `user_quota` 时退回 `scope=token_quota`，明确说明账户余额未提供；不限额 Key 不展示零剩余额度。账户余额为真实零或负数时按实显示。账户额度查询受上游令牌的 `allow_user_quota_query` 设置控制，不需要额外管理凭据；不会主动修改上游令牌权限。模型列表仍使用普通 Key 的 OpenAI 兼容 `/v1/models`，账户管理的 `/api/user/models` 使用另一种身份，不混用。依据：[Token 与账户额度实现](https://github.com/QuantumNous/new-api/blob/main/controller/token.go)、[模型路由](https://github.com/QuantumNous/new-api/blob/main/router/relay-router.go)。

OpenRouter 等官方能力依据下列文档；Yunwu/Openlux 另有实测依据。Keystone 回归使用隔离数据库和本地协议桩，没有发送付费模型请求。

| 渠道 | 单次调用费用 | 账户余额 | 首版处理 |
|---|---|---|---|
| DeepSeek | 已核对的对话响应提供用量，未提供单次金额 | `/user/balance` 提供分币种的总余额、赠送余额和充值余额 | 保存用量；费用未知；首版不开放余额刷新（仅保留解析声明） |
| OpenRouter | 响应 `usage.cost`；也可按 generation ID 查询 | `/api/v1/credits` 返回累计 credits 和使用量，需要 management key | 保存上游报告费用；必要时查询补全；支持余额刷新 |
| 阿里云百炼 | 尚未确认可按推理请求 ID 查询单次实际金额的接口 | `QueryAccountBalance` 查询阿里云账户，区分可用额度和现金余额 | 保存用量；费用未知；首版不开放账户余额查询（仅保留解析声明） |
| Yunwu/Openlux | 原 Token 分页拉取日志，本地精确匹配 `other.request_id` 与 `x-api-request-id`，按站点口径解析 quota | 当前适配未接入账户余额查询，Token 用量不视为账户余额 | 已接入自动核对和手动补查；详见第 3.6 节 |
| NewAPI（标准协议） | Bearer Key 查询 `/api/log/token`，精确匹配网关请求编号，按公开 `quota_per_unit` 换算系统 USD 额度 | `/api/usage/token/` 的 `user_quota` 查询账户余额，当前 Key 额度及不限额状态单独展示；未授权返回账户额度时明确标示 | 自动核对与手动补查、模型列表、账户与 Token 额度均已接入 |

DeepSeek 的余额解析保留接口返回的币种，总余额已经包含赠送和充值部分，不重复相加；首版未开放查询，因此余额状态为 unavailable。费用未知仅表示当前接入能力不足，不断言供应商永远没有相应能力。依据：[余额接口](https://api-docs.deepseek.com/api/get-user-balance/)、[对话响应](https://api-docs.deepseek.com/api/create-chat-completion/)。

Openlux/Yunwu 尚未接入账户余额查询表示 Keystone 的适配范围，不断言供应商没有相应接口。Openlux 官方区分推理 API Key 与账户管理的 System Access Token，管理接口还要求 `New-Api-User` 用户 ID；不能直接把推理 Key 或 Token 限额当作账户余额凭据/余额。依据：[Openlux 接入与鉴权说明](https://doc.openlux.ai/)。

管理者可通过 `GET /api/v1/model-channels/{id}/models` 使用渠道已保存的推理 Key 查询上游模型列表。模型查询在完整 API 前缀后追加 `/models`；OpenRouter 使用固定地址，NewAPI 使用保存的HTTPS 地址，均为有界 GET，不跟随跳转或自动重试；返回去重排序后的模型 ID，不返回原始元数据或秘密。查询在事务外完成，返回前复查权限及配置。控制台选择渠道后自动加载，也可刷新或手填；缺失的当前模型会提示核对 ID/Key 权限，不自动替换或停用配置。模型列表不自动创建 ModelEntry，不声明操作能力、价格或可调用性；管理员仍确认业务 key 与操作能力。Openlux 的入口依据：[官方接入说明](https://doc.openlux.ai/)。

Openlux/Yunwu 的模型、状态和 Token 日志路径属于 New API 兼容接口族，归入 Yunwu 选项；金额单位和原生任务仍按已验证的站点规则处理。NewAPI 默认使用 NewAPI AI 商业站点，也接受其他HTTPS API 地址及路径前缀。NewAPI AI 的文本及模型列表接口依据：[站点文档](https://docs.newapiai.com/)。Kling 原生图片任务仍仅允许已验证的 Openlux 站点；生成类媒体协议需要对应站点的额外接入；普通 Token 额度不视为账户余额，标准 NewAPI 的账户余额仅取明确的 `user_quota` 字段。

OpenRouter 的 `usage.cost` 表示该账户被收取的 credits；流式用量在最后的 SSE 消息中。不要用 `upstream_inference_cost` 替换账户费用，尤其 BYOK 还涉及外部供应商收费。余额由 `total_credits - total_usage` 得到，保留 credits 口径，不能当作某个 API Key 的独享额度。依据：[用量与费用](https://openrouter.ai/docs/cookbook/administration/usage-accounting)、[余额与凭据要求](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits)。

阿里云余额接口的 `AvailableAmount` 是可用额度，`AvailableCashAmount` 是现金余额，展示时分别标明；首版仅有解析声明，不发起查询。它们属于阿里云账户，可能同时供其他云产品消费，并非百炼专属余额。查询需要阿里云 OpenAPI 账务授权，不能直接使用 DashScope API Key。依据：[账户余额接口](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-queryaccountbalance)。

百炼账单存在出账延迟和聚合口径，出账也不等于即时扣款。首版不抓取控制台、不把聚合账单摊分成单次“实扣”；后续确认能够精确关联请求的官方数据后再补全。依据：[账单与费用管理](https://help.aliyun.com/zh/model-studio/bill-query-and-cost-management)。

### 3.4 Flow 的固定模型选择

**当前实现。** 用户在 model_bindings 中按角色提交 ModelEntry key，受理时解析全部角色，固定 model_entry_id、channel_id、provider_model、provider 和 api_base，写入 format=2 快照。业务 input 不混入 Selection；客户端不能伪造解析结果。角色、文件引用、Run 和审计同事务提交，见[Flow 角色绑定](flow.md#3-受理与幂等)。

同一 Run 始终使用保存的选择，不在每次执行或恢复时重新解析当前 key 映射。每次真正发送前仍检查当前用户、原个人令牌、原 ModelEntry 与原 Channel 是否有效；原配置停用或不存在则明确拒绝。对 format=2 Run，Entry 的 channel_id/provider_model 或 Channel 的 provider/api_base 与固定值不一致即拒绝。旧 Selection-in-input 数据保持原样，旧在途执行仍由原制品收尾。

允许原渠道在同一账户内轮换凭据，发送时读取当前秘密。原渠道 api_base 与保存值不一致时拒绝发送，不能静默改用新地址；更换供应商或账户必须新建 Channel。ModelInvocation 记录这次实际采用的渠道和模型。

Token 级查费适配固定原查询范围，换 Token 按第 2.1 节新建 Channel。已有 Run 的固定选择不改到新渠道，不能为完成旧查费而放宽 Flow 的新请求授权或目标校验。

Flow 的 request_digest 依据用户原始提交计算，不包含服务端解析结果。重复发起先按原请求检查幂等并返回原 Run，不因当前映射变化而重新解析或改写其 input/绑定快照。

### 3.5 Flow 模型请求的恢复与防重发

复用 ModelInvocation.id 作为每次持久模型请求的稳定标识，由执行上下文确定，恢复同一请求时必须得到同一 ID。不能只使用 agent_call_id：一次 Agent 调用中的多轮模型请求各有自己的 ID。

1. DBOS 已有可恢复的模型结果检查点时，直接复用结果，不新增调用记录、不再次发送。
2. 需要执行请求时，在发送边界原子创建 ModelInvocation，利用 ID 唯一约束取得首次发送资格。只有首次成功登记的执行者可以发送；冲突或不确定是否登记成功都不能取得发送资格。
3. 已有调用记录但没有可恢复的结果时，停止该次执行并报告明确错误，例如 `model_result_unavailable`；即使调用状态为 succeeded 也不能重发，因为费用记录不能替代丢失的模型响应。Flow 的恢复失败不覆盖已经确认的调用成功状态。

该规则宁可在“已登记、尚未发出”时中断，也不在恢复时猜测可以重发。可靠的上游查询可以核实原请求结果或费用，但不能借查询重新发起推理。首版不增加发送意图表、额外状态或保存模型响应的字段，也不使用 File 保存防重发标记。

这是复用现有调用表的设计选择，不等于供应商侧 exactly-once 保证。实现时必须验证稳定请求身份、原子登记以及 DBOS/Agent 内部恢复边界；仍用于恢复防重发的调用记录不能提前清理。

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

依据为 2026-10-21 的实际图片调用及只读查费探针；2026-10-22 复核了站点公开金额配置。Keystone 使用该协议实现并通过本地回归；上游真实出账时效和大流量容量仍需部署后观测。

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

本节的固定站点口径适用于已验证的 Openlux `https://api.openlux.ai/v1`、Yunwu `https://yunwu.ai/v1`。调用保存适配器口径；每轮日志查询先读取公开 `/api/status` 校验 quota_per_unit=500000、对应币种。Openlux 为 USD，Yunwu 要求 CUSTOM 且 custom_currency_exchange_rate=1，落库单位为 yunwu_credits。变化时 blocked/cost_policy_changed，不用新口径解释旧调用。只解析 type=2、非负整数 quota，并校验匹配日志的模型名。

#### 3.6.3 回执、查询范围与凭据

回执观测由适配器明确选出可查费编号，尽早保存到调用记录；可保留少量其他响应编号供诊断，不能被响应体模型 id 无条件覆盖。Yunwu/Openlux 优先使用已验证的 x-api-request-id，OpenRouter 保留其 generation ID 规则。没有合法编号时费用保持未知，不生成猜测编号；硬中断发生在编号持久化之前时不能承诺自动找回费用。

查询范围由实际账务站点和原凭据可见范围确定。原 Token 与其他 Token 不能只因账户相同就混查；首版换 Token 新建 Channel，旧渠道保留必要密文凭据。范围未变的轮换仍须可验证，无法确认则阻塞，不静默替换历史范围。相同真实 Token 重复配置为多个 Channel 时也属于同一协调范围，不能靠不同 channel_id 绕过防重或限流。

任务参数只包含调用、渠道、范围标识和受控分页位置，不包含秘密或完整日志。凭据在受控服务内执行时解密，查询前后复核原范围和配置；变化后的旧结果丢弃。key 位于 URL 查询串时，日志、HTTP 异常、追踪和 DBOS 保存的错误均须脱敏，不保存完整 URL 或上游原始错误；只保存费用所需的规范化证据，不保存日志里的账号、IP 或完整 content。只访问显式允许的 HTTPS origin、路径和方法，不自动跳转。

#### 3.6.4 调度与分页推进

调用结束的短事务同时保存回执和费用待办，API 不负责即时投递。只有已结束且费用核对 pending、next_check_at 到期的记录可被扫描；运行中的调用沿用原收尾机制，不能为了查费按时间改判 unknown。调用 succeeded、failed、unknown 均可能产生费用。

model_access/tasks/workflows.py 由 composition 显式装配：启动投递一次，此后 DBOS 每 5 秒扫描。稳定计划名为 keystone.model_access.reconcile_costs。扫描使用 keystone-model-maintenance 串行队列，每轮最多投递 20 个到期范围；外部查询使用 keystone-system，默认全部 Worker 与 API 手动入口共享查费并发 2，同一查询范围串行，并与推理容量隔离。Worker 仅提供 DBOS 执行包装，费用扫描和核对规则由本模块 Service 提供，计划与容量语义保持不变。

请求直查按调用处理；日志查询按原 Token 范围和相近时间窗口合并，单页可确认多次调用。每次执行限制页数和总耗时，单次 HTTP 使用明确超时；未完成则保存固定起止窗口、下一页和本轮匹配范围，后续继续。该小型 checkpoint 归原 Channel，费用和进度协调写入，不新增分页任务表。下一页只在本页匹配结果提交后推进，崩溃后可安全重读同一页。

当前每批最多 20 条调用、每页 20 条日志、每次最多 2 页/25 秒，单次 HTTP 最长 10 秒。窗口从本批最早登记时间前 120 秒到本轮开始时间；后续分页不修改边界。未扫完约 5 秒后继续，完整覆盖后才确认费用。相同站点/Token 通过服务端 HMAC 生成不含秘密的 scope，选最早创建的 Channel 保存共享 checkpoint、阻塞和冷却；API 与 Worker 共用 PostgreSQL session advisory lock，查询期间不持有业务事务。

每进程最多 2 个查费流程，取得数据库协调连接之前即受限，避免大量手动补查占满连接池。Yunwu 分页日志响应上限 256 KiB、标准 NewAPI 最近日志上限 4 MiB、公开口径响应上限 1 MiB；只保存有界请求 ID、模型名、额度及单位等规范化证据，拒绝过深 JSON 或异常结构。

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

手动补查保留现有 model:manage 和记录读取权限，与自动核对共用范围互斥、适配器和保存规则，不另建一条可绕过凭据范围的路径。日志查询超出同步预算时保存待办并返回尚未确认的状态，不在 API 请求中扫描无限页。对 blocked/exhausted 的重新触发由授权管理者显式执行，不因普通页面轮询无限重置预算。

#### 3.6.6 费用状态与落库

费用字段、到期索引及发送归属直接包含在 model_access 基线迁移中，不再单独追加。内部 JSON 和范围不通过接口自动暴露。

| 归属 | 需要持久化的事实 |
|---|---|
| ModelInvocation | cost_check_status：pending、confirmed、blocked、unavailable、exhausted；cost_check_reason、cost_check_rounds、cost_check_errors、cost_checked_at、cost_next_check_at、cost_deadline_at，显式返回调用详情与列表 |
| ModelInvocation | cost_context 保存原站点/凭据范围和口径，receipt_ids 保存必要响应编号，cost_evidence 保存规范化额度、单位、口径和证据时间；三者为内部字段 |
| Channel | cost_scope、cost_context、cost_block_reason、cost_next_allowed_at、cost_checkpoint 为内部字段；凭据仍使用原受控密文，不进入 checkpoint |

cost_check_status 与调用 status 分开，查到账务不改变 succeeded/failed/unknown，也不把已结束 Flow 重新置为运行中。已知费用仍使用 cost_amount、cost_unit、cost_source；查询写入 source=query，明确可信的响应费用使用 response。未确认金额和单位保留 null，只有上游明确报告零消耗才记录 0。查费错误另存，不覆盖模型调用 error_code。

重复结果只补全同一调用，不创建新 ModelInvocation、不累加金额或用量。保留 Numeric(38,18) 和 Decimal，不能经过二进制浮点估价；按渠道及单位汇总，不跨旧 Yunwu 自定义额度、Openlux USD、OpenRouter credits 或人民币参考值相加。查询失败不清空已知金额；新的可信实际查询替换原响应费用时保留前后证据，矛盾记录不能静默覆盖。

外部 HTTP 在短业务事务之外；写回前检查原查询范围、配置、调用状态及本轮 checkpoint。费用、证据、进度和需要的审计同事务保存，旧查询或失去协调所有权的执行不得推进新轮次。常规自动确认保存在调用记录及必要证据中，不为每页或每次未找到复制管理审计；显式重查仍写 `model.cost.recheck`，保留实际操作者与原核对状态。 新支持协议的旧调用缺少 cost_context 时，仅在原渠道配置自调用前未改动且已保存网关回执的情况下，允许补建查询范围并记录 `model.cost.bind`；范围、回执与审计同事务写入，仍不重新发送模型。

确认后停止自动轮询，本阶段不自动推断退款、负数费用、冲正或长期账单修订。不明历史口径、矛盾记录和窗口耗尽均保留未决；不能把已知费用汇总称为全部调用的最终实扣。

#### 3.6.7 多 Worker 的协调与恢复

查费允许重复只读查询，费用确认保持幂等。所有 Worker 和 API 手动补查通过 PostgreSQL 共享查询容量、真实 scope 会话锁、Channel 分页 checkpoint 和持久冷却。准备与写回在持锁连接的短事务内校验所有权，连接失效后禁止换连接提交。写回比较原配置、轮次和 checkpoint revision，每次推进递增 revision，防止旧页结果覆盖新进度；不同 Channel 指向同一真实 Token 时也不能并行推进同一范围。

DBOS 投递去重不代替上述保护，进程内 Semaphore 只控制本地资源。查询总容量默认 2，可通过 model_cost_concurrency 调整；拿不到容量时自动待办保留、手动入口返回忙碌，不消耗错误预算。Retry-After 在实例间共享，不能将预算乘 Worker 数。固定身份、按旧实例收尾及原身份重启恢复见[整体架构第 3.5 节](../architecture.md#35-多-worker恢复与发布)，不自动接管故障槽位。

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

Channel、ModelEntry 的删除按第 2.2 节引用规则执行；管理变更、改名、启停、删除和凭据变更记录审计。成功删除与成功审计同事务，失败时均回滚。改名不修改选择、凭据、余额或费用范围；删除检查在持有配置行锁的事务内进行，Flow 受理绑定使用相同行锁顺序，查到调用或快照引用时返回 409。Flow 自己的公开查询负责识别新快照与旧输入中的 Selection/model_key，由 composition 显式注入查询函数，模型领域不读取 Flow ORM。调用记录的保留策略不变：ModelInvocation 不提供普通用户修改/删除接口，仍承担 Flow 恢复防重发作用的记录必须保留，确认对应运行不再有恢复或协调义务后才能按保留期清理，不在这里扩展成归档领域。

不为这三个对象增加 deleted_at、deleted_by。首版不提供调用记录的删除或归档入口，也不逐条增加删除字段；未来清理须记录任务范围、数量和结果，并保留仍有恢复义务的记录；已有审计独立保留。首版不采用保留连接行、设置 deleted 并清除秘密和授权的删除模型，也不引入相关协调机制。

管理操作的审计日志与调用记录用途不同：前者追踪谁改了配置，后者记录调用发生及费用信息。两者都不保存秘密明文，调用记录默认不保存提示词和输出正文。

## 5. 接口与调用

本节记录当前实现约定，代码职责见第 8 节；自动核对的完整状态、分页 checkpoint、重试和迁移规则见第 3.6 节。手动与自动核对共用 CostService，不分别维护账务写入逻辑。

代码位于 backend/src/keystone/platform/model_access/，model_access 领域初始迁移建立 model_channels、model_entries、model_invocations，渠道直接使用凭据密文字段。受控发送、运行时凭据注入、响应观测和安全错误分类按发送前登记规则实现；没有复制路由池、配额、聊天会话或其他领域实体。

### 5.1 配置

模型始终使用真实服务，不提供 disabled/mock/live 模式和发送开关。渠道创建时填写 api_key，可选 billing_api_key；编辑时省略或传 null 保留原密钥，clear_billing_api_key=true 清除账务密钥。输入采用 SecretStr，响应、审计和日志都不返回明文或密文。API 与 Worker 读取同一数据库，变更立即生效，无需重启。配置步骤见[模型接入](../../README.md#模型配置手动)。

加密使用 Fernet，绑定渠道 ID 与推理/账务用途，防止凭据密文跨渠道或用途替换。首次保存时在现有 FILE_COORDINATION_ROOT 自动原子生成 `.model-credentials.key`（Linux/macOS 文件权限为 0600），不增加配置项或初始化命令。API 与 Worker 共用该协调目录；已有密文而密钥丢失时拒绝密钥写入和调用，不静默重新生成。备份应保留完整协调目录。实现依据 [Fernet 文档](https://cryptography.io/en/latest/fernet/)。

当前支持 OpenRouter 固定地址及 NewAPI 的HTTPS API 地址，普通目录只返回启用且支持发送的渠道模型；DeepSeek/阿里云既有配置仍可管理，但不能发送，也不开放余额查询。缺少或撤销凭据时在发送前拒绝。启动或保存配置不会自动发起模型调用。

确定性响应只通过测试目录中的 HTTP 传输替身注入，不随应用包或部署镜像发布。旧开发模拟数据不得用作真实调用的费用或恢复依据，应使用独立数据库和文件卷。

### 5.2 接口与权限

以下路径统一带 /api/v1。配置 PATCH 接收完整非秘密可编辑字段；凭据字段可省略以保留原值。Channel.provider 和 ModelEntry.key 创建后不可编辑；已填写账户标识不可更换，原为空时可补录。显示名称可通过完整编辑或独立 name PATCH 修改；独立改名只接收 name，不要求重新提交渠道连接或凭据。

| 接口 | 要求与用途 |
|---|---|
| GET /model-access/options | model:manage；返回支持的服务商列表 |
| GET/POST /model-channels；PATCH /model-channels/{id} | model:manage；查询、创建、编辑和启停渠道 |
| PATCH /model-channels/{id}/name；PATCH /model-entries/{id}/name | model:manage；快捷修改显示名称，保留模型 key 和固定选择 |
| DELETE /model-channels/{id}；DELETE /model-entries/{id} | model:manage；未被引用时删除并返回 204，有引用时 409，不级联删除 |
| POST /model-channels/{id}/balance | model:manage；同步刷新，失败保留旧快照，返回查询状态 |
| GET/POST /model-entries；PATCH /model-entries/{id} | model:manage；查询、创建、编辑和启停模型选择 |
| GET /models | model:invoke；只返回可用 key、显示名称和调用能力，不暴露渠道凭据配置 |
| POST /models/invoke | model:invoke；消息文本调用，stream=true 返回 start/delta/done/error SSE 事件 |
| GET /model-invocations；GET /model-invocations/{id}；GET /model-invocations/summary | read 限本人，read + read_all 扩大范围；明细、分页与汇总使用相同归属过滤 |
| POST /model-invocations/{id}/cost | 同时要求 model:manage 与该记录读取权限；仅补查已结束记录，更新费用，不改变调用状态 |

渠道和模型列表默认 50 条、最多 100 条；调用历史默认最近 30 天、范围最多 366 天，支持时间、渠道、FlowRun 过滤，列表另可按状态过滤。汇总在一个 SQL 快照内按状态计数，已知费用按渠道/单位分别求和，未知费用单独计数。数值以十进制解析，费用用 Numeric(38,18)，API 返回十进制字符串；只保留上游明确报告的用量字段，不补算缺失 token 或价格。

管理页提供配置、启停、余额、非流式调用、记录和费用汇总；所有权和 PAT 范围仍由服务端当前授权决定。只有历史权限时不会取得配置；只有调用权限时不会取得历史。当前分页选择、筛选及详情交互见 7.7。

### 5.3 发送与故障规则

NewAPI 的 `api_base` 保存完整 API 前缀，系统只追加已实现协议的路径，不允许凭据、查询参数、片段或路径穿越。自定义地址直接使用保存的域名和系统网络解析，可用于内网及代理 Fake-IP 环境；不做公网 IP 判定或预解析固定 IP，不自动跳转。NewAPI 与 Yunwu 类按各自协议核对费用，媒体能力仅在已验证站点启用。相同已验证站点与 Token 的新旧渠道使用一致的账务口径，共用原费用核对范围，保留历史记录与原凭据。通用渠道的查费范围同时区分协议及 API 前缀，避免根路径与 `/v1` 配置互相占用不同的查询上下文；既有 `/v1` 范围保持不变。

- 每次进入 SDK 的实际 POST 发送边界，重新校验身份、model:invoke、原模型/渠道启用状态、固定地址与当前秘密。短事务原子插入 ModelInvocation 并提交后才允许发送；登记失败不发送。
- SDK max_retries=0，禁止自动跳转和环境代理；相同可信 invocation_id 的并发或重放拒绝再次发送，包括原调用已成功但没有可恢复响应的情况。公开 HTTP 只接收 key/messages/max_tokens/stream，不能提交 user_id、Flow 上下文、固定目标或内部调用 ID。
- 已明确拒绝的 4xx 记录 failed；402 映射为 model_insufficient_credits。超时、5xx、断流、中断或无法解析的结果记 unknown。HTTP 200 中的 error 或 finish_reason=error 同样按未知结果处理，返回安全错误码，保留已经收到的编号、用量和费用，不暴露上游原始错误。SSE 必须同时收到完成原因与结束标记；完整响应但没有 usage/cost 仍可成功，费用保持未知。失败与费用是否已知分别判断。
- 已发送请求即使身份撤销或配置停用，也补全原记录；不因此再发请求。补全写入失败返回 model_result_unavailable，可能遗留 running，不自动重发或按时间猜测失败。稳定 ID 的防重发记录仍保留。
- 文本消息最多 32 条，内容总计不超过 64 KiB；max_tokens 默认 1024、上限 4096。单进程模型调用/查询并发 4，文本调用执行最长 60 秒、SDK I/O 超时 30 秒；响应观测最多 4 MiB，单条流事件缓冲 64 KiB。取消后的事实收尾最多等待 5 秒，不留下后台发送重试任务。
- 默认不保存消息或输出正文。记录必要用量、上游请求 ID、费用来源及稳定错误代码；OpenAI/httpx2/httpcore2 日志不随应用 DEBUG 输出请求体和认证信息。失败时在 API/Worker 输出结构化诊断日志，关联调用 ID、HTTP 请求 ID、固定渠道/模型、失败阶段、耗时、报文字节数、上游状态与请求编号、异常链类型及代码位置；不记录异常原文、局部变量、URL、提示词、结果正文或认证头。上游错误 code/type 仅保留明确白名单；`context_length_exceeded` 在明确的 400/413/422 拒绝中映射为 `model_context_length_exceeded`，网络异常映射为 `model_connection_error`，仍保持 unknown 且不重发。回执及结果保存失败在包装为公开错误前保留原异常类型与位置。日志查询见[调用失败排查](../deployment.md#21-调用失败排查)。

余额查询外部 I/O 最长 10 秒；每进程最多 2 个余额刷新，同渠道通过 PostgreSQL 会话锁协调，协调连接退出时释放，业务事务不跨外部 I/O。写回前核对配置，旧查询结果丢弃；失败保留上次成功快照，配置修改使余额来源变化时清空。余额采集不改 Channel.updated_at/updated_by；查询发生及结果进入 AuditLog。费用补查失败保留已知费用，重复查询只替换同一次费用，不累加。

### 5.4 Flow/Agent 接入

FlowModel 通过 ModelRuntime.invoke_sdk 内部入口，直接传入固定 Selection、稳定 invocation_id、InvocationContext 和 sdk_request 回调，把 Pydantic AI 的协议适配置于受控客户端内；不构造公开 Invoke 的占位消息，普通模型调用与 Agent 每轮请求仍共享发送登记和响应后记录。公开模型 API 不因此增加工具、身份、稳定调用 ID 或固定选择参数。实际请求体超过 256 KiB 在发送前拒绝。

真实 DBOS Worker 使用 workflow/step 身份派生稳定 UUID。Agent 的每轮模型请求保存独立 ModelInvocation，同属一次 Agent 调用的记录共享 agent_call_id。已登记未发送、已收到响应但缺少 DBOS 检查点这两个崩溃窗口均阻止重发；已确认成功和费用保留，未知仍为 unknown。取消发生在发送后也会保存已返回的用量/费用，新请求被阻断。没有新增费用快照、账本、定价或消息表。

### 5.5 普通 API 调用离线收尾

API 硬退出或结果写回失败后，操作者先停止连接同一数据库的全部 API 进程，并在命令执行期间保持停止，再运行：

```shell
keystone recover-model-calls --confirm-api-stopped
```

该标志是操作者对停止状态的明确确认，命令不会探测本机或远端 API 进程，也不会自动停止它们；不能在仍有普通调用执行时使用。缺少确认直接拒绝，数据库迁移不匹配时也拒绝。确认完成后才能重新启动 API。若有自定义普通调用进程，也须一并停止。

入口调用 model_access/services/maintenance.py，仅处理 status=running 且 flow_context 中没有 flow_run_id 的普通调用。每批最多 100 条，在锁定后将状态置为 unknown、error_code 置为 model_interrupted，填充 finished_at/updated_at；保留原 ID、请求编号、用量、费用及归属。每条 model.invocation.recover 系统审计与该批更新同事务；批次失败回滚，之前已提交批次保留，重跑不重复修改或审计。成功结束记录总处理数。

命令不发起任何上游请求、不重发推理、不删除记录；已经结束的调用及 Flow 调用均不处理。Worker 可以继续处理 Flow。收尾后，具备原费用补查权限的用户可通过已有 cost 接口查询；能否确认费用仍取决于上游编号和供应商能力。保持原有三张表，不增加心跳、租约或恢复任务表。

### 5.6 控制台

控制台提供两个独立入口：`/console/models` 为模型接入，使用配置管理与模型调用权限；`/console/model-usage` 为用量与费用，使用 `model_invocation:read`，具备 `model_invocation:read_all` 时扩大至其他用户和系统调用。统计页不依赖模型配置管理或用户管理权限，不返回 API 地址、余额、凭据或供应商原始内容。

模型接入按渠道配置、模型配置、调用测试切换。渠道与模型配置使用独立编辑弹窗和 50 条分页；渠道选择器、可用模型选择器也支持分页。服务商、模型 key 及已确定的账户标识不可修改，名称可快捷修改，未引用配置可确认删除，有引用时提示改为停用；密钥保存后仅显示设置状态。渠道与模型可跳转到带对应筛选条件的费用页。余额保留在渠道配置中：列表仅显示主要剩余额度、Key 限额标记和最近采集状态；账户已用、Key 明细、尝试时间及成功采集时间放在额度详情抽屉。刷新只更新对应渠道，保留已打开的抽屉；查询失败时明确保留的是历史成功快照，不与本系统的期间消费相加或对账。

用量与费用包含总览、维度分析、调用明细。统一筛选时间、渠道、业务模型、上游模型、用户或系统、普通/Flow/Agent 来源、运行、节点、模型角色、Agent 及调用 ID、操作、调用结果、费用核对状态、费用来源、费用是否已知、单位及错误码；可进一步筛选缺少流程关联等记录。默认最近 30 天，最多 366 天；小时分桶最多 31 天，所有时间分桶、日期快捷选择、日期输入及调用时间显示统一使用上海时区（Asia/Shanghai、UTC+8），不提供单独的时区选项或分析维度。首次加载固定时间范围，列表翻页沿用同一范围；相对快捷范围的手动刷新推进起止时间并回到明细第一页，自定义范围保持不变，草稿只在应用筛选后生效。费用补查刷新保留原范围及抽屉，没有自动统计轮询。

分组维度为渠道、业务模型、上游模型、用户、来源、Flow 运行、节点、模型角色、Agent、操作类型、调用结果、核对状态、费用来源和错误码。按调用次数或指定单位的已知费用排序，分组在服务端分页。汇总、趋势、分组计数及各单位金额由同一 SQL 快照计算，分页不影响总览或趋势。主体名称只解析当前可见调用涉及的渠道、模型及用户；筛选选项按名称或完整 ID 搜索并分页，不能借此枚举不可见主体。

统计金额按原始单位分别累计，包含失败调用已有的真实费用；未获得金额的调用单独计数，不能算作零费用。费用覆盖率是已有金额调用数 / 全部调用数；每个单位的单次平均费用仅以该单位已有金额的调用为分母。Token 输入/输出分别累计已报告的合法整数，报告覆盖数独立展示；缺失用量不估算或置零。平均/P95 耗时从调用登记到完成计算，排除未完成或时间异常的记录，明确完整时间覆盖数，不宣称为供应商纯推理延迟。

点击趋势柱形或分组进入相同条件的调用明细，保留其他筛选条件；时间桶钻取采用上海时区边界。URL 保存筛选、时间范围、分组、排序单位、分页、详情及当前页签，支持浏览器返回和分享。调用详情沿用原费用补查，流程入口仍要求 Flow 查看权限并由 Flow Service 复查对象范围。权限变化时清空统计、主体选项与旧详情，再按新范围加载；明细和统计错误分别显示。

新增只读接口与历史列表/汇总使用同一筛选定义：

| 接口 | 用途 |
|---|---|
| `GET /api/v1/model-invocations/statistics` | 返回汇总、上海时区趋势及分页分组；`group_by`、`bucket`、`order_by`、`sort_unit` 控制分析方式 |
| `GET /api/v1/model-invocations/options` | 根据当前调用范围分页搜索 `dimension` 对应的名称/ID；不要求配置管理权限 |

本次复用现有调用字段、JSONB 上下文和时间索引，不增加领域表、物化汇总或迁移。

控制台测试调用固定提交 stream=false，不提供流式响应开关，完成后一次性展示结果。按钮为“发送真实请求”，提示可能计费，打开页面或保存配置不会自动调用。Flow 发起表单同样提示真实调用。停止接收只中断浏览器等待，不表示上游撤销或没有费用，不自动重发；离开页面中断等待并清空输入输出。公开 API 的流式协议保留，HTTP 错误和流内 error 事件共用会话失效处理。配置变化后可手动刷新模型目录，实际发送仍复查当前配置和权限。

操作隔离和失效结果处理沿用本后台的 Element Plus 弹窗/详情抽屉、分页及请求代次控制；不引入通用连接授权或多模态探测功能。

### 5.7 OpenRouter 接入

沿用现有 OpenAI SDK、受控 transport 和 OpenRouter 费用适配，不增加网关、SDK 依赖或领域表。实际发送使用 httpx2.AsyncHTTPTransport(retries=0, trust_env=False)，SDK 同样禁止重试；忽略 Retry-After，不自动重发计费请求。只允许 `https://openrouter.ai/api/v1`，发送前逐次核对完整地址、POST /chat/completions、/embeddings 和固定模型；账务查询只允许 GET /generation、GET /credits，拒绝跳转和自定义代理地址。

| 能力 | 凭据与口径 |
|---|---|
| 普通/流式推理、Agent 工具调用 | api_key 使用普通推理 API Key；工具调用仅由可信 Flow 适配器使用，公开 API 不增加工具参数 |
| 响应费用 | 使用 usage.cost，单位 credits；不把 cost_details.upstream_inference_cost 当作账户扣费，不根据 token 估价 |
| 费用补查 | 使用原渠道推理 Key 请求 /generation?id=上游编号，验证响应 ID 后读取 total_cost；失败保留原费用，不重发推理 |
| 账户余额 | billing_api_key 使用同一账户的 Management Key；/credits 的 total_credits − total_usage，作为账户剩余额度快照，不是单 Key 限额 |

账务凭据可不配置，此时余额 unavailable，不影响推理；凭据不足或查询失败不能清零旧快照。账户归属由部署者核对，当前没有自动认证两个 Key 属于同一账户的能力。控制台直接录入密钥，保存后仅返回已配置状态，不回显内容。

依据 OpenRouter 官方[用量与费用](https://openrouter.ai/docs/cookbook/administration/usage-accounting)、[账户 credits](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits)、[Generation 查询](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation)及[错误处理](https://openrouter.ai/docs/api_reference/errors-and-debugging)实现协议适配。配置步骤见[OpenRouter 接入](../../README.md#模型配置手动)。


## 6. 模型操作与产物

### 6.1 操作与固定选择

ModelRole.operation 描述需要执行的操作。ModelEntry.operations 描述管理员为该配置确认的能力；协议适配器描述宿主实际实现的能力，三者必须匹配。能力不从任意模型名猜测，也不因渠道支持图片而认定全部模型都支持图片。

| operation | 请求 | 结果 | 当前状态 |
|---|---|---|---|
| text.generate | TextRequest | TextResult | 已实现 |
| text.embed | EmbeddingRequest | EmbeddingResult | OpenRouter、NewAPI 的同步 /embeddings 协议，模型须显式声明 |
| text.structured | StructuredRequest | StructuredResult | OpenRouter、NewAPI、Openlux/Yunwu 的兼容 Chat Completions；模型须显式声明，本地严格校验结果；OpenRouter 另要求 provider.require_parameters |
| image.understand | VisionRequest（受控 File ID） | TextResult | OpenRouter、NewAPI、Openlux/Yunwu 的兼容 Chat Completions；仅受权 PNG/JPEG/WebP，模型须显式声明该操作 |
| image.generate | ImageGenerateRequest（sync）/ ImageTaskRequest（async） | ImageResult（File ID）/ ImageTask | OpenRouter、Yunwu/Openlux 同步及 OpenLux Kling 异步已实现 |
| image.edit | ImageEditRequest（受控 File ID） | ImageResult（File ID） | OpenRouter、Yunwu/Openlux 同步已实现 |

兼容聊天协议的结构化输出统一发送严格 response_format.json_schema，不因模型名自动开启；依据：[云雾结构化输出](https://lrbebhrbp1.apifox.cn/api-417728203)、[NewAPI 请求类型](https://github.com/QuantumNous/new-api/blob/main/relaykit/dto/openai_request.go)。

Selection 保存 operation 和 delivery（sync / async），历史缺失分别按 text.generate / sync 读取。受理保存所需 operation；发送前核对角色操作、Entry.operations、协议能力及固定渠道/provider/model/base。修改其他能力不改写历史选择；移除本次所需能力阻止新发送。原凭据、权限、配置锁、取消、调用登记及费用规则继续生效。

ModelEntry.operations 非空、无重复、最多五项，未知枚举拒绝。目录 GET /models 可按 operation 和 delivery 筛选，分页在过滤之后；返回 capabilities/operations 与实际可发送交集。模型基线的 operations 默认 text.generate。OpenLux 图片任务另限制为已核验的 kling-v1、kling-v1-5、kling-v2、kling-v2-1；目录分页前过滤，受理与发送均复查，不能用任意模型名声明该原生协议。

### 6.2 请求与结果边界

结构化请求含 TextRequest 的消息/输出 token 以及明确 JsonOutput：名称和对象 Schema。Schema 本身有界，只解析本地引用、禁止远程读取；本地响应验证失败报告安全错误，不隐式修正重发。

图片请求使用有界 File ID 列表，发送前检查 Run 使用范围、当前内容权、MIME/实际文件格式、单图和累计字节。客户端不能提交远程 URL、data URI、本地路径或媒体字节；发送时编码为协议支持的图片输入。现有文本大小限制保持原值。

生成/编辑返回小型 ImageResult，包含 invocation_id 与文件 ID，不含 base64 或签名 URL。产物保存使用稳定逻辑键与原运行引用，失败不能遗留虚假的输出引用。当前 FlowContext 提供 invoke_structured(role, StructuredRequest, node_key=...) 与 invoke_vision(role, VisionRequest, node_key=...)，均在持久 Step 内使用；分别只允许匹配角色操作。图片理解及 OpenRouter 编辑最多四份、合计最多 8 MiB；Yunwu/Openlux 编辑最多九份、合计最多 32 MiB。每份仍最多 4 MiB、16M 像素，只接受单帧 PNG/JPEG/WebP，实际解码与 MIME 一致；取消等待原生校验完成。原媒体请求保持 12 MiB，Yunwu/Openlux multipart 请求上限为 32 MiB 加 128 KiB 封装；文本保持原 256 KiB。图片数量及累计大小在发送前再次按所选协议检查。

### 6.3 外部事实与恢复

发送意图登记、供应商任务 ID、可恢复结果、文件保存、运行完成分别确认。已有稳定调用 ID 无证据时拒绝重复发送；有任务 ID 时仅查询原任务；有原结果时仅取回原结果；文件已确认时仅继续原引用和结果落库。

可恢复证据由 Model Access 保管，文件和引用由 Files 保管，DBOS 保存步骤检查点，FlowRun 保存受理和业务结果。恢复阶段不保留长期数据库事务，不新增第二套流程引擎。查询/下载有独立有界重试与取消语义，不能把暂时失败当作未提交。

### 6.4 协议依据

实施采用官方文档并以本地确定性协议服务验证，不访问付费模型。能力按完成的 Adapter 逐项开放：

- [OpenRouter 结构化输出](https://openrouter.ai/docs/guides/features/structured-outputs)：请求携带 response_format，支持能力按端点而异，宿主仍校验最终结果。
- [New API 多模态输入](https://docs.newapi.pro/en/docs/api/ai-model/chat/gemini/geminirelayv1beta-391536411)、[云雾 Chat 识图](https://lrbebhrbp1.apifox.cn/api-417728221)：图片理解沿用 Chat Completions 的 image_url 内容块；Keystone 只发送经文件授权与校验的 Base64，不开放任意图片 URL。兼容渠道类型表示协议可用，不保证其每个上游模型支持图片。
- [OpenRouter 图片理解](https://openrouter.ai/docs/guides/overview/multimodal/image-understanding)：使用图片内容输入；Keystone 将受权 File 转换为受控协议值。
- [OpenRouter 图片生成](https://openrouter.ai/docs/guides/overview/multimodal/image-generation)：专用图片接口及 base64 结果；产物落为 File，不暴露大正文。
- [OpenLux 官方 Kling 规范](https://doc.openlux.ai/api/docs/public/specs/platform/kling)：原生图片提交、查询、任务状态与结果来源已逐项核验，未实现的音视频与编辑任务不开放。

### 6.5 图片产物

模型接入基线直接包含 ModelInvocation.operation（默认 text.generate）和可空 result_context。后者是受限证据，公共调用投影不返回它；不复用 cost_context，不保存提示词、base64、明文签名 URL 或供应商正文。Yunwu/Openlux 的受限取回凭据见第 6.7 节。

同步图片接口的 result_context 使用明确 ImageManifest：format_version=1、request_digest、operation 和 outputs；每个 output 只保存稳定 artifact_key、checksum_sha256、size_bytes、content_type。artifact_key 由 invocation_id 与序号派生，不接受客户端任意 owner/slot。最多两份输出、每份 8 MiB、总计 16 MiB；媒体 HTTP 响应单独有界，文本响应上限不变。

FlowContext 增加 invoke_image(role, ImageGenerateRequest, node_key=...) 与 edit_image(role, ImageEditRequest, node_key=...)，均在持久 Step 内使用。请求含有界 prompt、数量、分辨率/格式等明确参数；编辑引用仍只接受 File ID。返回 ImageResult(invocation_id, files)，每个 file 项只有 file_id。普通业务输出声明 files.*.file_id 即可复用原引用校验。

恢复顺序：

1. 以当前 DBOS Step 得到原 invocation_id，校验原身份、角色、operation、固定目标和请求摘要。
2. 已有 ImageManifest 时按原稳定键查询并验证全部文件及摘要；全部存在则复用文件 ID，不再次生成。缺少任何不可取回结果时安全结束，保留原调用及已保存产物。
3. 无调用记录时才允许原发送登记；已有调用却无可恢复证据，禁止重发。
4. 图片响应解析并验证完整批次后，原子保存生成成功与 ImageManifest；再逐份上传 Files，最后返回小型文件引用。
5. 文件已保存、Step 检查点尚未保存时重启，以原 Manifest 和文件恢复；响应或部分文件未持久化的同步接口没有虚构的“重新获取”能力。

异步接口的证据另用 TaskReceipt（原协议、task_id、请求摘要）；只查询同一任务，获取最终结果后再形成 ImageManifest。查询、下载与发布保持原身份和固定来源，轮询用原生 Workflow/Step/DBOS sleep 表达，不在 Context 创建隐式执行引擎。OpenLux 原生图片协议已接通；不能用同步接口补发替代异步恢复。

### 6.6 异步交付

官方 OpenLux Kling 文档确认 POST /kling/v1/images/generations 提交，以及 GET /kling/v1/images/generations/{id} 查询；任务标识在 data.task_id，状态为 submitted/processing/succeed/failed，结果在 task_result.images。渠道仍保存既有 https://api.openlux.ai/v1 配置；原生路径由适配器固定映射，不允许客户端改 origin。

角色增加 delivery=sync|async（默认 sync），Selection 与角色快照同样保存该值；历史缺失按 sync 读取。模型目录同时按 operation/delivery 过滤，受理与发送复查两者，避免把只能异步交付的模型绑定给只使用同步返回的流程。Entry.operations 仍描述语义操作，交付方式由已实现协议决定，不另建路由池。

异步使用 submit_image(role, ImageTaskRequest, node_key=...)，返回只有 invocation_id 的 ImageTask；image_result(role, invocation_id, node_key=...) 返回 ImagePending（含有界等待秒数）或 ImageResult。流程显式使用原生 Step 调用，pending 时使用 DBOS sleep，Context 不偷偷建立 Workflow。TaskRequest 仅含该协议已确认的 prompt、n、resolution，不把不支持的格式等参数静默丢弃。

TaskReceipt 持久化 protocol、task_id、request_digest、数量、原截止时间、查询次数与最早查询时间。提交 Step 恢复先复用原回执；查询不创建第二次 ModelInvocation，不换任务 ID。Worker 中断后的 unknown 调用可以凭原任务的明确结果转为已知成功/失败；原发送归属及费用保留。

ImageManifest 可保留原 TaskReceipt，保证“查询结果已取得、文件尚未完整保存”时仍能取回原任务结果。已确认输出的摘要不能被后续下载偷偷替换。临时获取错误返回 pending，由显式有界循环继续；原提交起 300 秒、最多 60 次查询、最早查询时刻均持久保存，跨重启不重置。退避只用于查询/取回原结果，不会重新发送生成。已确认生成成功后，下载失败不倒改生成事实；Run 可以失败而调用仍成功，费用另行核对。

下载使用适配器声明的 HTTPS CDN 来源与普通 HTTP 连接，拒绝 URL 中的凭据、非 443 端口和跳转；域名解析交由系统网络处理。下载不携带渠道 Authorization，字节与实际图片格式仍有界校验，URL 不进入日志、公开响应或持久回执。取消、原凭据或权限撤销、配置变化阻止新查询/下载/文件发布；本地中断保存原调用证据，不取消或重建供应商任务。

### 6.7 Yunwu/Openlux 同步图片

两个站点共用 OpenAIImages，NewAPI 按精确站点地址选择对应 TokenLogs 账务实现；原 provider 继续处理既有渠道，不迁移或合并历史渠道/币种。同步操作限制为 gpt-image-2、gpt-image-2-c、gpt-image-2.5-sunburst、gpt-image-2.5-flare；ModelEntry 仍须显式声明 image.generate / image.edit。

文生图 POST /images/generations 使用 JSON；编辑 POST /images/edits 使用 multipart，重复 image 文件字段。模型、路径、参数、文件数量与实际编码体均在登记前检查。生成与编辑走原发送登记，鉴权及唯一物理 POST 只能发生在提交之后，SDK 与传输不自动重试。

ImageGenerateRequest / ImageEditRequest 增加可选 aspect_ratio（1:1、16:9、9:16、4:3、3:4）及 quality（low、medium、high）。本协议省略时使用 1:1 / medium，resolution 沿用 1K/2K/4K；代码将比例和档位转换为精确比例、16 倍数且处于模型像素/边长限制内的 size。output_format 沿用 png/jpeg/webp。OpenRouter 不接受新增控制，既有报文及默认值不变。新增字段为空时不进入请求摘要，旧请求的摘要和 Manifest 仍可读取。

只有本同步图片协议将 SDK 总窗口及读写超时设为 600 秒，文本和 Kling 不变。此窗口包含发送协调等待和报文处理，不是后台任务承诺。流程作者须显式设置足够的 FlowDefinition.timeout_seconds；覆盖生成、最多两份结果下载及文件保存时建议至少 900 秒，原快照期限不自动延长。

返回 data[].b64_json 时校验真实图片并形成原 Manifest；支持匹配 MIME 的 data URI 前缀。返回 URL 时，先将原位置加密写为私有 ImageDownloadReceipt：绑定 channel_id、invocation_id、operation 和 request_digest，最多两份、单 URL 最多 3072 字符，密文及 Manifest 保持 16 KiB 数据库上限。取回凭据本地有效期最多一小时，上游签名可能更早失效；公开 API、日志及 DBOS 检查点不返回这些地址或密文。

每份下载最多三次尝试、两次跳转，总窗口 120 秒，单请求 30 秒、结果最多 8 MiB。每次请求/跳转重新检查原运行权限、固定模型/渠道和来源；仅 HTTPS/443，按原 URL 域名连接且不限制解析所得 IP 范围，跳转同样检查，CDN 请求不携带渠道 Token。下载失败只重试 GET，不重新生成。

上游有效 URL 回应落库时调用状态为 succeeded，费用核对可独立开始；这不代表文件或 Flow 已完成。下载后形成带原取回凭据的 Manifest，再按原 artifact_key 保存 Files；重启可从取回凭据、Manifest 或已保存文件继续，部分文件保存后不会多出另一份逻辑产物。已有 Manifest 与重新下载的摘要、大小或 MIME 冲突时拒绝覆盖。无结果证据、签名失效、密钥不可用或权限失效均禁止补发。恢复限原运行仍可执行的重启/重放，不自动重启终态 Flow。只有 Base64 且文件未保存的响应无法靠哈希取回，仍按原有不可恢复窗口结束，不承诺第三方 exactly-once。

图片费用沿用原 Token 日志核对：仅以 x-api-request-id 精确匹配 other.request_id 和上游模型；响应体 id、usage.cost 或余额差不作为本渠道实扣证据。缺回执为 unavailable/cost_request_id_missing，缺日志保持 pending，模型或账务口径冲突为 blocked。生成失败或产物不可用仍可保存实际查得的费用；重复补查只补全原调用。Yunwu 的自定义额度与 Openlux USD 按原范围保存，不因共用协议而换算或合并。

该实现按 EP 现有 Yunwu 报文与本地真实 SDK/DBOS 协议桩验证，没有执行付费线上验收。New API 的公开 generations/edits 形状相近，但其示例使用 image[]，具体模型与结果取回能力也可能不同，不能据此宣称通用 New API 完全兼容；核验实际站点后再扩充代码声明。参考[New API 图片调用说明](https://github.com/QuantumNous/new-api-docs-v1/blob/main/content/docs/en/plugins/usage.mdx)。

### 6.8 直接调用与控制台测试

控制台「模型接入 → 调用测试」复用现有 Element Plus，支持文本、图片生成、图片编辑和图片理解。模型目录按 operation 和 sync 交付过滤；图片目录按本次 operation 和 delivery 返回协议实际支持的 image_parameters（比例／画质）、max_reference_images 和 max_input_bytes；同一个模型的编辑与理解限制分别计算，前端直接使用目录值。不支持的可选参数不显示、不静默丢弃。异步上游任务仍使用原有 Flow 接口，本测试入口不创建 FlowRun、后台任务或额外测试账本。

| POST 接口 | 请求与响应 |
|---|---|
| `/models/invoke` | 原有 Invoke 文本契约，保持 JSON/SSE 行为 |
| `/models/images/generate` | model_key + ImageGenerateRequest → invocation_id、files[].file_id |
| `/models/images/edit` | model_key + ImageEditRequest → invocation_id、files[].file_id |
| `/models/vision` | model_key + VisionRequest → invocation_id、content |

路径统一加 `/api/v1`。图片输入只接收 Files 的 file_id；上传通过原文件接口完成，按顺序发送。ModelMediaService 经 Files 公开 Service 检查输入对象与内容权限，通过同一 ModelRuntime 完成发送登记、共享节奏、渠道协议、回执与费用记录，生成结果按原 Invocation 的产物键保存 Files。直接调用没有 flow_context 或 Worker 归属；下载回执继续绑定当前用户、原渠道与调用，不能读取其他用户的结果证据。文本和提示词不新增持久化，素材与生成文件按 Files 生命周期保存。

四种操作均需 model:invoke；生成／编辑另需 file:create，使用参考图另需 file:content_read。图片理解读取已有授权文件不要求 file:create；控制台选择本地文件上传时需同时具备该权限。预览和下载独立检查文件内容读取权限。调用成功不授予其他用户文件访问权。

推理模型的输出 Token 上限包含推理用量，预算耗尽时可能没有可显示正文；控制台对空文本给出明确提示并保留调用编号，费用仍按原调用事实记录。

每次明确发送产生一次新调用，没有自动生成重试。页面「停止接收」、断线或离开不证明上游取消或免费；结果未知时先查原调用记录。生成已成功而下载／保存失败时保留原调用成功和费用事实，不补发生图。直接请求不承诺跨进程恢复；需要持久编排的业务继续使用 Flow。同步图片协议的 600 秒生成窗口及每份 120 秒下载窗口不变，控制台图片路由代理允许 900 秒等待；外层部署代理应保留相应时限。

## 7. 发送节奏与 Flow 关联

ModelDispatchScope 以真实站点/Token 的 HMAC 为主键，保存 next_send_at、blocked_until、updated_at，不保存 Token 或正文。相同凭据的重复 Channel、API 和 Worker 共用节奏，不同 Token 独立。model_send_interval_ms 默认 0（0～60000）；冷却默认 5 秒（1～300），最高默认 300（1～3600，且不小于默认）。公共配置摘要包含这些参数。

claim_in_transaction 使用数据库时钟锁定范围，仅到期时推进资格，并与 Flow 预算预留和 ModelInvocation 登记同事务提交；失败整体回滚，不持事务 sleep，不预领一串未来时间槽。取得资格后仍复查原身份/目标，原调用 ID 已存在则拒绝重发。HTTP 429 的 Retry-After 在 Adapter 规范化、有界后持久保存 shared cooldown，即使没有回执也生效；本次不自动重发。原任务 GET 同样受节奏约束，tokenless CDN 下载使用自身取回限制。

Flow 的批量绑定在调用方当前身份事务内解析，全部 Entry→全部 Channel 固定锁序、按 ID 去重。固定 provider、地址、上游模型及 operation/delivery；发送前再次核验，Key 新映射不改变原 Run。原用户/PAT 与每个外部操作继续实时授权。

InvocationContext 的 workflow_id（1～512 字符）/step_id（严格整数 0～2³¹−1）保存原模型 Step，历史为空不补写；异步查询比较原身份/角色/节点，观测位置不参与授权、不覆盖原提交位置。image_sources_in_transaction 每批最多 100 项，在当前模型记录范围和同 Run 内校验私有 Manifest 与 File 登记摘要、大小、MIME，只返回确认的 File/调用/产物键关联。

Run 关联调用按显式 Run 查询，不套默认 30 天窗口；SQL 权限在分页前生效。只投影受控身份、操作、状态、角色/Agent/Step 和错误分类，费用仍独立。Run 预算与诊断接口统一见 [Flow](flow.md#8-api授权与诊断)。

## 8. 多渠道模型接入设计

现有渠道采用代码适配、Service 管理调用、数据库保存配置和事实。协议代码保留在当前仓库的 adapters/ 内，宿主依赖与协议实现明确分开，随 Keystone 发布；当前不是独立分发的库，有其他实际使用方后再考虑独立发包。

### 8.1 职责边界

| 范围 | 当前实现 |
|---|---|
| 渠道声明 | CHANNELS 显式组合每种渠道的协议与账务实现；目录、角色绑定、发送和余额查询共用同一声明 |
| 协议实现 | 兼容文本、OpenRouter 图片、Yunwu/Openlux 同步图片、Openlux 原生图片任务与各渠道查费，使用轻量类型和协议错误 |
| 宿主接入 | InferenceClient / BillingClient 接入受控传输、发送登记钩子，并将协议错误转换为 ServiceError |
| 数据与运行 | 现有 Service 负责身份、事务、原任务/产物恢复及费用核对；数据库结构保持不变 |
| 扩展状态 | 没有新增渠道或自由配置站点；视频和独立发包均未实现 |

### 8.2 代码与数据库的边界

| 内容 | 归属 |
|---|---|
| 路径、鉴权、参数转换、响应解析、任务提交/查询 | 渠道适配代码 |
| 查费方式、请求编号匹配、金额/单位解析、余额查询 | 渠道适配代码 |
| 模型特殊处理、支持的操作、交付方式、参数校验和默认值 | 类型明确的代码 |
| Channel | 数据库：现有 provider、名称、地址、密钥密文、启停、账户说明及余额/核对字段 |
| ModelEntry | 数据库：现有 key、channel_id、provider_model、operations 和启停 |
| ModelInvocation | 数据库：原调用归属、状态、用量、回执、恢复证据、实际费用及核对状态 |

继续使用三个主要实体及现有协调记录。不新增主表或通用配置字段；数据库不保存请求模板、字段映射、协议组合、参数 Schema 或计费公式。恢复回执及账务快照是已发生调用的证据，不是可编辑的协议配置。

管理员只选择渠道类型并填写连接信息、模型映射；不分别选择调用协议和账务协议。provider 继续作为代码注册 key，不另加 adapter_key。已支持类型的渠道实例和模型配置通过数据库维护；新协议、新查费行为和模型特殊处理通过代码更新。确有额外连接参数时，只为具体渠道增加明确类型的字段，不预设通用配置 JSON。

基址、路径和下载目标由渠道实现校验，配置不能放宽网络边界或跨站携带密钥。渠道账户归属、Token/站点修改及历史凭据规则沿用第 2.1、3.6 节，不新增凭据版本机制；无可验证原范围时阻塞历史查询，不用当前密钥冒充原凭据。

### 8.3 渠道组合与模型能力

composition 显式装配渠道入口。一个入口在代码中组合所需的文本、图片、原生任务和账务实现；同类协议使用普通函数和对象复用，不动态扫描或在线加载插件。例如 OpenRouter 组合兼容文本、其图片实现和 OpenRouter 查费；Yunwu/Openlux 共用 GPT Image 同步协议，Openlux 还组合原生图片任务，各自保留原账务范围。渠道内部的组合不暴露为管理配置。

沿用现有明确的操作和请求 DTO，参数默认值、限制和特例放在对应实现中。有效能力取代码支持、ModelEntry.operations 和 Flow 角色要求的交集，未知参数/不支持组合在登记发送前拒绝。新增上游模型只有在既有实现已支持它时才只改映射；不能因模型名存在或兼容文本格式，就宣称支持图片、结构化、视频或查费。

不支持的操作明确拒绝；没有账务查询仍可使用已支持的调用能力，并记录可信响应费用或未知原因。现有模型目录按代码投影可用操作，无需新增能力 API 或通用能力注册平台。

### 8.4 Service 与 Adapter 接口

以下为当前内部接口边界；不新增 HTTP API，也不强迫所有渠道实现同一个大基类。

| 接口 | 输入 → 输出；职责 |
|---|---|
| ChannelAdapter.protocol / supports | 操作、交付方式和上游模型 → 已安装实现或支持与否；无数据库或网络 |
| 协议 validate_request | 实际报文、固定选择和目标 → 校验结果及 ModelRequestBudget；发送登记前执行 |
| 文本/图片调用与流 | 明确请求、短期连接参数、受控传输 → 类型明确的结果/事件、用量和回执 |
| 原生任务提交/查询 | 请求或原任务回执、受控传输 → 回执或待完成/结果；仅支持的渠道提供 |
| 账务 binding / receipt / lookup / page / balance | 原范围配置、响应白名单字段、请求 ID 或有界窗口 → 原账务绑定、Fee / Receipt / LogPage 或余额；按实际能力提供 |
| ModelEntryService / ModelRuntime | 现有选择、角色批量绑定和调用入口；负责身份、固定目标、预算及发送登记 |
| ModelRuntime.sdk_settings / invoke_sdk | Agent 使用的固定选择与设置 → 渠道代码补齐的参数及受控调用；Flow 不拼装渠道专用参数 |
| 调用/图片/费用 Service | 保存调用与回执，通过 Files 管理产物，编排原任务恢复和费用核对，提交事实 |

渠道声明与协议实现只依赖轻量请求/结果类型和传入的 SDK/传输契约，不导入 ORM、用户权限、Service、FlowContext、DBOS 或 ServiceError。协议失败使用 AdapterError，查询退避使用 QueryLater；Keystone 客户端在宿主边界转换或处理。复用现有 Fee、Receipt、Binding、LogPage 等结构，不序列化 SDK 对象；秘密仅在本次请求期间由宿主传入，不进回执或快照。

Keystone 宿主保留授权、事务、凭据读取、共享容量和取消收尾；向适配层注入受控传输及发送前登记/回执钩子；渠道代码提供 authorize，宿主仅在登记成功后用本次秘密执行鉴权，当前渠道均复用 Bearer 实现。所有生成请求都必须经过原子登记，SDK 隐藏重试关闭；查询、下载不能隐式再次生成。共享锁和协调实现仍属宿主，不随协议代码抽出。其他使用方可提供自己的宿主实现，不能因此推定已有 Keystone 的持久化或恢复保证。

### 8.5 固定选择、媒体任务与恢复

保留现有 Selection、ModelInvocation.status、usage、result_context 和 cost_check_status，不增加执行/交付/取消三套状态机。Flow 受理时固定渠道类型、Entry/Channel、上游模型、基址、操作和交付方式，调用回执及账务绑定按原格式持久化；恢复不重新解析当前 key 映射，不保存可执行对象或整套能力定义。新外部操作继续核验原身份和固定目标。

图片仍沿用现有提交、查询原任务与 Flow 显式等待，适配分派不改变轮询归属；费用补查不推进生成任务。恢复查询原 task_id，复用原调用 ID 和产物键；无可靠回执的已登记调用保持 unknown，不重发或换渠道。文件仍经 Files 公开 Service 校验和引用，调用可见不授予内容权。

后续视频增加 video.generate 的明确请求/结果类型和具体原生任务实现，复用调用记录、文件与费用字段；按真实协议验证时限、查询和取消能力后开放。不提前新增视频 API、Webhook、远端任务配额表或媒体任务引擎，也不把当前 HTTP 并发/图片时限当作视频保障。

### 8.6 实际费用与统计

渠道代码获取并解析上游费用事实，CostService 负责原范围校验、精确匹配、幂等和事务写回。继续使用 ModelInvocation 的 cost_amount、cost_unit、cost_source、cost_evidence 和 cost_check_status；来源为响应报告、按原请求查询或原范围日志核对，不新增费用表、价格表、估价或用户扣费。

响应和补查只补全同一条调用，不重复累加；回执归属冲突或同一查询证据的金额冲突标记 blocked。查询规范化替换原响应口径时保留 previous 证据，具体写回语义沿用第 3.6 节。金额用 Decimal 和原单位，仅允许依据渠道明确报告的扣费额度及单位因子规范化。公开单价乘用量、余额差和请求时长不证明实扣；明确报告的 0 才是 0。缺失、失败、取消保持未知及原因。原账务范围、锁、分页、重试和截止仍按第 3.6 节。

usage 按上游提供的已知计量保存；新增图片数量或视频计费秒数时用操作对应字段，不预建通用计量系统。当前沿用现有列表、补查、汇总 API 和权限范围，不在本次整理中增加统计接口或字段。现有汇总按单位分组，未知成本不计作零；后续补充模型/操作过滤和未确认数量时，再扩展对应 DTO，不建设通用查询语言。

### 8.7 版本与发布

适配层先随 Keystone 代码、锁文件和同一制品发布，不新增独立仓库、包发布流水线、在线插件或自动升级。数据库中已支持的渠道实例/模型映射变更与代码能力升级分开；代码更新按现有[发布规则](../deployment.md#3-更新与恢复)处理，不热替换在途执行使用的实现。

以后独立发包时，由宿主锁定依赖版本，渠道实现仍通过显式代码装配。包版本不能替代回执/快照格式版本或 execution_version；更新依赖必须检查旧回执、账务口径与 Flow 检查点兼容性，无法兼容的旧运行保留原制品和身份收尾，不让新版本重新解释或重发。

### 8.8 实现位置与验证边界

下表模块内路径相对 backend/src/keystone/platform/model_access/。

| 位置 | 当前职责 |
|---|---|
| adapters/channels.py | ChannelAdapter 与显式 CHANNELS 组合；声明固定基址、协议和账务实现 |
| adapters/inference/protocols.py、openai_images.py、images.py、kling.py | 请求校验、文本/流/识图、共用 GPT Image 报文与验证、原生任务协议；不导入宿主权限或执行器 |
| adapters/billing/（除 registry.py） | 费用/余额协议、原范围绑定与精确回执解析；协议错误独立于 ServiceError |
| adapters/inference/client.py、transport.py、errors.py；adapters/billing/registry.py | Keystone SDK 生命周期、受控发送、观察、错误转换和客户端分派 |
| adapters/http.py、credentials.py、coordination.py、errors.py | 宿主 HTTP/下载边界、密钥、共享协调及协议错误转换 |
| services/entries.py、channels.py、queries.py | 从同一渠道声明读取能力及账务类型；保留授权、SQL 过滤和模型角色绑定 |
| services/runtime.py、image_results.py、image_tasks.py、costs.py | 调用登记、预算、回执/产物持久化、原任务恢复及费用核对 |
| 模块外 keystone/composition.py | 将渠道声明与宿主依赖注入客户端，保留原生命周期 |

渠道适配不增加表结构、独立包或新配置字段；当前完整迁移基线及旧库切换规则见[迁移说明](../../backend/migrations/README.md)。provider、Selection、Flow 快照、图片回执/Manifest 和费用证据继续使用原格式，历史不改写、不推测回填。代码及其目录变化仍影响 execution_version，旧在途运行遵守第 8.7 节。

测试覆盖协议代码的依赖边界、多个渠道复用文本协议时的不同回执/费用口径、Agent 参数来自渠道代码、登记先于渠道鉴权和发送、能力与目录/发送一致、协议错误到宿主错误的转换，并保留真实 PostgreSQL/DBOS 的旧快照、原任务恢复、未知不重发、取消、共享容量及费用核对回归。自动验证使用本地协议桩，不触发付费调用；视频与独立分发待具体实现后再验证。

### 8.9 文本向量

`ModelRuntime.invoke_embedding` 与持久 Step 内的 `FlowContext.invoke_embedding(role, request, node_key=...)` 使用 `EmbeddingRequest(texts, dimensions)`；Runtime 返回向量，Flow 返回包含向量和原调用 ID 的 `EmbeddingResult`。每次 1～16 条非空文本，每条最多 8 KiB、合计最多 32 KiB，维度 1～4096；编码固定 float，所选模型须支持请求维度。只装配 OpenRouter／NewAPI 已有兼容协议，不按模型名推断能力，也不自动修改已有条目。

实际报文在登记前核对字段、模型和目标；结果检查模型、输入序号、数量、维度、有限数值和非零向量。响应继续使用现有 4 MiB 上限。沿用当前用户／PAT、固定角色绑定、稳定调用 ID、发送登记、预算、实际用量与费用；计入模型请求及实际输入 token，输入用量未知时阻止继续发送，不预留输出 token。模型调用账本不存文本／向量正文；Flow 输入、输出与检查点仍遵循原生持久化规则，不建立通用向量库或索引任务。

已发送但缺少持久结果时保留原调用事实，拒绝自动重发；原 Step 检查点或业务已保存的完整结果可按各自授权恢复。取消和撤权阻止后续操作，真实发送与费用受限收尾。较大的向量不能直接塞进 64 KiB Flow 输出，业务按需保存受控文件或自己的派生索引，并负责对象权限、索引更新及过期结果拒绝。