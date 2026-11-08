from typing import Annotated

from pydantic import AfterValidator, StringConstraints

Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


def installed_provider(value):
    # Defer loading channel declarations until validation, avoiding schema import cycles.
    from keystone.platform.model_access.channels import CHANNELS

    if value not in CHANNELS:
        raise ValueError("Unknown model provider")
    return value


Provider = Annotated[str, StringConstraints(max_length=32), AfterValidator(installed_provider)]
ModelName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}$")]
WorkspaceId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    ),
]
