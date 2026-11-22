"""Provider-neutral message content; adapters render it and reject what they cannot show."""

from typing import Annotated, Literal

from pydantic import Field, HttpUrl, TypeAdapter, field_validator

from keystone.core.schemas import Input

MAX_CONTENT_BYTES = 16 * 1024


class CardField(Input):
    label: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=512)


class CardLink(Input):
    text: str = Field(min_length=1, max_length=32)
    url: HttpUrl

    @field_validator("url")
    @classmethod
    def no_credentials(cls, value):
        if value.username or value.password or value.scheme not in {"http", "https"}:
            raise ValueError("Links must be plain http or https URLs")
        return value


class TextContent(Input):
    kind: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=4000)


class CardContent(Input):
    kind: Literal["card"] = "card"
    title: str = Field(min_length=1, max_length=128)
    body: str | None = Field(None, max_length=4000)
    fields: list[CardField] = Field(default_factory=list, max_length=10)
    links: list[CardLink] = Field(default_factory=list, max_length=5)


Content = Annotated[TextContent | CardContent, Field(discriminator="kind")]
content_adapter = TypeAdapter(Content)
