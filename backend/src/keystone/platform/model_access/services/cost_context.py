"""Bind original non-secret billing scope inside the caller's transaction."""

from keystone.core.errors import ServiceError


def bind_channel(channel, clients):
    adapter = clients.billing.adapter(channel.provider)
    try:
        binding = adapter.binding(channel.api_base)
    except ValueError:
        raise ServiceError(
            409, "model_target_not_allowed", "中转渠道须使用对应站点的固定地址"
        ) from None
    if binding is None:
        return None
    secret = clients.credentials.decrypt(channel.api_key_encrypted, channel.id, "inference")
    scope = clients.credentials.scope(binding.origin, secret)
    context = binding.snapshot(scope, channel.provider, channel.api_base)
    channel.cost_scope, channel.cost_context = scope, context
    return dict(context)
