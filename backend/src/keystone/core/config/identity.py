from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator


class IdentitySettings(BaseModel):
    initial_admin_password: SecretStr | None = Field(default=None, repr=False)
    session_hours: int = Field(default=8, ge=1, le=168)
    pat_max_days: int = Field(default=90, ge=1, le=365)
    browser_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
    ]

    @field_validator("initial_admin_password", mode="before")
    @classmethod
    def empty_initial_password(cls, value):
        return None if value == "" else value

    def validate_browser_origins(self, *, production):
        for origin in self.browser_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or (production and parsed.scheme != "https")
            ):
                raise ValueError("Configure exact browser origins; production requires HTTPS")
        if not self.browser_origins:
            raise ValueError("Configure at least one browser origin")
