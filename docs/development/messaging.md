# 业务发送消息与卡片

业务模块通过消息渠道把通知和带按钮的卡片发到飞书，在用户点击按钮或发送固定命令时由业务决定做什么。契约与限制见[消息渠道](../domains/messaging.md)，飞书应用的开通和控制台配置见控制台“消息渠道”页的配置说明。下面以[业务接入](business.md)的 notes 模块为例。

## 1. 发送

在业务 Service 自己的身份事务里调用 `enqueue_in_transaction`，与业务变更一起提交；事务回滚时消息不会发出。Worker 每 5 秒发送一轮，业务不直接调用飞书接口。

```python
from keystone.platform.messaging.services.deliveries import enqueue_in_transaction

async def submit(self, identifier):
    async with self.context.access(
        "notes.note:write", action="notes.note.submit", target="notes.note", target_id=identifier
    ) as (session, auth):
        row = await self._owned(session, auth, identifier)
        row.status = "submitted"
        await enqueue_in_transaction(
            session,
            auth,
            destination_key="notes.review",  # 或 user_id=reviewer_id，二选一
            content={
                "kind": "card",
                "title": f"待审阅：{row.title}",
                "fields": [{"label": "提交人", "value": auth.user.display_name}],
                "actions": [
                    {"key": "approve", "text": "通过", "style": "primary"},
                    {"key": "reject", "text": "退回", "style": "danger"},
                ],
            },
            source_module="notes",
            source_type="note",
            source_id=str(row.id),
        )
        await self.context.log(session, auth, "notes.note.submit", "notes.note", row.id)
```

