"""HerdrAppBackend: TerminalBackend on herdr-core, shown in the Herdr app."""

from typing import Any


class HerdrAppBackend:  # stub until Task 3
    def __init__(self, herdr_app_session: str = "cao", client: Any = None) -> None:
        self._session = herdr_app_session
        self._client = client
        self._connect()

    def _connect(self) -> None:
        raise NotImplementedError

    @property
    def herdr_app_session(self) -> str:
        return self._session
