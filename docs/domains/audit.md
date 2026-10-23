# Keystone 审计日志

[范围与边界](#1-范围与边界) · [领域模型](#2-领域模型) · [主要流程](#3-主要流程) · [权限与审计](#4-权限与审计) · [设计取舍](#5-设计取舍) · [待确定事项](#6-待确定事项)

## 1. 范围与边界

审计日志回答：谁在什么时候，对什么对象，做了什么，结果如何。各领域决定哪些操作需要记录，审计模块负责统一保存和查询。

| 记录类型 | 用途 | 存放位置 |
|---|---|---|
| 审计日志 | 用户管理、授权变更、删除等重要操作的追溯 | AuditLog |
| 模型调用记录 | 每次上游尝试的用量、费用和结果 | ModelInvocation |
| 运行日志 | 堆栈、网络错误、调试信息、任务执行情况 | 应用日志 |

首版记录以下操作，不记录每一次普通读取或每一个数据库 UPDATE：

- 身份：登录成功/失败、退出与会话撤销、用户创建/停用/启用、资料修改、密码变更/重置、角色和权限关系变更、个人令牌签发/撤销。
- 字典：字典与选项的创建、名称及说明修改、排序调整和启停；普通查询与选项校验不逐次记录。
- 文件：上传开始/完成/失败、资料修改、启停、授权与引用的建立/移除、删除请求及清理完成/失败；创建默认授权可以并入上传开始日志。
- 模型接入：渠道和模型配置的创建、修改、启停及密钥变更；只记录密钥是否设置或更新，不保存密钥明文或密文。首版配置只停用、不删除。手动余额刷新记录 model.balance.refresh，保存实际操作者及查询结果状态；余额快照和采集状态保存在 Channel，不复制到审计详情。每次推理及费用补全保存在 ModelInvocation，不额外生成调用审计。
- Flow：运行发起、首次取消请求；目标为 Run。自动步骤进度和恢复诊断留在引擎与运行日志，不逐步骤复制审计；人工待办不在首版范围内。

上述管理操作的成功、业务失败和权限拒绝都可记录；登录失败也记录，但未认证的输入不能被当成已确认的用户身份。批量撤销等操作可用一条日志保存目标用户、范围及实际数量，无需给每条受影响记录再生成相同事件。

## 2. 领域模型

仅一个对象 AuditLog，建议表名 `audit_logs`：

| 字段 | 含义 |
|---|---|
| `id` | 日志 ID，由服务端生成 |
| `occurred_at` | 操作结果产生的时间，UTC，必填 |
| `actor_type` | `user`、`system`、`anonymous` |
| `actor_id` | user 时为已认证用户 ID，其他类型为空 |
| `action` | 稳定操作代码，例如 `identity.user.disable`、`file.reference`（details.added 区分建立或解除） |
| `target_type` | 目标类型，例如 `identity.user`、`file`；无明确目标时可空 |
| `target_id` | 目标的稳定标识，字符串；未知或尚未创建成功时可空 |
| `outcome` | `success`、`failure`、`denied` |
| `correlation_id` | 可空，串联同一请求或执行尝试；不能依赖它永久连接所有异步阶段 |
| `details` | 可空、小型 JSON，保存必要且脱敏的变更、原因或目标快照 |

日志是事实记录，不增加 created_at/created_by、updated_at/updated_by 或 deleted_at/deleted_by。时间和操作人已由 occurred_at、actor_type、actor_id 表达；已写入记录不提供修改功能。

action 和 target_type 由各模块在代码中声明，不建立操作类型表或目标对象表。actor_id、target_id 是历史引用，不建立会随业务记录删除而级联删除的关系；目标删除后仍可独立读取日志。

details 由每种操作按白名单构造。例如角色删除保存角色 key，文件授权移除保存 file_id、主体和动作，引用移除保存 owner、slot、file_id；修改操作只保存必要的字段前后值，不能自动序列化整行。密码变更只记录“已变更”，不记录密码、哈希、令牌、Cookie 或渠道秘密；也不保存提示词、模型输出、文件正文、完整请求或异常堆栈。

操作者按实际执行身份确定，不从目标的所属用户反推：

| 场景 | actor_type、actor_id | 其他必要信息 |
|---|---|---|
| 用户通过会话执行操作 | user、当前用户 ID | 目标由该操作决定 |
| 使用 PAT 发起业务操作 | user、PAT 所属用户 ID | 需要区分凭据时，details 保存非秘密令牌 ID |
| 管理员管理或撤销他人凭据 | user、实际管理员 ID | 目标为被管理用户，凭据类别与数量放入 details；此规则不额外承诺管理接口范围 |
| 代表用户执行的 Worker 业务操作 | user、可信执行上下文中的用户 ID | 仍检查该用户及原凭据权限，按需关联 run_id |
| 系统清理、已完成结果同步、后台确认删除 | system、空 | details 保存任务来源、run_id 等，原发起人通过关联记录追溯 |
| 未认证的登录失败 | anonymous、空 | 输入用户名只是未确认输入，不能充当已认证身份 |

“使用令牌操作”和“管理令牌本身”分别判断。运行在 Worker 中不自动意味着 system；以用户身份执行业务与平台维护、事实同步是不同来源。保留现有 actor_type、actor_id、目标字段、correlation_id 和 details 即可，不新增 operator_id、initiator_id 或操作者关系表。管理员撤销凭据时记录实际管理员，所属用户作为目标信息。

## 3. 主要流程

### 3.1 数据库内的管理操作

业务服务完成身份和权限检查后，在自己的事务里修改数据并调用统一审计写入入口。成功状态变更与成功日志同事务提交；审计写入失败则整个事务回滚，不能已修改数据却返回成功。

物理删除前提取必要的目标标识和安全快照，删除与日志一起提交。审计记录不随被删除对象清理。若业务失败或权限拒绝，在业务事务回滚后用独立短事务写 failure/denied，不留下 success。失败审计若也因数据库不可用而写入失败，保留原有失败结果并写运行日志告警，不递归审计审计模块自身的错误。

### 3.2 文件删除等外部操作

外部内容清理无法和数据库共用事务，分阶段记录事实，不在删除请求受理时声称内容已经删除：

| 阶段 | 操作与结果 | 含义 |
|---|---|---|
| 用户提交删除，文件进入 deleting | `file.delete.request` / success | 请求已受理，记录实际发起用户 |
| 后台清理完成，文件进入 deleted | `file.delete.complete` / success | 内容清理完成，同事务设置 deleted_at、清除 FileGrant，记录系统执行来源与授权清理范围 |
| 后台清理报错，文件仍为 deleting | `file.cleanup.failure` / failure | 本次清理失败，允许后续重试 |

这些日志共用 file_id，以操作代码区分请求、失败和完成，各阶段数据库状态与对应日志同事务提交。首版文件不恢复，删除只有一个不可逆生命周期，因此不要求持久传递原请求 correlation_id；后台每次执行可使用自己的关联标识。Flow 同理通过 run_id 追溯，不增加关联任务表或领域字段。后台执行者使用 system，原始发起人通过请求日志追溯，不能伪装成用户仍在直接执行。重试是新的尝试，追加真实结果，不修改此前失败日志。各类阶段日志按目标关联，不新增额外关联表。

确认文件删除时的授权清理可合并在完成日志中，保存清理数量、范围和必要授权标识，不要求逐条复制授权移除事件。进入 deleting 或内容清理失败时不提前清除授权；确认 deleted、授权清理与成功日志必须同事务提交。

进程中断可能没有最终结果日志；不能仅凭缺少完成记录推断清理失败或没有发生。恢复时核实实际存储状态再记录确认结果，保留重试/恢复来源。首版不为此引入消息队列或分布式事务。

## 4. 权限与审计

写入入口仅供服务端业务代码调用，不开放客户端任意新增日志的接口。操作人来自可信请求上下文，系统来源由服务端设置，目标和 details 由执行操作的模块生成。

通过现有 Casbin 声明 `audit:read`，仅授权用户可查询；个人令牌仍受自身权限范围限制。首版提供按时间、操作人、操作代码、目标和结果的分页查询，不提供编辑、逐条删除或普通用户全量读取接口。

日志按 occurred_at、id 排序；首版索引覆盖时间、操作人＋时间、目标类型/标识＋时间，不提前为 JSON 中每个键建立索引。目标名称等展示快照仅按需要放在 details，查询不能强依赖当前业务行仍然存在。

应用路径只追加日志；这不代表能够防止拥有数据库管理权限的人直接篡改。首版不声明防篡改或合规认证能力，也不为审计日志再生成一套创建、修改、删除审计字段。

## 5. 设计取舍

- 各领域显式记录有业务含义的操作，不通过 ORM 钩子自动记录所有表变化。
- 一张表、一份操作代码目录、一个写入入口；不做事件溯源、消息总线、通用差异计算或独立审计服务。
- 保留必要的行级字段方便查询当前状态，AuditLog 保存历史；两者用途不同，不能互相完全替代。
- 过期会话、调用记录等批量数据清理由运行日志记录任务、范围、数量和结果，不逐条扩增审计数据。
- 审计日志自身的保留期清理由受控维护任务执行，执行结果写运行日志；不开放业务删除接口，也不递归生成“删除审计日志的审计日志”。

## 6. 待确定事项

审计历史清理暂不纳入首版，不设定保留期限或维护入口；当前继续保留审计记录。details 上限为 UTF-8 JSON 8 KiB；每个 action 单独声明字段白名单，嵌套对象也检查。查询默认最近 30 天，单次范围最多 366 天，时间须带时区；默认 50 条、最多 100 条，offset 最多 100000。

本设计覆盖身份、字典、文件、模型接入和 Flow 的操作留痕，不要求每张业务表拥有相同审计字段；新增领域按相同入口补充必要操作即可。

## 7. 当前实现

AccessContext 成功取得授权上下文后，失败或拒绝日志与成功日志一样保留已认证 PAT 的 credential_id，仍写在 details 中；correlation_id 和实际用户身份保持不变。无效或未认证凭据只按现有匿名规则记录，不从客户端提供的令牌推测编号或所属人。余额刷新离开预检事务后的失败路径也传递此前认证取得的编号，不增加审计字段或额外身份查询。

平台动作由各所属模块的 catalog.py 声明，Worker 生命周期动作位于 worker/catalog.py；业务同样在自身 catalog.py 声明动作及详情字段白名单。composition.py 检查重复动作后合并成应用级不可变目录并显式绑定 Database，审计 writer 不保存全平台 ACTIONS。writer 从调用方事务的 session.info 读取目录，未绑定目录或动作未声明时拒绝写入；不同应用的目录不会互相覆盖。业务沿用下述事务、身份、关联编号、递归字段校验和 8 KiB 大小限制，不增加业务审计表；具体声明见[业务接入规范](../architecture.md#55-业务权限审计与模型接入)。

代码位于 backend/src/keystone/platform/audit/，表为 keystone.audit_logs，由 audit 领域初始迁移创建。record 接受调用方事务；成功日志写入失败时抛错并回滚整个业务操作。HTTP 用例服务的权限拒绝、业务冲突和执行失败在回滚后尝试独立写日志，失败审计不可用只记录脱敏运行告警，不覆盖原始错误。请求 Schema 校验和 CSRF 边界拒绝不自动解析请求正文生成领域审计。Flow 内部步骤失败通过运行终态的 flow.complete 及安全错误码留痕，不逐步骤复制失败审计。

登录失败使用 anonymous，成功登录使用验证后的用户；CLI 初始化使用 system；PAT 操作使用所属用户，details 可保存非秘密 credential_id；管理员批量撤销其他人的凭据记录实际管理员，目标及 details 保存被管理用户。密码、哈希、Cookie 和秘密令牌均不进入 details。occurred_at 在结果写入时取服务端 UTC，correlation_id 使用服务端生成的请求 ID。

GET /api/v1/audit-logs 要求 audit:read，支持 since/until、actor_type/actor_id、action、target_type/target_id、outcome、offset/limit，按 occurred_at、id 倒序返回。不提供客户端写入、修改或删除 API，也不依赖目标当前行。当前身份 action 目录统一为 identity.login/logout、identity.password.change/reset、identity.user.create/update/enable/disable、identity.credentials.revoke、identity.role.create/update/delete/assign、identity.pat.issue/revoke、identity.admin.initialize。

文件和字典已接入同一入口：file.upload.start/complete/failure/residue_cleanup、file.update、file.grant、file.reference、file.delete.request/complete、file.cleanup.failure，以及 dictionary.definition/item 的 create/update。用户发起的资料、授权、引用变更和删除请求记录实际用户（PAT 附 credential_id）；上传事实确认、清理及可信引用补齐记录 system。删除完成日志保留 file_id、授权清理范围及数量；失败清理不移除授权。Flow 事实同步已接入；审计保留期清理仍未启用。

模型接入使用 model.channel.create/update、model.entry.create/update、model.balance.refresh；显式重查单条调用费用另记 model.cost.recheck（target 为调用，details 保留 previous_status）。配置及启停沿用同事务管理审计；details 只保留声明的非秘密字段和密钥是否设置、是否更新的状态，不保存密钥明文或密文、消息或响应正文。手动余额刷新记录发起用户及查询状态，PAT 附 credential_id，不因此修改渠道配置审计字段；调用完成、用量和费用补查保存在 ModelInvocation 内，不重复生成每次调用的 AuditLog。测试传输替身同样遵守这些写入边界。

Flow 新增 flow.start、flow.cancel、flow.complete；发起/取消记录实际当前用户，完成同步记录 system。发起与 Run/输入引用、完成与终态/结果/输出引用分别同事务提交。报告上传开始记录原用户，上传完成及可信引用补齐记录 system；步骤和模型调用不重复记审计。撤权或取消不阻止已确认事实留痕。

模型调用的中断收尾统一使用 model.invocation.recover，操作者为 system，target_type 为 model.invocation，target_id 为原调用 ID，普通 API 离线收尾的 details.source 为 offline_api_recovery，持有独占锁的 Worker 启动收尾为 flow_worker_recovery，历史无归属 Flow 调用的停机专项收尾为 offline_flow_recovery。这是维护操作审计，区别于普通调用完成；每批状态更新与逐条审计同事务，写日志失败则回滚该批，重跑不重复记录已收尾项。命令失败写脱敏运行日志，不递归补写失败审计。平台操作名以代码 ACTIONS 目录为准，业务动作来自显式合并的 Catalog，不另设 files.* 别名。

### 7.1 控制台查询

/console/audit 要求 audit:read，提供每页 50 条的查询和只读详情。支持操作名、操作者类型/ID、目标类型/ID、结果及起止时间过滤；默认最近 30 天，单次最多 366 天。操作者 ID 按 UUID 校验，目标 ID 接受字符串，不把字典 key 等目标错误限制为 UUID。

进入页面或提交查询时固定本次的起止时间，并显示实际查询范围；只填写一个端点时也补全另一个。翻页和刷新沿用已提交的范围，不应用尚未提交的筛选输入；再次点击“查询审计”或重置可更新默认时间范围。

用户、系统、匿名操作者分别显示，缺少用户名称不会被推断为系统。详情中的 JSON 作为文本展示，沿用服务端脱敏结果，不补查用户资料或扩展审计字段。没有编辑、删除和导出入口。页面沿用统一的列表/详情方式，操作名和筛选规则以 Keystone API 为准。

### 代码组织

AuditLog 独立于 models/audit_log.py。services/writer.py 提供 Actor、白名单、成功写入和失败审计，保持调用方事务约定，不依赖 AccessContext；services/query.py 通过 AccessContext 完成授权查询。HTTP 与响应投影分别位于 controllers/logs.py、schemas/logs.py。 统一规范见[整体架构](../architecture.md#54-模块分层与代码规范)。

审计与模型调用历史共用时间窗口：since/until 均包含端点；未传 until 时取数据库当前时间，未传 since 时取 until 前 30 天，因此仅传历史 until 也可查询；最多 366 天，两个端点均须带时区。occurred_at 在写入时使用数据库时钟，避免应用与数据库时间差造成新事件被默认窗口遗漏。显式边界原样执行，查询错误继续使用 invalid_range。
