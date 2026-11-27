from pydantic import BaseModel, Field, SecretStr, field_validator


class IdentitySettings(BaseModel):
    initial_admin_password: SecretStr | None = Field(default=None, repr=False)
    session_hours: int = Field(default=72, ge=1, le=168)
    pat_max_days: int = Field(default=90, ge=1, le=365)

    @field_validator("initial_admin_password", mode="before")
    @classmethod
    def empty_initial_password(cls, value):
        return None if value == "" else value
