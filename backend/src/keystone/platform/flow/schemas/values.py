"""Bounded JSON values accepted by Flow inputs and outputs."""

import json

from keystone.core.errors import ServiceError


def small_json(value):
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        if len(raw) > 65536:
            raise ValueError()
        return raw
    except (ValueError, TypeError, RecursionError):
        raise ServiceError(
            422, "flow_value_too_large", "流程数据须为不超过 64 KiB 的 JSON"
        ) from None
