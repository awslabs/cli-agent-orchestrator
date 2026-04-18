"""Tests for Discord plugin configuration, lifecycle, and hook dispatch."""

import json

import httpx
import pytest

from cao_discord.plugin import DiscordPlugin
from cli_agent_orchestrator.plugins import PostSendMessageEvent


def _timeout_values(plugin: DiscordPlugin) -> tuple[float | None, float | None, float | None, float | None]:
    """Return the configured timeout values from the plugin's HTTP client."""

    timeout = plugin._client.timeout
    return timeout.connect, timeout.read, timeout.write, timeout.pool


async def _replace_client_with_mock_transport(
    plugin: DiscordPlugin, handler: httpx.MockTransport
) -> None:
    """Swap in a mock transport-backed client and close the setup client first."""

    await plugin._client.aclose()
    plugin._client = httpx.AsyncClient(transport=handler)


@pytest.mark.asyncio
async def test_setup_raises_when_webhook_url_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Missing configuration should raise a RuntimeError with guidance."""

    monkeypatch.delenv("CAO_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CAO_DISCORD_TIMEOUT_SECONDS", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_discord.plugin.find_dotenv", lambda usecwd=True: "")

    plugin = DiscordPlugin()

    with pytest.raises(RuntimeError, match="CAO_DISCORD_WEBHOOK_URL"):
        await plugin.setup()


@pytest.mark.asyncio
async def test_setup_reads_webhook_url_from_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A .env file in the process CWD should populate the webhook URL."""

    webhook_url = "https://discord.example/from-dotenv"
    (tmp_path / ".env").write_text(f"CAO_DISCORD_WEBHOOK_URL={webhook_url}\n", encoding="utf-8")

    monkeypatch.delenv("CAO_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CAO_DISCORD_TIMEOUT_SECONDS", raising=False)
    monkeypatch.chdir(tmp_path)

    plugin = DiscordPlugin()
    await plugin.setup()

    assert plugin._webhook_url == webhook_url
    await plugin.teardown()


