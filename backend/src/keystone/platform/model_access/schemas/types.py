from typing import Annotated, Literal

from pydantic import StringConstraints

Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Provider = Literal["deepseek", "openrouter", "aliyun_bailian"]
ModelName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}$")]
