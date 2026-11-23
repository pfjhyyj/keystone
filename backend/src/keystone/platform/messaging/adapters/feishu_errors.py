"""What an operator should do about a Feishu error code; the codes themselves stay as recorded.

https://open.feishu.cn/document/server-docs/im-v1/message/create
https://open.feishu.cn/document/server-docs/group/chat/list
https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
"""

HINTS = {
    # App credentials and setup.
    "credential_invalid": "App ID 或 App Secret 不正确，或应用未启用",
    "token_unavailable": "暂时无法从飞书获取访问凭证，稍后会自动重试",
    "feishu_token_20002": "App Secret 不正确，请在渠道中更换",
    "feishu_token_20048": "App ID 不存在，请确认后新建渠道",
    "feishu_token_20069": "应用未启用，请在飞书开发者后台启用应用",
    "feishu_token_20009": "应用尚未安装到企业，请发布版本并由管理员审核通过",
    "feishu_99991672": "应用缺少接口权限：发消息需“以应用的身份发消息”（im:message:send_as_bot），"
    "选群需“获取群组信息”（im:chat:readonly），绑定账号需“通过手机号或邮箱获取用户 ID”"
    "（contact:user.id:readonly）；开通后发布版本才生效",
    # Sending to a bound user.
    "feishu_230013": "该用户不在应用的可用范围内，请在飞书开发者后台调整可用范围",
    "feishu_230029": "该用户已离职",
    "feishu_230053": "该用户已屏蔽应用发来的消息",
    "recipient_unbound": "该用户已解除飞书账号绑定，消息没有发出",
    "recipient_disabled": "该用户已停用，消息没有发出",
    "feishu_230027": "应用缺少发消息所需权限，请开通“以应用的身份发消息”并发布版本",
    "feishu_230006": "应用未启用机器人能力，请在飞书开发者后台添加机器人能力并发布版本",
    "feishu_232025": "应用未启用机器人能力，请在飞书开发者后台添加机器人能力并发布版本",
    "feishu_232034": "应用未安装或未在企业内启用，请发布版本并由管理员审核通过",
    # Sending to a group.
    "feishu_230002": "机器人不在这个群里，请先把应用机器人拉进群",
    "feishu_232009": "群已解散，请删除这个发送目标",
    "feishu_230018": "群设置不允许机器人发言",
    "feishu_230035": "机器人被禁言或被企业策略限制发消息",
    "feishu_230022": "消息内容含敏感信息，被飞书拦截",
    "feishu_230028": "消息未通过飞书数据防泄漏审核",
    "provider_rate_limited": "触发飞书限频：自建应用稍后自动重试，群机器人的这条消息没有发出",
    "content_too_large": "消息内容超过飞书的大小限制",
    # Group custom bots.
    "feishu_19021": "签名校验失败：签名密钥不对，或机器人未开启签名校验却填了密钥",
    "feishu_19022": "机器人设置了 IP 白名单，当前服务器不在白名单内",
    "feishu_19024": "机器人设置了关键词，消息里没有包含关键词",
    # Outcomes the platform records itself.
    "send_unconfirmed": "飞书没有明确答复，消息可能已送达；自建应用在去重窗口内用同一投递重试，"
    "群机器人不再重发",
    "send_interrupted": "发送过程中进程中断，消息可能已送达",
    "credential_unavailable": "渠道凭据或加密密钥不可用，请检查密钥文件或重新填写凭据",
}


def hint(code):
    return HINTS.get(code) if code else None
