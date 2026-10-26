"""Lightweight operation contracts shared by model configuration and Flow roles."""

from typing import Literal, get_args

Operation = Literal[
    "text.generate",
    "text.structured",
    "text.embed",
    "image.understand",
    "image.generate",
    "image.edit",
]
Delivery = Literal["sync", "async"]

OPERATIONS = frozenset(get_args(Operation))