| 参数 | 说明 |
|---|---|
| destination_key | 管理员在控制台配置的消息标识：群机器人的标识或自建应用里某个群的标识。业务不传飞书群 ID，换群只改配置 |
| user_id | 发给一位 Keystone 用户，取其在已启用自建应用上最早的绑定；未绑定或已停用时登记失败 |
| inbound_id | 回复本模块收到的一条命令，发到原消息所在的会话，见[第 3 节](#3-处理命令) |
| content / supplied | 二选一：直接给内容，或 `supplied="text"`/`"card"` 在发送时再取内容，见[第 4 节](#4-发送时取内容) |
| source_module / source_type / source_id | 业务命名空间、对象类型和对象 ID（各 ≤48/64/64 字符）；点击按钮时平台按此找到声明的动作和业务对象 |
| expires_at | 可选截止时间，过了仍未发出就不再发送 |

内容只能是下面两种，序列化后不超过 16 KiB，不放凭据或其他秘密；投递记录不对控制台展示内容：

| kind | 字段 |
|---|---|
| text | text（≤4000 字） |
| card | title（≤128）、body（≤4000）、fields（≤10 个 label/value）、links（≤5 个 http(s) 链接按钮）、actions（≤4 个按钮：key、text ≤16 字、style 为 default/primary/danger） |

所有文字按纯文本渲染，不解析 Markdown 或 @。带 actions 的卡片只能发给自建应用（群或绑定用户），发给群机器人时登记失败。

| 登记失败 | 含义 |
|---|---|
| 409 message_destination_unavailable | 消息标识不存在，或渠道/群已停用 |
| 409 message_recipient_unbound / message_recipient_disabled | 用户没有可用绑定或已停用 |
| 404 message_reply_unavailable | 要回复的入站不存在、不是命令，或不是本模块的命令 |
| 422 message_actions_unsupported | 带按钮的卡片发给了群机器人 |
| 422 message_content_too_large / message_already_expired | 内容超限，或截止时间已过 |

登记失败是 ServiceError，会让整个业务事务回滚。通知不该阻挡业务时，业务在调用前自行判断（例如用户是否需要通知），不要吞掉异常后继续使用同一个事务。

## 2. 处理按钮点击

按钮点击由 Worker 收到后，以点击人绑定的 Keystone 用户身份调用业务声明的 handler。在 `composition.py` 显式列出：

```python
from keystone.platform.messaging.services.actions import MessageAction
from keystone.businesses.notes.services.review import review_note

MESSAGE_ACTIONS = (
    MessageAction("notes", "note", "approve", "notes.note:review", review_note),
    MessageAction("notes", "note", "reject", "notes.note:review", review_note),
)
```

字段依次为 source_module、source_type、按钮 key、执行身份须具备的功能权限（可为 None）和 handler。同一组 source 与 key 重复、或权限未在 catalog 中声明时启动失败。默认以点击人绑定的用户执行；发到群里的卡片可以声明 `identity="destination_user"`，群里任何人点击都以管理员为该群设置的执行用户执行，点击人不需要绑定（回复命令的卡片属于命令所在的群）。handler 所在模块会被 API 和 Worker 导入，保持轻量，不在导入时建连接。

```python
from keystone.core.errors import ServiceError

async def review_note(runtime, identity, source_id, action_key, inbound_id):
    service = NoteService(identity)  # identity 即点击人的身份，用法同 AccessContext
    await service.review(source_id, approved=action_key == "approve", inbound_id=inbound_id)
```

```python
async def review(self, identifier, *, approved, inbound_id):
    async with self.context.access(
        "notes.note:review", action="notes.note.review", target="notes.note", target_id=identifier
    ) as (session, auth):
        row = await session.get(Note, UUID(identifier), with_for_update=True)
        if row is None:
            raise ServiceError(404, "note_not_found", "笔记不存在")
        if row.status != "submitted":
            # 已经处理过：按业务状态判断，重复点击得到同样的结果
            if row.status == ("approved" if approved else "returned"):
                return
            raise ServiceError(409, "note_already_reviewed", "这篇笔记已被他人处理")
        row.status = "approved" if approved else "returned"
        await self.context.log(
            session, auth, "notes.note.review", "notes.note", row.id,
            {"approved": approved, "via": "messaging", "inbound_id": str(inbound_id)},
        )
```

handler 约定：

- **只受理**：校验业务规则、保存决定，需要较长处理时登记业务任务或 Flow 后返回。平台给整次处理 2 秒预算，超时由 Worker 补处理，点击人先看到“已收到，正在处理”。handler 与 Worker 共用事件循环，不得执行同步阻塞调用或耗时计算。
- **幂等**：同一点击可能被处理不止一次（补处理租约过期、飞书重推），按业务状态判断是否已处理，不按入站次数。
- **授权**：声明的功能权限由平台在调用前检查；对象范围（例如只能审阅本组的笔记）仍由业务 Service 判断。执行身份是点击人当前的角色权限，不继承发送人身份，也不叠加 PAT 范围。
- **拒绝**：抛出 ServiceError，detail 会作为错误提示直接显示给点击人，只写可以给对方看的话；卡片保持不变，对方可以修正后再点。
- **审计**：业务自己写，操作者就是点击人；details 记录 `via=messaging` 和 `inbound_id`，这两个字段要加入该审计动作的允许集合。点击人的飞书 open_id 在入站记录里，不必重复。
- **返回值**：被忽略。成功后平台把卡片按钮换成“{点击人} 已{按钮文字}”，业务无法自定义更新后的卡片。

点击人须先绑定飞书账号（个人中心自助绑定或管理员绑定），否则平台直接拒绝并提示绑定，不调用 handler。

## 3. 处理命令

用户私聊应用、或在已配置的群里 @应用发送一段固定文字，平台按声明完整匹配后调用 handler。在 `composition.py` 显式列出：

```python
from datetime import timedelta

from keystone.platform.messaging.services.actions import MessageCommand
from keystone.businesses.notes.services.digest import request_digest

MESSAGE_COMMANDS = (
    MessageCommand(
        "notes",                    # 模块
        "digest",                   # command_key，入站记录只保存它，不保存原文
        "笔记摘要",                  # 去掉 @ 和首尾空白后与消息完全相同才匹配
        ("p2p", "group"),           # 私聊、群里，或两者
        "notes.note:read",          # 执行身份须具备的功能权限（可为 None）
        request_digest,
        accept_within=timedelta(minutes=5),  # 消息发出后超过这个时间才处理就作废
        reply_failures=True,        # 拒绝、过期、处理失败时由平台回复原消息
    ),
)
```

同模块的 key 重复、不同命令的文字相同、文字以“绑定”开头或权限未声明时启动失败。群里只有配置成发送目标的群才接收命令，其他群的消息直接忽略。

```python
async def request_digest(identity, inbound_id, command_key):
    await NoteService(identity).request_digest(inbound_id)
```

```python
async def request_digest(self, inbound_id):
    async with self.context.access("notes.note:read", action="notes.digest.request") as (session, auth):
        job = DigestJob(owner_id=auth.user.id, inbound_id=inbound_id, status="queued")
        session.add(job)
        await session.flush()
        # 登记 Flow 或业务任务后返回；需要立即确认时在同一事务里回复
        await enqueue_in_transaction(
            session,
            auth,
            inbound_id=inbound_id,
            content={"kind": "text", "text": "已收到，正在生成摘要"},
            source_module="notes",
            source_type="digest",
            source_id=str(job.id),
        )
        await self.context.log(
            session, auth, "notes.digest.request", "notes.digest", job.id,
            {"via": "messaging", "inbound_id": str(inbound_id)},
        )
```

handler 约定与按钮相同（只受理、幂等、ServiceError 拒绝、业务自己写审计），另有：

- **结果回复**：平台不等待结果。结果由 Flow 产生时，在 Flow 最后一个 Step 中以原身份用保存的 inbound_id 登记回复。Flow 失败、取消或超时不会执行业务代码；需要回复这些结果时，业务用自己的计划找出仍未回复的入站及其 Flow 终态再登记。
- **失败回复**：`reply_failures=True` 时，handler 抛出的 ServiceError 说明、命令过期、未绑定、群没有执行用户、无权限和多次处理失败都由平台回复原消息，handler 不必处理；为 False 时这些情况不回复。
- **执行身份**：默认是发送人绑定的用户，未绑定时拒绝。`identity="destination_user"` 的命令只能在群里用，以管理员在“消息渠道 → 群 → 设置执行用户”中为该群指定的用户执行，发送人不需要绑定；设置执行用户需要 `messaging:delegate`，且执行用户的权限不能超过设置人。
- **回复只限本模块**：`inbound_id` 必须是本模块命令产生的入站。回复发到原消息所在的私聊或群。

## 4. 发送时取内容

正文含有不应落库的内容（例如带凭据的订阅链接）时，登记时只声明种类，由 Worker 发送前向业务要内容：

```python
await enqueue_in_transaction(
    session,
    auth,
    inbound_id=inbound_id,  # 或 destination_key / user_id
    supplied="text",
    source_module="notes",
    source_type="share_link",
    source_id=str(row.id),
)
```

```python
from keystone.platform.messaging.services.actions import MessageContent

async def share_link(identity, source_id):
    # identity 是登记投递的用户，按其当前权限执行；只读，不改变业务状态
    link = await ShareService(identity).current_link(source_id)
    return None if link is None else {"kind": "text", "text": f"订阅链接：{link}"}

MESSAGE_CONTENTS = (MessageContent("notes", "share_link", share_link),)
```

- 返回 None 表示内容已不再可用，投递记为已过期，不再发送。
- 抛出 ServiceError（包括登记人已停用或失去权限）、没有声明提供函数，或返回的种类与登记时不同、卡片带按钮，投递失败。其他异常或超过 10 秒视为没有发出，按发送前被拒的规则重试。
- 内容不进入投递记录、日志和错误信息；不要在异常说明里写入内容。
- 每次尝试都会重新取，内容可能不同，所以只在飞书自建应用这类按投递 ID 去重的渠道上重试。

## 5. 测试

- 发送：在业务 Service 的集成测试里断言 `message_deliveries` 出现对应 source 的 pending 记录，并覆盖业务事务回滚时没有记录。不需要真实飞书。
- 按钮：参照 `backend/tests/integration/test_messaging.py` 的 `card_click`、`receive` 辅助函数构造点击事件，把业务的 `MessageAction` 放进 `runtime.message_actions`，断言业务状态、审计和 `/api/v1/messaging/inbounds` 中的结果；同一事件重复送达、无权限、业务拒绝都应覆盖。
- 命令与取内容：参照 `backend/tests/integration/test_messaging_commands.py` 的 `group_text`、`declare` 构造命令并替换 `runtime.message_commands`、`runtime.message_contents`，把 `feishu_app.reply` 换成记录调用的假实现，断言回复、失败回复和投递记录中不含内容。
- 真实联调：在控制台对自建应用的群或成员“发送测试卡片”，确认长连接和绑定可用，再用业务流程发送。
