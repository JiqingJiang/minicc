from collections.abc import Callable
from typing import Literal, TypedDict


class Message(TypedDict):
    role: Literal["user", "assistant"]
    content: str


Complete = Callable[[list[Message]], str | None]


def run_turn(messages: list[Message], complete: Complete) -> Message:
    content = complete(messages)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("模型没有返回可显示的回复。")
    return {"role": "assistant", "content": content}
