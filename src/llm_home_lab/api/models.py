from typing import Any

from pydantic import BaseModel, Field

GENERATION_FIELDS = ("temperature", "max_tokens", "chat_template_kwargs", "response_format")


class Message(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    session_id: str | None = None
    task_type: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)
    chat_template_kwargs: dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None

    def unset_generation_fields(self) -> set[str]:
        """Names of the optional generation parameters the client did not set.

        Backends exclude these from the payload they build so an unset parameter is omitted
        rather than sent as `null`, leaving the backend's own default in force.
        """
        return {name for name in GENERATION_FIELDS if getattr(self, name) is None}
