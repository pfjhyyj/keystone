from typing import Annotated

from pydantic import Field, SecretStr, StringConstraints

Username = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=3,
        max_length=64,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$",
    ),
]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Password = Annotated[SecretStr, Field(min_length=8, max_length=128)]
