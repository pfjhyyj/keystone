from typing import Annotated

from pydantic import StringConstraints

Name = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f/\\]+$"
    ),
]
