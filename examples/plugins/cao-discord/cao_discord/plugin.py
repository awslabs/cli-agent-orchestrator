"""Discord plugin lifecycle, hook handling, and webhook dispatch."""

import logging
import os

import httpx
from dotenv import find_dotenv, load_dotenv

from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.plugins import PostSendMessageEvent, hook
from cli_agent_orchestrator.plugins.base import CaoPlugin

logger = logging.getLogger(__name__)


class DiscordPlugin(CaoPlugin):
    """Discord webhook plugin for CAO inter-agent messaging events."""

    _webhook_url: str
    _client: httpx.AsyncClient

    async def setup(self) -> None:
        """Load configuration and initialize the HTTP client."""

        load_dotenv(find_dotenv(usecwd=True))

        webhook_url = os.environ.get("CAO_DISCORD_WEBHOOK_URL")
        if not webhook_url:
            raise RuntimeError(
                "CAO_DISCORD_WEBHOOK_URL is not set. "
                "Set it in the environment or in a .env file before starting cao-server."
            )

        self._webhook_url = webhook_url
        timeout = float(os.environ.get("CAO_DISCORD_TIMEOUT_SECONDS", "5.0"))
        self._client = httpx.AsyncClient(timeout=timeout)

    async def teardown(self) -> None:
        """Close the HTTP client when setup completed successfully."""

        if hasattr(self, "_client"):
            await self._client.aclose()

    @hook("post_send_message")
    async def on_post_send_message(self, event: PostSendMessageEvent) -> None:
        """Forward post-send-message events to the configured Discord webhook."""

        display_name = self._resolve_display_name(event.sender)
        await self._post(username=display_name, content=event.message)

    def _resolve_display_name(self, terminal_id: str) -> str:
        """Resolve a human-friendly sender name from terminal metadata."""

        metadata = get_terminal_metadata(terminal_id)
        if metadata is None:
            return terminal_id
        return metadata.get("tmux_window") or terminal_id

    async def _post(self, *, username: str, content: str) -> None:
        """Send a Discord webhook payload and swallow all HTTP failures."""

        try:
            response = await self._client.post(
                self._webhook_url,
                json={"username": username, "content": content},
            )
            if response.status_code >= 400:
                logger.warning(
                    "Discord webhook POST failed: %s %s",
                    response.status_code,
                    response.text[:200],
                )
        except httpx.HTTPError as exc:
            logger.warning("Discord webhook POST raised: %s", exc)
