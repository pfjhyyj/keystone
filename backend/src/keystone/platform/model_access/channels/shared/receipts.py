"""Shared bounded receipt identifiers, without protocol or billing policy."""

import re


def request_id(value):
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value) else None
    )
