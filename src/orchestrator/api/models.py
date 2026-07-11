from pydantic import BaseModel, Field


class Message(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
