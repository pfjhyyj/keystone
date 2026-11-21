"""Managed storage keys and the layouts already recorded in the database."""

import mimetypes
import re
from pathlib import PurePath

from keystone.core.time import utcnow

_ID = r"[0-9a-f]{32}"
_SUFFIX = r"\.[a-zA-Z0-9]{1,16}"
OBJECT_KEY = re.compile(rf"files/(?:[0-9]{{8}}/)?{_ID}(?:{_SUFFIX})?")


def content_suffix(name, content_type):
    """A safe suffix for people and suffix-sniffing tools; never a content-type decision."""
    suffix = PurePath(name).suffix
    if not re.fullmatch(_SUFFIX, suffix):
        suffix = mimetypes.guess_extension(content_type.lower(), strict=False) or ""
    return suffix if re.fullmatch(_SUFFIX, suffix) else ".bin"


def new_object_key(identifier, name, content_type):
    """Choose the date and suffix once, when the File is registered."""
    return f"files/{utcnow():%Y%m%d}/{identifier.hex}{content_suffix(name, content_type)}"
