from threading import Lock

from src.harness.agent_loop.run_turn import Complete, Message, run_turn


class ChatSession:
    def __init__(self, complete: Complete) -> None:
        self._complete = complete
        self._history: list[Message] = []
        self._turn_lock = Lock()

    def set_model(self, complete: Complete) -> None:
        with self._turn_lock:
            self._complete = complete

    def send(self, content: str) -> str:
        if not content.strip():
            raise ValueError("请输入消息。")
        if not self._turn_lock.acquire(blocking=False):
            raise RuntimeError("请等当前回复结束。")

        try:
            user: Message = {"role": "user", "content": content}
            messages = [*self._history, user]
            assistant = run_turn(messages, self._complete)
            self._history = [*messages, assistant]
            return assistant["content"]
        finally:
            self._turn_lock.release()
