"""``cao doctor`` — tiered health check for a CAO install.

Static tier (``cao doctor``): no agents spawned, no token cost. Reports whether
the server is reachable, which preferred providers have their CLI binary
installed, which agent profiles are discoverable, and the effective server
timeouts.

Live tier (``cao doctor --live``): for each installed preferred provider, spawn
a throwaway session via the API, assert it reaches IDLE within
``provider_init_timeout``, report time-to-IDLE, then exit + delete it. This
spawns REAL agents and therefore costs tokens — it is the manual twin of the
opt-in e2e provider smoke test.
"""

import time

import click
import requests

from cli_agent_orchestrator.constants import (
    API_BASE_URL,
    PREFERRED_PROVIDERS,
    SERVER_HOST,
    SERVER_PORT,
)
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.providers import provider_binary, provider_binary_installed
from cli_agent_orchestrator.utils.server_process import health, read_pidfile

_OK = click.style("✓", fg="green")
_BAD = click.style("✗", fg="red")


def _mark(ok: bool) -> str:
    return _OK if ok else _BAD


@click.command()
@click.option(
    "--live",
    is_flag=True,
    help="Spawn a throwaway agent per installed provider to confirm it reaches IDLE. "
    "Costs tokens (real agents).",
)
@click.option(
    "--provider",
    "only_provider",
    default=None,
    help="Limit --live checks to a single provider.",
)
def doctor(live, only_provider):
    """Diagnose a CAO install (static checks; --live spawns real agents)."""
    _static_report()
    if live:
        _live_report(only_provider)


def _static_report() -> None:
    click.echo(click.style("CAO doctor — static checks", bold=True))
    click.echo("")

    # 1. Server reachability
    info = health()
    pid = read_pidfile()
    if info is not None:
        pid_str = f" (pid {pid})" if pid else ""
        click.echo(f"{_mark(True)} server: running on {SERVER_HOST}:{SERVER_PORT}{pid_str}")
        components = info.get("components") or {}
        for name, state in components.items():
            click.echo(f"    {_mark(state == 'ok')} {name}: {state}")
    else:
        click.echo(f"{_mark(False)} server: not reachable on {SERVER_HOST}:{SERVER_PORT}")
        click.echo("    Start it with: cao server start")

    # 2. Preferred providers — binary install status
    click.echo("")
    click.echo("Providers (preferred order):")
    for name in PREFERRED_PROVIDERS:
        installed = provider_binary_installed(name)
        binary = provider_binary(name) or "?"
        click.echo(f"    {_mark(installed)} {name} ({binary})")

    # 3. Discoverable agent profiles
    click.echo("")
    profiles = _list_profiles(info is not None)
    if profiles is None:
        click.echo(f"{_mark(False)} profiles: could not query (server unreachable)")
    else:
        click.echo(f"{_mark(bool(profiles))} profiles: {len(profiles)} discoverable")
        for p in profiles[:10]:
            click.echo(f"      - {p.get('name')} [{p.get('source', '?')}]")
        if len(profiles) > 10:
            click.echo(f"      ... and {len(profiles) - 10} more")

    # 4. Effective server timeouts
    click.echo("")
    settings = get_server_settings()
    click.echo("Effective server settings:")
    click.echo(f"      mcp_request_timeout:    {settings['mcp_request_timeout']}s")
    click.echo(f"      provider_init_timeout:  {settings['provider_init_timeout']}s")
    click.echo(
        f"      startup_prompt_handler_timeout: {settings['startup_prompt_handler_timeout']}s"
    )


def _list_profiles(server_up: bool):
    """Return discoverable profiles via the API, or None on failure."""
    if not server_up:
        return None
    try:
        resp = requests.get(f"{API_BASE_URL}/agents/profiles", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException:
        return None


def _live_report(only_provider) -> None:
    click.echo("")
    click.echo(
        click.style("CAO doctor — live checks (spawns real agents, costs tokens)", bold=True)
    )

    if not health():
        raise click.ClickException(
            "Server not reachable — cannot run --live checks. Start it with: cao server start"
        )

    targets = [only_provider] if only_provider else list(PREFERRED_PROVIDERS)
    init_timeout = get_server_settings()["provider_init_timeout"]

    for provider in targets:
        if not provider_binary_installed(provider):
            click.echo(f"{_mark(False)} {provider}: binary not installed — skipping")
            continue
        click.echo(f"  {provider}: spawning throwaway session...")
        ok, elapsed, detail = _probe_provider(provider, init_timeout)
        if ok:
            click.echo(f"{_mark(True)} {provider}: reached IDLE in {elapsed:.1f}s")
        else:
            click.echo(f"{_mark(False)} {provider}: {detail}")


def _probe_provider(provider: str, init_timeout: float):
    """Create a throwaway session, wait for IDLE, then clean up.

    Returns (ok, elapsed_seconds, detail).
    """
    session_name = f"doctor-{provider}-{int(time.time())}"
    terminal_id = None
    actual_session = session_name
    start = time.time()
    try:
        resp = requests.post(
            f"{API_BASE_URL}/sessions",
            params={
                "provider": provider,
                "agent_profile": "code_supervisor",
                "session_name": session_name,
            },
            timeout=init_timeout + 30,
        )
        if resp.status_code not in (200, 201):
            return False, 0.0, f"session creation failed: {resp.status_code} {resp.text[:200]}"
        data = resp.json()
        terminal_id = data["id"]
        actual_session = data["session_name"]

        deadline = time.time() + init_timeout
        while time.time() < deadline:
            status = _terminal_status(terminal_id)
            if status == "idle":
                return True, time.time() - start, ""
            if status == "error":
                return False, time.time() - start, "terminal reported ERROR"
            time.sleep(2)
        return False, time.time() - start, f"did not reach IDLE within {init_timeout:.0f}s"
    except requests.exceptions.RequestException as e:
        return False, time.time() - start, f"request failed: {e}"
    finally:
        if terminal_id is not None:
            try:
                requests.post(f"{API_BASE_URL}/terminals/{terminal_id}/exit", timeout=10)
            except requests.exceptions.RequestException:
                pass
            time.sleep(1)
        try:
            requests.delete(f"{API_BASE_URL}/sessions/{actual_session}", timeout=10)
        except requests.exceptions.RequestException:
            pass


def _terminal_status(terminal_id: str) -> str:
    try:
        resp = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}", timeout=10)
        if resp.status_code != 200:
            return "unknown"
        return str(resp.json().get("status", "unknown"))
    except requests.exceptions.RequestException:
        return "unknown"
