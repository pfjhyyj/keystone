from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


class EnvironmentSettings(BaseModel):
    opensandbox_url: str | None = None
    opensandbox_key: SecretStr | None = Field(default=None, repr=False)
    browser_runtime_image: str | None = None
    browser_backup_key: SecretStr | None = Field(default=None, repr=False)

    @field_validator("browser_backup_key", mode="before")
    @classmethod
    def empty_browser_backup_key(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def valid_environment_providers(self):
        if self.browser_backup_key is not None:
            key = self.browser_backup_key.get_secret_value()
            if len(key) != 64 or any(char not in "0123456789abcdefABCDEF" for char in key):
                raise ValueError("browser_backup_key must be a 32-byte hexadecimal key")
            if not all((self.opensandbox_url, self.opensandbox_key, self.browser_runtime_image)):
                raise ValueError("browser backup requires OpenSandbox and a browser runtime image")
        if (
            self.opensandbox_url is None
            and self.opensandbox_key is None
            and self.browser_runtime_image is None
        ):
            return self
        url = urlsplit(self.opensandbox_url or "")
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or not self.opensandbox_key
            or not self.opensandbox_key.get_secret_value().strip()
            or not self.browser_runtime_image
            or not self.browser_runtime_image.strip()
        ):
            raise ValueError("Configure an OpenSandbox origin, API key, and browser image")
        return self
