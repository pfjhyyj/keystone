"""Write-only cloud accounting credentials, independent of inference API keys."""

import json

from pydantic import ConfigDict, Field, SecretStr, field_validator

from keystone.core.schemas import Input


class CloudBillingCredentials(Input):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    access_key_id: SecretStr = Field(min_length=1, max_length=256, repr=False)
    access_key_secret: SecretStr = Field(min_length=1, max_length=1024, repr=False)
    security_token: SecretStr | None = Field(
        default=None, min_length=1, max_length=4096, repr=False
    )

    @field_validator("access_key_id", "access_key_secret", "security_token")
    @classmethod
    def safe_secret(cls, value):
        if value is not None:
            raw = value.get_secret_value()
            if not raw.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in raw):
                raise ValueError(
                    "Cloud credentials must contain printable ASCII without whitespace"
                )
        return value

    def encoded(self):
        return SecretStr(
            json.dumps(
                {
                    name: value.get_secret_value()
                    for name in type(self).model_fields
                    if (value := getattr(self, name)) is not None
                },
                separators=(",", ":"),
            )
        )


class CloudBillQuery(Input):
    period: str = Field(pattern=r"^20\d{2}-(0[1-9]|1[0-2])$")
    offset: int = Field(default=0, ge=0, le=10000)
