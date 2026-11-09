"""Whether code handed to CAO at runtime may run on this host (#745).

With ``terminal.backend=remote`` every agent runs on a cao-runner, and
cao-server runs none itself. Python workflow scripts and flow pre-scripts are
not agents: they are code given to CAO through its API, CLI or flow files, and
they would run as subprocesses of whatever process executes them, on the
server's host, beside its database and credentials. Until they can run on a
runner they are refused there, at the point where they would start: storing a
definition runs nothing and stays allowed.

Out of scope, on purpose: installed Python packages, including the
``cao.plugins`` and ``cao.deciders`` entry points. They are server extensions,
installed into cao-server's own environment by whoever deploys it, and trusted
like CAO itself; CAO's own built-in plugins and decider load the same way.
"""

from typing import Optional


class ServerHostCodeRefused(Exception):
    """User code would run on the cao-server host while terminals run on runners.

    Deliberately not a ``ValueError``: callers map that to "not found" or "bad
    request", and this is neither. The API answers it with 409.
    """


def runs_terminals_elsewhere() -> bool:
    """Whether the active terminal backend runs terminals off this host."""
    from cli_agent_orchestrator.backends.registry import get_backend

    return getattr(get_backend(), "resolves_paths_locally", True) is False


def refuse_server_host_code(what: str) -> None:
    """Raise :class:`ServerHostCodeRefused` for ``what`` under the remote backend."""
    if runs_terminals_elsewhere():
        raise ServerHostCodeRefused(
            f"{what} refused: with terminal.backend=remote, agents run on runners and "
            "cao-server runs no workflow scripts or flow pre-scripts on its own host, "
            "which this would (#745)"
        )


def remote_auth_refusal() -> Optional[str]:
    """Why cao-server must not start, or None: API auth with the remote backend.

    Agents on runners call this server's API for assign, handoff and
    send_message, and with API auth on they would need a credential. Phase 1 of
    #745 has none to give them short of the full API token, which every agent
    on the runner could read. So, rather than half-work (agents whose
    orchestration calls fail) or hand that token out, the combination is
    refused: with terminal.backend=remote, no authenticated API use is possible
    in this phase, external clients included. Terminal-scoped credentials are
    the follow-up.
    """
    from cli_agent_orchestrator.security.auth import is_auth_enabled

    if not (runs_terminals_elsewhere() and is_auth_enabled()):
        return None
    return (
        "refusing to start: terminal.backend=remote does not support API authentication yet "
        "(CAO_AUTH_LOCAL_TOKEN or an identity provider is configured). Agents on runners "
        "would have no credential for their orchestration calls (#745). Unset the API auth "
        "settings, or use a local terminal backend."
    )
