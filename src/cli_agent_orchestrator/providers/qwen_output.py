"""Read Qwen's native JSON side channel without publishing reasoning/tool blocks."""

import json
import threading
from pathlib import Path

from cli_agent_orchestrator.providers.base import OutputExtractionError, OutputExtractionRejected


class QwenOutput:
    """Incremental reader for one private, per-launch JSONL file (Qwen >= 0.28).

    Interactive mode emits assistant messages, not the headless ``result`` event.
    Readiness still comes from the composed TUI. A new dispatch is checkpointed
    BEFORE paste; output from the previous user turn cannot satisfy it.
    """

    def __init__(self, path: Path):
        self.path = path
        self._offset = 0
        self._partial = b""
        self._users = 0
        self._required_user = 0
        self._text = ""
        self._message_id = None
        self._failed = False
        self._lock = threading.RLock()

    def _refresh(self) -> None:
        if self._failed:
            raise OutputExtractionError("Qwen JSON output reader failed; recreate the terminal")
        try:
            self._read_events()
        except OutputExtractionError:
            self._failed = True
            raise

    def _read_events(self) -> None:
        try:
            with self.path.open("rb") as stream:
                end = stream.seek(0, 2)
                if end < self._offset:
                    raise OutputExtractionError("Qwen JSON output was truncated")
                stream.seek(self._offset)
                # Consume the whole observed generation before checkpointing or
                # publishing, with bounded memory even for long conversations.
                while self._offset < end:
                    chunk = stream.read(min(1024 * 1024, end - self._offset))
                    if not chunk:
                        raise OutputExtractionError("Qwen JSON output changed while reading")
                    self._offset = stream.tell()
                    self._consume(chunk)
        except OSError as exc:
            raise OutputExtractionError("Qwen JSON output is unavailable") from exc

    def _consume(self, chunk: bytes) -> None:
        lines = (self._partial + chunk).split(b"\n")
        self._partial = lines.pop()
        if len(self._partial) > 8 * 1024 * 1024:
            raise OutputExtractionError("Qwen JSON record exceeds the reader limit")
        for line in lines:
            if len(line) > 8 * 1024 * 1024:
                raise OutputExtractionError("Qwen JSON record exceeds the reader limit")
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise OutputExtractionError("Invalid Qwen JSON event") from exc
            if not isinstance(event, dict):
                raise OutputExtractionError("Invalid Qwen JSON event shape")
            if event.get("parent_tool_use_id"):
                continue  # A nested agent is not this terminal's final answer.
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content", [])
            if not isinstance(content, list):
                raise OutputExtractionError("Invalid Qwen message content")
            blocks = [block for block in content if isinstance(block, dict)]
            texts = [
                block["text"]
                for block in blocks
                if block.get("type") == "text" and isinstance(block.get("text"), str)
            ]
            if event.get("type") == "user" and texts:
                self._users += 1
                self._text = ""
                self._message_id = None
            elif event.get("type") == "assistant":
                if any(block.get("type") == "tool_use" for block in blocks):
                    self._text = ""
                    self._message_id = None
                elif texts:
                    message_id = message.get("id")
                    if message_id != self._message_id:
                        self._text = ""
                    self._message_id = message_id
                    self._text += ("\n" if self._text else "") + "\n".join(texts)

    def begin_dispatch(self) -> None:
        with self._lock:
            self._refresh()
            self._required_user = self._users + 1
            self._text = ""
            self._message_id = None

    def has_current_user(self) -> bool:
        with self._lock:
            self._refresh()
            return self._users >= self._required_user

    @property
    def user_turns(self) -> int:
        with self._lock:
            self._refresh()
            return self._users

    def response(self) -> str:
        with self._lock:
            self._refresh()
            if self._users < self._required_user:
                raise OutputExtractionError("Qwen has not acknowledged the current dispatch")
            if not self._text.strip():
                raise OutputExtractionRejected("Qwen has no publishable text for the current turn")
            return self._text.strip()
