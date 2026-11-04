# 模型接入实现参考

本页维护当前供应商协议、接口和恢复限制；职责、主流程与不变量见[模型设计](../domains/model-access.md)，价格格式见[费用预估](model-pricing.md)。路径相对 backend/src/keystone/platform/model_access/。

## 渠道与模型列表

新建可选 OpenRouter、NewAPI、Yunwu、阿里云百炼和火山方舟；已有 openlux 内部标识保留，界面归 Yunwu 类。DeepSeek 仅保留声明。模型能力由代码协议和条目 operations 决定，不由模型列表推断。

| 渠道 | 地址/能力限制 |
|---|---|
| OpenRouter | 固定 https://openrouter.ai/api/v1；文本、向量、结构化、识图、同步图片 |
| NewAPI | 默认 https://newapiai.com/v1，可配置 HTTPS 站点及路径前缀；通用兼容文本/向量，媒体按验证站点 |
| Yunwu / Openlux | https://yunwu.ai/v1、https://api.openlux.ai/v1；同步 GPT Image，Openlux 另有原生 Kling 图片 |
| 百炼 | 固定 https://dashscope.aliyuncs.com/compatible-mode/v1；北京按量付费，统一 enable_thinking=false，仅思考模型不适用 |
| 方舟 | 固定 https://ark.cn-beijing.volces.com/api/v3；北京按量付费，接受 Model ID 或 ep-* 接入点；尚无向量/图片生成/视频 |

百炼支持标准 /embeddings，模型须支持指定维度及 float 输出。两家云渠道保存真实用量和请求 ID，单次实扣 unavailable，不从公开价格或月账单摊分。

GET /api/v1/model-channels/{id}/models 使用推理 Key，在事务外有界查询，前后复查 model:manage 和配置。仅返回去重排序 ID，不创建 ModelEntry 或调用记录；失败仍可手填，不自动替换缺失模型。不跟随跳转或隐藏重试，目录不保证当前 Key 全部可调用。

- OpenRouter 固定地址，兼容渠道在完整 API 前缀追加 /models。
- 方舟请求固定 /api/v3/models，最多 4 MiB/10000 条，不保证列出全部自建接入点。
- 百炼请求 https://dashscope.aliyuncs.com/api/v1/models；可选 workspace_id 使用代码生成的北京业务空间域名 https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/models。workspace_id 仅单个 DNS 标签，不改推理地址；编辑省略保留、null 清除。
- 百炼按 page_no/page_size=100，最多 100 页/10000 条，每页 256 KiB、总计 20 秒；校验 success、分页、总数及 output.models[].model，任何缺页或变化不返回部分列表。