@pytest.mark.asyncio
async def test_setup_prefers_process_env_over_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Process environment variables should override .env values."""

    dotenv_url = "https://discord.example/from-dotenv"
    env_url = "https://discord.example/from-env"
    (tmp_path / ".env").write_text(f"CAO_DISCORD_WEBHOOK_URL={dotenv_url}\n", encoding="utf-8")

    monkeypatch.setenv("CAO_DISCORD_WEBHOOK_URL", env_url)
    monkeypatch.delenv("CAO_DISCORD_TIMEOUT_SECONDS", raising=False)
    monkeypatch.chdir(tmp_path)

    plugin = DiscordPlugin()
    await plugin.setup()

    assert plugin._webhook_url == env_url
    await plugin.teardown()


@pytest.mark.asyncio
async def test_setup_uses_configured_timeout_or_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Timeout should default to 5.0 seconds and honor configured overrides."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_discord.plugin.find_dotenv", lambda usecwd=True: "")

    default_plugin = DiscordPlugin()
    monkeypatch.setenv("CAO_DISCORD_WEBHOOK_URL", "https://discord.example/default-timeout")
    monkeypatch.delenv("CAO_DISCORD_TIMEOUT_SECONDS", raising=False)
    await default_plugin.setup()

    configured_plugin = DiscordPlugin()
    monkeypatch.setenv("CAO_DISCORD_WEBHOOK_URL", "https://discord.example/custom-timeout")
    monkeypatch.setenv("CAO_DISCORD_TIMEOUT_SECONDS", "2.5")
    await configured_plugin.setup()

    assert _timeout_values(default_plugin) == (5.0, 5.0, 5.0, 5.0)
    assert _timeout_values(configured_plugin) == (2.5, 2.5, 2.5, 2.5)

    await default_plugin.teardown()
    await configured_plugin.teardown()


@pytest.mark.asyncio
async def test_teardown_is_safe_after_failed_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Teardown should be a no-op when setup failed before client creation."""

    monkeypatch.delenv("CAO_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CAO_DISCORD_TIMEOUT_SECONDS", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_discord.plugin.find_dotenv", lambda usecwd=True: "")

    plugin = DiscordPlugin()

    with pytest.raises(RuntimeError, match="CAO_DISCORD_WEBHOOK_URL"):
        await plugin.setup()

    await plugin.teardown()


@pytest.mark.asyncio
async def test_on_post_send_message_posts_webhook_payload_with_tmux_window_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The hook should send a webhook payload with the resolved display name."""

    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "json": json.loads(request.content.decode("utf-8")),
            }
        )
        return httpx.Response(204)

    monkeypatch.setenv("CAO_DISCORD_WEBHOOK_URL", "https://discord.example/happy-path")
    monkeypatch.delenv("CAO_DISCORD_TIMEOUT_SECONDS", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_discord.plugin.find_dotenv", lambda usecwd=True: "")
    monkeypatch.setattr(
        "cao_discord.plugin.get_terminal_metadata",
        lambda terminal_id: {"tmux_window": "coder-a1b2", "id": terminal_id},
    )

    plugin = DiscordPlugin()
    await plugin.setup()
    await _replace_client_with_mock_transport(plugin, httpx.MockTransport(handler))

    result = await plugin.on_post_send_message(
        PostSendMessageEvent(
            sender="abc12345",
            receiver="def67890",
            message="hello",
            orchestration_type="send_message",
        )
    )

    assert result is None
    assert requests == [
        {
            "method": "POST",
            "url": "https://discord.example/happy-path",
            "json": {"username": "coder-a1b2", "content": "hello"},
        }
    ]

    await plugin.teardown()


@pytest.mark.asyncio
async def test_on_post_send_message_falls_back_to_terminal_id_when_metadata_is_missing_or_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Missing or empty tmux window metadata should fall back to the sender id."""

    requests: list[dict[str, str]] = []
    metadata_values = iter([None, {}, {"tmux_window": "", "id": "abc12345"}])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(204)

    monkeypatch.setenv("CAO_DISCORD_WEBHOOK_URL", "https://discord.example/fallback")
    monkeypatch.delenv("CAO_DISCORD_TIMEOUT_SECONDS", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_discord.plugin.find_dotenv", lambda usecwd=True: "")
    monkeypatch.setattr(
        "cao_discord.plugin.get_terminal_metadata",
        lambda terminal_id: next(metadata_values),
    )

    plugin = DiscordPlugin()
    await plugin.setup()
    await _replace_client_with_mock_transport(plugin, httpx.MockTransport(handler))

    for message in ("first", "second", "third"):
        result = await plugin.on_post_send_message(
            PostSendMessageEvent(
                sender="abc12345",
                receiver="def67890",
                message=message,
                orchestration_type="send_message",
            )
        )
        assert result is None

    assert requests == [
        {"username": "abc12345", "content": "first"},
        {"username": "abc12345", "content": "second"},
        {"username": "abc12345", "content": "third"},
    ]

    await plugin.teardown()


@pytest.mark.asyncio
async def test_on_post_send_message_logs_warning_for_http_500_without_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """HTTP 5xx responses should log a warning and not escape the hook."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="webhook temporarily broken")

    monkeypatch.setenv("CAO_DISCORD_WEBHOOK_URL", "https://discord.example/server-error")
    monkeypatch.delenv("CAO_DISCORD_TIMEOUT_SECONDS", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_discord.plugin.find_dotenv", lambda usecwd=True: "")
    monkeypatch.setattr(
        "cao_discord.plugin.get_terminal_metadata",
        lambda terminal_id: {"tmux_window": "coder-a1b2", "id": terminal_id},
    )

    plugin = DiscordPlugin()
    await plugin.setup()
    await _replace_client_with_mock_transport(plugin, httpx.MockTransport(handler))

    with caplog.at_level("WARNING", logger="cao_discord.plugin"):
        result = await plugin.on_post_send_message(
            PostSendMessageEvent(
                sender="abc12345",
                receiver="def67890",
                message="hello",
                orchestration_type="send_message",
            )
        )

    assert result is None
    assert "Discord webhook POST failed: 500 webhook temporarily broken" in caplog.text

    await plugin.teardown()


@pytest.mark.asyncio
async def test_on_post_send_message_logs_warning_for_httpx_error_without_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Transport errors should log a warning and not escape the hook."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setenv("CAO_DISCORD_WEBHOOK_URL", "https://discord.example/connect-error")
    monkeypatch.delenv("CAO_DISCORD_TIMEOUT_SECONDS", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_discord.plugin.find_dotenv", lambda usecwd=True: "")
    monkeypatch.setattr(
        "cao_discord.plugin.get_terminal_metadata",
        lambda terminal_id: {"tmux_window": "coder-a1b2", "id": terminal_id},
    )

    plugin = DiscordPlugin()
    await plugin.setup()
    await _replace_client_with_mock_transport(plugin, httpx.MockTransport(handler))

    with caplog.at_level("WARNING", logger="cao_discord.plugin"):
        result = await plugin.on_post_send_message(
            PostSendMessageEvent(
                sender="abc12345",
                receiver="def67890",
                message="hello",
                orchestration_type="send_message",
            )
        )

    assert result is None
    assert "Discord webhook POST raised: boom" in caplog.text

    await plugin.teardown()


@pytest.mark.asyncio
async def test_teardown_closes_real_client_after_successful_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Teardown should close a real AsyncClient instance."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    monkeypatch.setenv("CAO_DISCORD_WEBHOOK_URL", "https://discord.example/teardown")
    monkeypatch.delenv("CAO_DISCORD_TIMEOUT_SECONDS", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_discord.plugin.find_dotenv", lambda usecwd=True: "")

    plugin = DiscordPlugin()
    await plugin.setup()
    await _replace_client_with_mock_transport(plugin, httpx.MockTransport(handler))

    await plugin.teardown()

    assert plugin._client.is_closed is True
