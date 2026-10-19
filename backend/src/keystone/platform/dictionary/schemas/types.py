from typing import Annotated

from pydantic import StringConstraints

Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
