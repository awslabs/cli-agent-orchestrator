"""``caller_id`` is a request parameter, not authorization (#802).

The session-terminal route inherits two things from the terminal named by
``caller_id``: the ``owner`` recorded on the new worker, and — when that caller is
remote — the runtime the worker is placed on. Both were taken from whatever id the
request supplied.

That let a caller holding write scope name ANOTHER principal's terminal and get a
worker attributed to that principal, placed beside its terminal. The owner column
is what the delivery-time gate and the revocation decision read, so inheriting it
from an unvalidated id walked straight through the boundary it exists to carry
(Copilot review on #802).

The fix is the same binding the inbox route makes for ``sender_id``: with auth on,
a caller terminal owned by a different principal is refused rather than inherited.
With auth off there is no identity to bind to and the check is inert — the same
acknowledged posture, recorded here so the gap is not mistaken for coverage.
"""

from unittest.mock import patch

OTHER_OWNER = "auth0|someone-else"


class TestACallerOwnedByAnotherPrincipalIsRefused:
    def test_naming_another_owners_terminal_is_403(self, client):
        """The load-bearing case: attribution must not be inheritable by id."""
        with (
            patch("cli_agent_orchestrator.api.main.is_auth_enabled", return_value=True),
            patch(
                "cli_agent_orchestrator.api.main.caller_owner_id",
                return_value=OTHER_OWNER,
            ),
        ):
            resp = client.post(
                "/sessions/cao-somesession/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "caller_id": "beefbeef",
                },
            )
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert "owned by another principal" in detail
        assert "beefbeef" in detail

    def test_the_error_names_ownership_not_a_missing_terminal(self, client):
        """A 404 would tell a prober the id does not exist; this is an authz answer."""
        with (
            patch("cli_agent_orchestrator.api.main.is_auth_enabled", return_value=True),
            patch(
                "cli_agent_orchestrator.api.main.caller_owner_id",
                return_value=OTHER_OWNER,
            ),
        ):
            resp = client.post(
                "/sessions/cao-somesession/terminals",
                params={"provider": "kiro_cli", "caller_id": "beefbeef"},
            )
        assert resp.status_code not in (404, 500)

    def test_an_unowned_caller_is_not_refused(self, client):
        """Unowned predates ownership or is operator-created; it confers nothing.

        Refusing it would break every terminal created before the owner column
        existed, and it cannot be used to impersonate anyone — there is no
        principal on the row to inherit.
        """
        with (
            patch("cli_agent_orchestrator.api.main.is_auth_enabled", return_value=True),
            patch("cli_agent_orchestrator.api.main.caller_owner_id", return_value=None),
            patch(
                "cli_agent_orchestrator.api.main.runtime_registry.is_remote",
                return_value=False,
            ),
        ):
            resp = client.post(
                "/sessions/cao-somesession/terminals",
                params={"provider": "kiro_cli", "caller_id": "beefbeef"},
            )
        assert resp.status_code != 403

    def test_the_check_does_not_fire_when_the_owner_matches(self, client):
        """A caller acting for its own terminal is the ordinary path."""
        with (
            patch("cli_agent_orchestrator.api.main.is_auth_enabled", return_value=True),
            patch(
                "cli_agent_orchestrator.api.main.runtime_registry.is_remote",
                return_value=False,
            ),
            patch("cli_agent_orchestrator.api.main.get_current_principal") as principal,
            patch("cli_agent_orchestrator.api.main.caller_owner_id") as owner,
        ):
            owner.return_value = "local"
            principal.return_value = type("P", (), {"id": "local"})()
            resp = client.post(
                "/sessions/cao-somesession/terminals",
                params={"provider": "kiro_cli", "caller_id": "beefbeef"},
            )
        assert resp.status_code != 403
