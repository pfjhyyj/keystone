# 费用预估

估价已接入现有 ModelEntry/ModelInvocation，使用真实计量乘登记时价格，不代表供应商实扣。主流程与状态见[模型设计](../domains/model-access.md#9-单次费用预估计算器设计契约)。

## 价格来源与适用范围

ModelEntry.pricing 为 inherit/override/disabled，pricing_revision 单调递增。覆盖规则按 operation/delivery 精确绑定，默认继承内置目录；未命中即缺价格，disabled 与有效零价分别表达。

规则按 provider、规范化 api_base、精确上游模型、operation/delivery 解析，不按名称前缀或角色猜测。中转站和 ep-* 不继承直连厂商价格。改绑目标/操作重新校验覆盖，改名不改价格。

默认规则随制品发布，包含来源 URL、核对时间和版本，不在调用期间抓价。参考目录由 adapters/pricing/catalog.py 列举：百炼北京 Qwen Max/Plus/Flash 及六个向量模型；方舟北京 Seed Evolving、2.1/2.0 系列的核实版本。完整 ID 和价格以代码数据为准。

百炼输入档位按 K=1000，方舟按接口整数边界（32K=32768、128K=131072）；完整输入选档后对全部数量用该档价格。qwen-plus 按实际 thinking 设置，百炼隐式缓存使用官方折扣、实时原价，不套活动或显式缓存；方舟默认仅标准服务，fast/flex/auto 需覆盖价。未核实的缓存单价不猜测。

价格可记录采购单价/折扣及来源，不自动扣免费额度、资源包或代金券。查询目录、余额/月账单、轮询和下载均不增加计价次数；币种不自动换汇，预估不参与 Flow 资源预算。

依据：[百炼价格](https://help.aliyun.com/zh/model-studio/model-pricing)、[隐式缓存](https://help.aliyun.com/zh/model-studio/context-cache)、[方舟价格](https://docs.volcengine.com/docs/ark/model-pricing?lang=zh)；条件树及快照参考 [NewAPI 计价表达式](https://github.com/QuantumNous/new-api/blob/main/pkg/billingexpr/expr.md)，不采用其钱包或用户组售价。

## 计量与分区

计算器仅接受 PricingSnapshot 和 PricingFacts，不读取 ORM、SDK、环境、网络或当前时钟。

| 输入 | 内容 |
|---|---|
| binding | 固定渠道、模型、操作、交付和计量规范版本 |
| context | 已校验报文的 size/quality/resolution/格式、参考图数及有来源的时间事实 |
| usage/events | 上游报告或受控终态的数量、单位、来源和完整性 |

prompt、正文、URL、密钥不进入估价事实。max_tokens、请求 n、预期时长不是实际计费量；缺失与显式 0 区分。协议用 normalize_pricing_facts 普通函数声明白名单和单位，管理员不配置 JSONPath。

| 协议 | 已支持计量 |
|---|---|
| Chat：文本、结构化、识图、Agent、SSE | 真实输入/输出 token、缓存、完整上下文档位、已确认成功请求数；不从图片数推 token |
| Embeddings | prompt_tokens，或规范明确的全输入 total_tokens；16 文本仍为 1 请求，输出 token 为结构性零 |
| OpenRouterImages | 已报告 token、受控生成张数、成功请求数和有效参数 |
| Yunwu/Openlux OpenAIImages | GPT Image 输入/输出及文本/图片细分、生成张数、实际 size/quality |
| KlingImages | 原任务成功生成张数及请求数，不提供计费秒或 credits；轮询不计次 |
| 未装配视频/音频等 | 不允许通过配置启用计量 |

文本/向量/同步图片使用 v2，Kling 使用 kling_images.v1；每种协议只安装当前计量规范。v2 校验 input/prompt、output/completion 及明细别名一致；DeepSeek hit/miss 仅在总量关系成立时推导。预算 usage() 与估价归一化分别保存，不改变预算口径。

规则为输入、输出各选一个互斥分区：

| 分区 | 数量与约束 |
|---|---|
| aggregate | 总输入/输出，不另收缓存、图片或 reasoning 子集 |
| cache | 普通输入＋独立缓存读/写，普通输入扣除已单独计价部分 |
| cache_read | 未命中（含写入）＋命中输入，不要求捏造缓存写入 0 |
| modality | 协议明确提供的文本/图片互斥数量 |
| modality_cache | 需有模态/缓存交集；当前无独立计量，未开放 |
| reasoning | 普通输出＋reasoning，避免输出总量重复计费 |
| none | 明确不按该侧 token 计价 |

input_total_tokens 用于完整上下文档位，包含缓存。所选分区的所有可能非零项必须配置，免费项也显式 rate=0；编译拒绝总量与子集重复收费、单位错误、未声明字段。缺细分可用 aggregate，不能按比例拆分交集。

当前 chat.v2 为直连文本规范；relay_chat.v2/OpenRouter 图片规范 cache 分区需显式普通输入、读取、写入，上游未回写入不能补 0。GPT Image 缺文本/图片分项时记 partial。

upstream_success_count 只在原请求/任务成功确认时为 1，不随 SSE、轮询或多图增加。generated_images 为实际受控结果数量，不等于请求 n 或成功下载数。秒数/credits 须有明确上游字段，不能从耗时或费用反算。

## 规则格式与计算

PriceRule 是版本化条件树，表单、阶梯和高级 JSON 使用同一格式，不执行脚本。

| 节点/字段 | 契约 |
|---|---|
| 根 | format=1、currency、root；支持的 ISO 币种 |
| branch | when/then/else 齐全；白名单字段 eq/in/lt/lte/gt/gte，有界 all/any；缺条件不当 false |
| price | 稳定 id、input_partition/output_partition、1～16 items、显式 factor |
| item | 稳定 id、quantity、rate、per；金额=quantity×rate÷per |

同次计算仅命中一个叶子，总额为项目和乘 factor。阶梯嵌套且穷尽，先按完整输入选档再对全部数量计价，不隐式超额累进。条件已知时 rate=0 不依赖未知数量，factor=0 可得确定零，仍校验字段/单位合法性。

Decimal 内部至少 80 位，项目不提前舍入，总额 ROUND_HALF_EVEN 到 18 位。金额/rate 有限且小于 1e20，rate 最多 18 小数位；per 为 1～1e12 正整数，factor 为 0～1e6。溢出 invalid，不钳上限或零。

规则最多 16 KiB、64 节点、深度 8、每叶 16 项；context/计量各最多 32 字段，token 为小于 2^63 的非负整数。拒绝重复 ID、未知字段/版本、非法数值和不穷尽分支。

以下为计算示例，不是真实官方报价：普通输入 2、缓存读取 0.2、输出 8 CNY/百万 token。

```json
{
  "format": 1,
  "currency": "CNY",
  "root": {
    "kind": "price",
    "id": "standard",
    "input_partition": "cache",
    "output_partition": "aggregate",
    "items": [
      {"id": "input", "quantity": "usage.input_regular_tokens", "rate": "2", "per": 1000000},
      {"id": "cache", "quantity": "usage.input_cache_read_tokens", "rate": "0.2", "per": 1000000},
      {"id": "output", "quantity": "usage.output_total_tokens", "rate": "8", "per": 1000000}
    ],
    "factor": "1"
  }
}
```

仅当协议没有其他缓存写入项时该 cache 分区才完整。输入 1000（缓存 100）、输出 200，普通输入 900，总额 0.00342 CNY；reasoning 已含输出时不另加。

阶梯可用 branch 判断 input_total_tokens≤32000，32000 进 A，32001 进 B，A/B 为完整 price 叶。图片使用 generated_images，每次价使用 upstream_success_count，size/quality 作为条件。

time_basis 声明时区、all_days/weekdays（日历不含法定节假日调整）及 observed_response/upstream_completed 来源，固定 pricing_date/hour/weekday。日历排除为 pricing_calendar_excluded，不记免费；缺供应商完成时间不能用登记/查询时间替代。本地完整响应时间须标 observed，响应头 Date 不冒充上游完成时间。

## 快照、持久化与恢复

发送登记事务锁 Entry/Channel、复查身份/固定目标、取得发送许可后，保存完整 PricingSnapshot。Flow 固定模型选择，每个尚未发生的新调用使用各自登记时价格，同一调用恢复使用原快照。

快照含规则全文、规范哈希、来源/版本、revision、计量版本、固定目标和请求条件，最多 24 KiB；未配置/关闭也保存明确结果，结束时不按新价格补算。进程缓存可有界按版本/哈希复用，历史恢复不能依赖缓存。

| ModelInvocation 字段 | 契约 |
|---|---|
| estimate_snapshot | 登记时只写一次 |
| estimate_facts | 白名单计量/来源/完整性，最多 8 KiB，可由原调用可靠回执补齐 |
| estimate_status | pending、estimated、partial、unavailable、invalid |
| estimate_amount/currency | 仅 estimated 保存非负总额，其他总额 null，合法零价可为 0 |
| estimate_reason | 白名单，如 price_missing、pricing_disabled、snapshot_missing、metric_missing、condition_missing、meter_conflict、price_overflow |
| estimate_detail | 命中叶、项目数量/单价/金额、factor、缺项、规则/事实哈希，最多 16 KiB |
| estimate_checked_at | 最近计算或记录缺失结果时间 |

partial 的已知项仅放 detail，不作为总额。估价与 cost_* 互不覆盖；同快照/事实重复计算一致，轮询/下载不能累加计量。缺少快照时标记 snapshot_missing，不补用当前价格；未知格式/计量版本不可计算。

同步/SSE 保存最终可信计量，不累计重复 usage chunk。流中断仅完整计量可成总额，部分计量为 partial；failed/unknown 不证明零费。可信未发送且有效币种快照才可确定零。

图片/异步在确认生成时保存数量与事件，不等下载；下载失败、取消、部分文件取回不抹去生成量。同 task_id 重复终态不计次，未知数量不填请求 n。估价失败不回滚真实调用/用量/产物、不覆盖推理错误或重发；数据库失败沿原未知结果保护。

新条目默认 inherit/revision=0；调用登记时冻结完整价格快照，恢复沿用同一快照与计量事实。

## 接口与展示

三项均要求 model:manage：

| API（/api/v1/model-entries/{id}/pricing） | 行为 |
|---|---|
| GET | 当前配置/revision、默认规则和可用计量 |
| PUT | expected_revision 原子更新，冲突 409；配置与成功审计同事务 |
| POST /preview | 完整草稿＋人工事实试算，simulation=true/manual；不发送模型、不保存配置或调用 |

合法规则但本次漏计量可 partial；非法或未装配字段拒绝保存。审计仅变更、规则哈希、revision、绑定摘要，不存示例正文。

编辑页支持继承/自定义/关闭、单价/多档阶梯（最多 6 档，按完整输入上限逐档递增）/闲忙时/按次/按张及高级 JSON，同一计算器预览；另显示只读的 NewAPI 风格表达式，不回写、不执行。闲忙时以收到完整响应的本地整点划分 [开始, 结束)，结束整点不算闲时，跨零点则结束于次日，其余为忙时，并可叠加同一套阶梯；试算示例写入 context.pricing_hour。厂商若按请求开始时间计费，结果可能不同。同一交付方式下计量 profile 完全相同的操作（如文本生成、结构化文本、图片理解）合并为一组，价格只填一次，保存时仍为每个操作各写一条相同的规则；已保存规则或参考价在组内不一致时分开显示并提示，可手动“分别设置”或“合并设置”。显示币种、来源、核对时间、适用操作和缺项，不用 0 占位。

新界面主要显示预估，实扣独立区域。统计 cost_basis=estimated|actual，旧客户端默认 actual，新界面显式 estimated；一次聚合只用一种口径，币种分组。预估状态与实扣 cost_status/source 不混传，未知覆盖不从分母消失，有效 0 计已估价。公开仅白名单分项/来源，不暴露内部地址、秘密或结果证据。

云月账单不进入调用/Flow 汇总；启用估价不自动移除余额、停用实扣补查或要求云账务凭据。

## 代码与验证

schemas/pricing.py 放规则/快照/事实/结果 DTO；services/pricing_calculator.py 的 validate_rule、estimate 为纯函数；adapters/pricing/catalog.py 放默认数据，协议负责归一化。pricing/entries 管价格授权与 revision，runtime/image_tasks/image_results/recovery 保存原调用事实，statistics 管口径与覆盖。

单元验证分区不重计、32000/32001 档位、零/缺失/冲突、精度/溢出、树边界和时间来源；真实 PostgreSQL/DBOS 验证登记快照、调价竞争、迁移、权限、恢复及取消不重发。HTTP 用本地协议桩，不发送付费请求，不以手造计量声称协议已支持。
