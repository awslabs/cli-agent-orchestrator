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

import pytest

from cli_agent_orchestrator.security.principal import LOCAL_PRINCIPAL

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
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "caller_id": "beefbeef",
                },
            )
        # Positively 403, not merely "not a 404": the earlier form was satisfied
        # by a 422 validation error, which is not an authorization answer at all.
        assert resp.status_code == 403
        assert "owned by another principal" in resp.json()["detail"]

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
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "caller_id": "beefbeef",
                },
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
            # The principal comes from a FastAPI dependency resolved at request
            # time, so patching the module attribute does nothing — the client's
            # real principal is LOCAL_PRINCIPAL. Make the caller's recorded owner
            # match THAT, which is what the ordinary path looks like.
            patch(
                "cli_agent_orchestrator.api.main.caller_owner_id",
                return_value=LOCAL_PRINCIPAL.id,
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
        assert resp.status_code != 403


class TestAnUnreadableOwnerIsNotTreatedAsUnowned:
    """A failed read must not pass the ownership check.

    ``caller_owner_id`` used to swallow a database failure and return ``None``,
    which is indistinguishable from a confirmed-unowned caller — so the 403 above
    was skipped and the worker was attributed to the REQUEST principal instead.
    With auth on, a remote caller's placement can come from the in-memory registry
    without any row read succeeding, so this was reachable (Copilot review on #802).

    Unreadable is now its own answer, and the route turns it into a retryable 503
    rather than proceeding on a guess.
    """

    def test_an_unreadable_owner_is_503_not_a_silent_pass(self, client):
        from cli_agent_orchestrator.services.agent_step import OwnerUnavailableError

        with (
            patch("cli_agent_orchestrator.api.main.is_auth_enabled", return_value=True),
            patch(
                "cli_agent_orchestrator.api.main.caller_owner_id",
                side_effect=OwnerUnavailableError("database unreachable"),
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
        assert resp.status_code == 503
        assert "cannot read the owner" in resp.json()["detail"]

    def test_the_helper_raises_rather_than_returning_none(self):
        """At the source: a read failure is not an answer."""
        from cli_agent_orchestrator.services import agent_step

        with patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            side_effect=RuntimeError("db down"),
        ):
            with pytest.raises(agent_step.OwnerUnavailableError):
                agent_step.caller_owner_id("beefbeef")

    def test_a_confirmed_unowned_caller_still_returns_none(self):
        """The distinction must not collapse the other way either."""
        from cli_agent_orchestrator.services import agent_step

        with patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value={"owner": None},
        ):
            assert agent_step.caller_owner_id("beefbeef") is None