依据：[DashScope 地址](https://help.aliyun.com/en/model-studio/base-url)、[结构化](https://help.aliyun.com/en/model-studio/qwen-structured-output)、[向量](https://help.aliyun.com/en/model-studio/embedding-interfaces-compatible-with-openai)、[模型列表](https://help.aliyun.com/zh/model-studio/list-models)、[方舟 Chat](https://www.volcengine.com/docs/82379/1494384)、[模型目录](https://www.volcengine.com/docs/82379/1330310)、[鉴权](https://docs.volcengine.com/docs/ark/base-url-and-authentication)。

## 供应商实扣与余额

| 渠道 | 单次费用证据 | 余额 |
|---|---|---|
| OpenRouter | usage.cost，或原 generation ID 查询 total_cost；单位 credits | 同账户 Management Key 查询 /credits，total_credits−total_usage |
| NewAPI | Bearer GET /api/log/token，网关请求编号精确匹配，quota/quota_per_unit，系统 USD | 普通 Key GET /api/usage/token/，user_quota 为账户，token_items 为 Key |
| Yunwu/Openlux | 原 Token 的 Key 参数分页日志；other.request_id 匹配 x-api-request-id | 未接入，不把 Token 用量当账户余额 |
| 百炼/方舟 | 尚无按推理请求 ID 确认单次金额的已接协议 | 独立云 AK/SK，见下一节 |
| DeepSeek | 对话用量，未提供单次金额 | 解析声明保留，首版不开放刷新 |

OpenRouter 不用 upstream_inference_cost 替代账户扣费，BYOK 还可能有外部费用。余额是账户观察，不是单 Key 独享额度。

NewAPI 优先 x-oneapi-request-id，其次 x-api-request-id，匹配日志顶层 request_id，其次 other.request_id；响应体模型 ID 不是网关账单 ID。最近列表最多 1000 条，达到上限记录可见时间边界；超范围未匹配记 cost_log_window_unavailable，不循环读同页或记零。部分站点返回分页对象，quota_per_unit 随本轮 checkpoint 固定，不用界面兑换率二次换算。

NewAPI 的 user_quota 受上游 allow_user_quota_query 控制，不主动修改权限。缺账户额度时 scope=token_quota；unlimited 仅表示 Key 不限额，不表示账户无限，不填零。零/负账户余额如实展示，管理身份 /api/user/models 不与普通 Key /v1/models 混用。

DeepSeek 分币种总余额已含赠送/充值，不重复相加。Openlux 的 System Access Token 和 New-Api-User 属于管理鉴权，不能用推理 Key 冒充账户余额查询。

依据：[OpenRouter 用量](https://openrouter.ai/docs/cookbook/administration/usage-accounting)、[余额](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits)、[Generation](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation)、[NewAPI 日志](https://docs.newapi.pro/en/docs/guide/feature-guide/user/log)、[额度换算](https://github.com/QuantumNous/new-api/blob/main/web/src/lib/currency.ts)、[Token 额度](https://github.com/QuantumNous/new-api/blob/main/controller/token.go)、[DeepSeek 余额](https://api-docs.deepseek.com/api/get-user-balance/)、[对话](https://api-docs.deepseek.com/api/create-chat-completion/)、[Openlux 鉴权](https://doc.openlux.ai/)。

## 云账户账务

百炼/方舟可选 billing_credentials={access_key_id, access_key_secret, security_token?}，使用同云账户的最小只读子账号权限，与推理 Key 分开。ID/Secret 成对替换，STS 过期更新完整凭据；留空保留，clear_billing_api_key=true 清除，不与旧 billing_api_key 同传。结构化凭据加密存原账务列，不回显，不新增环境变量。

| 云渠道 | 固定站点/签名 | 接口 |
|---|---|---|
| 百炼 | business.aliyuncs.com，RPC HMAC-SHA1，2017-12-14 | QueryAccountBalance；QueryBillOverview |
| 方舟 | billing.volcengineapi.com，HMAC-SHA256，cn-beijing，2022-01-01 | QueryBalanceAcct；ListBillDetail 产品/月聚合 |

百炼最小权限 bss:DescribeAcccount（官方拼写）及 bss:DescribeBillList；方舟仅授权两项账务查询。不使用根账号密钥，不授充值/购买权限，当前仅中国站。

余额分项按上游币种和含义保存：百炼可用额度/现金可能覆盖其他产品，方舟的现金、欠费、信控、冻结也可能包含，不相加。

GET /api/v1/model-channels/{id}/bills?period=YYYY-MM&offset=0 要求 model:manage，手动查询，每次最多 50 条；仅上海时区当前月及之前 24 个月，响应最多 4 MiB，百炼总览最多 10000 条。事务外查询、返回前复查权限和账务配置，变化丢弃旧结果。

月账单保留供应商月份、币种、原精度十进制及允许的负调账，分列应付、现金、抵扣、已付/未付，不把查询 RequestId 当推理 ID。云账户其他产品、出账延迟及后续调整都可能影响金额，不摊分到单次调用或混入调用统计。

依据：[百炼余额/授权](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-queryaccountbalance)、[月账单](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-querybilloverview)、[RPC 签名](https://help.aliyun.com/zh/document_detail/268240.html)、[方舟余额](https://docs.volcengine.com/docs/billing-center/query-balance-acct-query-user-account-balance-information?lang=zh)、[账单](https://docs.volcengine.com/docs/billing-center/list-bill-detail-page-query-bill-details?lang=zh)、[签名](https://docs.volcengine.com/docs/api-signature-call-guide/signature-method?lang=zh)。

## 费用核对

### 回执与范围

费用只接受可信响应或精确原请求查询，时间/模型/token 相似均不能代替 ID。回执由适配器尽早保存，硬中断前未保存编号时不保证找回。相同真实 Token 的重复 Channel 共用范围；不能因同账户而用另一 Key 查询历史。

Yunwu/Openlux 在 2026-10-21/22 的只读探针确认 Key 参数、分页和金额口径：

```http
GET https://api.openlux.ai/api/log/token?key=<原调用API_KEY>&start_timestamp=<开始秒>&end_timestamp=<结束秒>&p=1&page_size=20
```

解析 data.items 和分页，other 可为 JSON 字符串。仅 Bearer 的探针未成功；request_id 参数不保证服务端过滤，日志 id 不稳定，均不能作幂等键或水位。x-oneapi-request-id 与本协议 x-api-request-id 不同。

每轮先校验 /api/status：quota_per_unit=500000，Openlux 为 USD；旧 Yunwu 为 CUSTOM 且 exchange_rate=1，保存 yunwu_credits。仅 type=2、非负整数 quota，校验模型名；样本 1328/500000=0.002656 USD。口径变化 blocked/cost_policy_changed，不套当前比例重解释旧调用。其他兼容站点按显式声明保存本站额度，不推断美元。

凭据执行时解密，任务/检查点只传 ID、范围和有界页位置。Key 查询串、完整 URL、原始 HTTP 错误和日志账号/IP/content 不进日志或 DBOS，只保存规范证据。仅访问代码允许的 HTTPS origin/路径/方法，不跳转。

### 扫描与分页

调用结束事务保存回执和 pending 待办；仅扫描已结束、pending 且 next_check_at 到期记录，不改运行中调用。succeeded/failed/unknown 都可能有费用。

keystone.model_access.reconcile_costs 每 5 秒、启动立即扫描。keystone-model-maintenance 串行扫描，每轮最多 20 范围，查询使用 keystone-system。API 手动和 Worker 默认共享查询容量 2，同范围串行，不占推理容量。

日志按原 Token 及相近窗口合并，每批最多 20 调用，每页 20 日志，每次 2 页/25 秒，HTTP 最长 10 秒。固定窗口从本批最早登记前 120 秒至轮次开始；未扫完保存下一页，约 5 秒后继续，不消耗“完整未找到”轮次。完整覆盖才确认匹配，崩溃重读页不重复累加。

真实 scope 由站点/Token HMAC 产生，最早 Channel 保存共享 checkpoint、阻塞与冷却。费用与页进度同事务推进，revision 防旧结果覆盖，连接失锁不得换连接提交。分页和后续轮次允许重叠，不能以日志 ID/最高时间永久跳过旧窗口；新待办不改写当前窗口，扫描须避免饥饿。

Yunwu 页最多 256 KiB，NewAPI 最近日志 4 MiB，公开口径 1 MiB；拒绝深层/异常 JSON，只留有界 ID、模型、额度和单位证据。同版本活动任务合并，下一轮新执行 ID，扫描器不等待自己的子任务。

### 状态与重试

| 结果 | cost_check_status/处理 |
|---|---|
| 唯一可信费用 | confirmed，清 next_check_at |
| 完整扫描未匹配 | pending，有界退避，不记零 |
| 连接、超时、5xx | 暂时失败，最多 12 次错误重试 |
| 429 | 优先合法 Retry-After，整个范围共享冷却 |
| 凭据/范围/结构/金额/匹配冲突 | blocked，修复并确认原范围后显式重触发 |
| 无编号或无能力 | unavailable |
| 时间或错误预算耗尽 | exhausted，保留未知与证据 |

自动截止为调用结束后 24 小时，完整未找到轮次约在 10 秒、30 秒、2 分钟、10 分钟、1 小时、6 小时、24 小时到期；停机不重置。传输错误按 30 秒倍增至 1 小时、±20% 抖动，最多 12 次（不含首次），成功不重置累计错误预算。Retry-After 支持秒/日期但不越截止，分页续跑不延长窗口；不为每调用建立长期 sleep。

手动补查同时要求 model:manage 和该记录读取权，共用适配、容量、锁及幂等；同步预算不足保存待办后返回。blocked/exhausted 只能显式重触发，普通页面刷新不重置预算。

费用、证据及进度在持锁连接短事务提交，外部 HTTP 不持业务事务。cost_check_* 与调用 status、error_code 独立。金额 Numeric(38,18)/Decimal，重复只补原行；矛盾阻塞，可信查询替换响应口径时保留 previous 证据，不静默累加。confirmed 后不自动推断退款或长期账单修订。

内部 cost_context、receipt_ids、cost_evidence 不公开；显式重查审计 model.cost.recheck。查费只使用调用登记时的范围与回执；缺少范围时不可查询，不按当前渠道补绑。

## 发送与协调

每次物理 POST 前复查原身份/PAT、model:invoke、固定模型/渠道启用和当前秘密；唯一登记与 Flow 预算、发送资格同事务提交，之后才鉴权发送。SDK/transport retries=0，不自动跳转，trust_env=False。公开请求不能提供内部调用 ID、身份、固定目标或 Flow 上下文。

NewAPI 自定义 api_base 为完整 HTTPS 前缀，拒绝内嵌凭据、查询、片段、路径穿越。使用系统 DNS，不做公网 IP 预判或固定解析，支持内网/Fake-IP；OpenRouter 仅固定 origin 及已实现路径。

明确拒绝的 4xx 为 failed（402→model_insufficient_credits）；超时、5xx、断流、解析异常和 HTTP 200 error/finish_reason=error 为 unknown。SSE 须有完成原因及结束标记，缺 usage/cost 可成功但保持未知。明确 400/413/422 的 context_length_exceeded 映射受控错误，连接异常保留 unknown。

| 边界 | 当前值 |
|---|---|
| 文本消息/正文 | 最多 32 条，总计 64 KiB |
| max_tokens | 默认 1024，最高 4096 |
| 本地调用/查询并发 | 4；实际推理另受跨进程共享容量 |
| 文本窗口/SDK I/O | 60 秒/30 秒 |
| 文本请求/响应/SSE 单事件 | 256 KiB/4 MiB/64 KiB |
| 发送后取消事实收尾 | 最多 5 秒 |
| 余额刷新 | HTTP 10 秒，本进程 2，同渠道会话锁串行 |

ModelDispatchScope 保存 next_send_at、blocked_until、updated_at，站点/Token HMAC 为键。model_send_interval_ms 默认 0、范围 0～60000；冷却默认 5 秒（1～300），上限默认 300（1～3600，且≥默认）。不预领未来槽，不持事务 sleep；异步任务 GET 同样受节奏，CDN 使用取回限制。

余额外部请求不持事务，写回复查原配置；持锁连接失效不换连接提交。响应保存失败可能遗留 running，返回 model_result_unavailable，不重发。

诊断日志只保留调用/请求 ID、固定目标、阶段、耗时、字节、状态/回执及异常类型/代码位置，不记录原文异常、URL、局部变量、正文或认证头。排查见[部署](../operations/deployment.md#21-调用失败排查)。

## 媒体与恢复

### 输入与向量

结构化使用严格 response_format.json_schema，本地验证，Schema 有界且仅本地引用，验证失败不修正重发。OpenRouter 要求 provider.require_parameters。图片输入校验实际解码、MIME、单帧 PNG/JPEG/WebP，单份最多 4 MiB/16M 像素。

| 场景 | 输入上限 |
|---|---|
| 图片理解、OpenRouter 编辑 | 4 份、合计 8 MiB |
| Yunwu/Openlux 编辑 | 9 份、合计 32 MiB |
| 普通媒体报文 | 12 MiB |
| Yunwu/Openlux multipart | 32 MiB＋128 KiB |

EmbeddingRequest(texts, dimensions) 每次 1～16 非空文本，每条 8 KiB、合计 32 KiB，维度 1～4096，float 输出。OpenRouter/NewAPI/DashScope 装配 /embeddings，验证模型、序号、数量、维度、有限非零向量。请求/结果不存调用账本，Flow 大结果须另存文件或业务索引；输入 token 缺失阻止后续相关发送，不预留输出 token，不增加向量库。

### 产物证据

ImageManifest format=1，保存 request_digest、operation、outputs；每项仅稳定 artifact_key、SHA-256、size_bytes、content_type，键由 invocation_id＋序号派生。最多两份、每份 8 MiB、总计 16 MiB，result_context 私有，不与 cost_context 混用。

恢复先核原身份、角色、操作、固定目标和摘要；已保存 Manifest 则验证原文件，缺不可取回内容时失败；无调用才可登记发送。模型层先确认生成事实与 Manifest，并返回受控图片内容；Flow 再逐份保存 File/Run 引用，返回自身的轻量 ImageResult。只有哈希不能取回丢失 Base64；文件已保存则按原稳定键复用。

### 异步图片

Openlux 原生 POST /kling/v1/images/generations、GET /kling/v1/images/generations/{id}；固定映射 origin，不从客户端收地址。仅 kling-v1、kling-v1-5、kling-v2、kling-v2-1，状态 submitted/processing/succeed/failed，结果 task_result.images。

submit_image 返回 invocation_id，image_result 返回 ImagePending 或 ImageResult；Workflow 显式 Step/DBOS sleep，不由 Context 暗建 Workflow。TaskReceipt 保存 protocol、task_id、摘要、数量、原截止、次数和最早查询时间；恢复只查原任务，不新建调用。原提交起 300 秒、最多 60 次，重启不重置。

Manifest 可保留 TaskReceipt，以便生成成功后重新取原产物；摘要不符拒绝覆盖。CDN 只允许适配器声明的 HTTPS/443，无凭据、无跳转，不携渠道 Authorization，URL 不存日志或公共回执。撤权/取消阻止新查询、下载与发布，本地中断不重建或承诺取消远端任务。

### Yunwu/Openlux 同步图片

两个站点共用 OpenAIImages，支持 gpt-image-2、gpt-image-2-c、gpt-image-2.5-sunburst、gpt-image-2.5-flare。生成 POST /images/generations JSON，编辑 /images/edits multipart，重复 image 字段；模型声明仍须包含操作。

可选 aspect_ratio 为 1:1、16:9、9:16、4:3、3:4，quality 为 low/medium/high，默认 1:1/medium；resolution 为 1K/2K/4K，转换成符合比例、16 倍数及像素/边长限制的 size，输出 png/jpeg/webp。OpenRouter 不接受这组新增控制；请求摘要使用完整的当前 DTO。

仅该协议发送协调及 SDK 总窗口为 600 秒，建议图片 Flow timeout_seconds 至少 900 秒覆盖下载/保存，旧快照不自动延长。

b64_json（含匹配 MIME 的 data URI）验证后形成 Manifest。URL 回应先加密存 ImageDownloadReceipt，绑定渠道/调用/操作/摘要；最多两份、每 URL 3072 字符，证据/Manifest 最多 16 KiB。本地取回凭据最长 1 小时，上游可更早失效。

每份下载最多 3 次、2 次跳转、总窗口 120 秒、每请求 30 秒、最多 8 MiB。每次及跳转复查原身份、固定目标和来源，仅 HTTPS/443，系统 DNS，不携渠道 Token；只重试 GET。URL 有效回应落库即确认调用成功，文件或 Run 仍可能失败；重启复用原凭据、Manifest 或 File，不重新生成或复活终态 Run。

费用仍以原 Token 的 x-api-request-id 精确匹配日志，响应体 id、usage.cost、余额差不作本渠道实扣。缺回执 unavailable，缺日志 pending，口径/模型冲突 blocked，生成失败也可保存查实费用。

依据：[OpenRouter 结构化](https://openrouter.ai/docs/guides/features/structured-outputs)、[图片理解](https://openrouter.ai/docs/guides/overview/multimodal/image-understanding)、[图片生成](https://openrouter.ai/docs/guides/overview/multimodal/image-generation)、[云雾结构化](https://lrbebhrbp1.apifox.cn/api-417728203)、[识图](https://lrbebhrbp1.apifox.cn/api-417728221)、[Openlux Kling](https://doc.openlux.ai/api/docs/public/specs/platform/kling)、[NewAPI 图片说明](https://github.com/QuantumNous/new-api-docs-v1/blob/main/content/docs/en/plugins/usage.mdx)。协议桩不代表任意兼容站点均已线上验证。

## 接口与控制台

| API（/api/v1） | 权限/行为 |
|---|---|
| GET /model-access/options | manage，已安装类型 |
| GET/POST /model-channels；PATCH /model-channels/{id} | manage，渠道配置 |
| GET/POST /model-entries；PATCH /model-entries/{id} | manage，模型配置 |
| PATCH /model-channels/{id}/name；/model-entries/{id}/name | manage，仅改名称 |
| DELETE /model-channels/{id}；/model-entries/{id} | manage，未引用 204，否则 409 |
| POST /model-channels/{id}/balance | manage，手动余额刷新 |
| GET /model-invocations、/{id}、/summary | read，本人；read_all 扩大范围 |
| GET /model-invocations/statistics、/options | 同记录权限，统计与当前可见主体搜索 |
| POST /model-invocations/{id}/cost | manage＋记录读取，补查已结束调用 |

配置 PATCH 提交完整非秘密编辑字段；省略/null 密钥保留，clear_billing_api_key 清账务 Key。provider/key/已确定账户归属不可编辑，原空账户可补录。SecretStr 输入、Fernet 绑定渠道/用途；首次保存原子生成协调卷 .model-credentials.key，Linux/macOS 0600，须联合备份。

列表默认 50、最高 100。调用查询默认最近 30 天、最多 366 天，范围在 SQL 分页/聚合前应用，汇总同一快照。数值为十进制字符串，cost Numeric(38,18)。Run 诊断显式按 Run 查询，不套默认窗口。

/console/models 提供渠道、模型、价格配置与余额查询；/console/model-usage 提供独立记录与费用页，不依赖配置或用户目录权限。业务通过 runtime.model_service(identity) 调用 ModelRuntime，自行授权输入并按需使用 Files 保存结果。模型模块不保存 File，不提供通用推理 HTTP 入口。

统计支持时间、渠道/模型/用户、来源、Run/角色/节点/Agent/操作、调用和费用状态/来源/单位/错误。上海时区分桶，小时桶最多 31 天；筛选提交后固定，翻页沿用，相对范围手动刷新推进，自定义范围不变。URL 保留筛选、分页和详情，主体选项只来自可见调用，权限变化清空旧数据。

已知费用含失败调用真实消耗，按原单位分列。覆盖率=有金额调用/全部调用，单位均价仅以该单位已知调用为分母。输入/输出 token 各计已报告整数并展示覆盖；平均/P95 为登记至完成，排除不完整/异常时间，不称供应商纯延迟。趋势/分组钻取保留其他条件；无自动统计轮询。

## 离线收尾

停止同库全部 API/自定义普通调用进程并保持停止后执行：

```shell
keystone recover-model-calls --confirm-api-stopped
```

命令不探测或停止进程，需操作者确认且迁移匹配。maintenance.py 每批最多 100 条，仅普通无 flow_run_id 的 running 改 unknown/model_interrupted，保存 finished_at/updated_at；保留原回执、用量、费用与归属。每条 model.invocation.recover 系统审计和批次变更同事务，失败回滚本批、重跑不重复；不请求上游、不处理终态或 Flow，Worker 可继续。

## 代码与接口边界

| 位置/接口 | 职责 |
|---|---|
| adapters/channels.py：CHANNELS、protocol/supports | 显式组合能力，无数据库/网络 |
| adapters/inference/ 协议 validate_request | 校验实际报文与固定目标，返回 ModelRequestBudget |
| 文本/图片/原生任务协议 | 明确 DTO＋短期配置＋受控传输 → 类型结果/事件/回执 |
| adapters/billing/：binding/receipt/lookup/page/balance | 原范围、回执、费用、分页、余额；仅支持的实现提供 |
| inference/client、transport、errors；billing/registry | SDK 生命周期、发送观察、宿主错误转换 |
| adapters/http、credentials、coordination | 受控下载、秘密、共享技术协调 |
| services/entries、channels、queries | 配置、SQL 授权、角色批量绑定 |
| services/runtime、image_results、image_tasks、costs | 发送登记、预算、证据、原结果恢复和费用核对 |
| composition.py | 显式注入依赖，协议与宿主同制品发布 |

协议仅依赖轻量 DTO 和传入 SDK/传输，不导入 ORM、Service、用户权限、DBOS/FlowContext。AdapterError/QueryLater 在宿主边界转换；秘密本次注入，不进快照。没有统一大基类、在线插件或数据库请求模板。

InvocationContext 的 workflow_id 长 1～512，step_id 为 0～2³¹−1 整数，保存原发送位置，异步查询不覆盖。image_sources_in_transaction 每批最多 100，在同 Run 与当前可见记录内核对 Manifest 和登记字节元数据，只返回确认来源。

Flow 批量绑定按全部 Entry→全部 Channel、ID 去重排序。恢复不重新解析 key，不新增执行/交付/取消状态机；视频须具体协议验证后另接。代码、目录、锁文件和资源变化影响 execution_version，旧回执格式及在途运行保留原制品，发布见[部署](../operations/deployment.md#3-更新与恢复)。

验证使用本地协议桩及真实 PostgreSQL/DBOS，覆盖能力一致、登记先发送、固定选择、原结果恢复、未知不重发、取消及共享核对。仅 Yunwu/Openlux 已记录的探针属于上游实测，其余本地回归不声称已完成付费线上验收。
