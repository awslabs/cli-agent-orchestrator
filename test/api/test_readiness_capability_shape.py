"""Which providers each capability list may name, and why they differ.

``/managed-launch/capabilities`` answers two different questions with two
different keys, and the difference is load-bearing:

* ``readiness_providers`` — providers the **v1 bridged** surface has a
  readiness adapter for. A consumer gates a *bridged* launch on it.
* ``native_tui.providers`` — providers with a **v2 native** adapter, read
  ANDed with ``v2_launchable_execution_modes`` from one capability read.

Claude has the second and not the first: ``_READINESS_RECEIPT_KINDS`` has
no ``claude_code`` entry and ``launch()`` refuses it outright, while the
native SessionStart path is real and published. Adding ``claude_code`` to
``readiness_providers`` would therefore let a bridged Claude launch pass a
pre-reservation gate on the strength of an adapter that does not exist,
and be refused at preflight *after* a reservation was created — the same
late-refusal shape these campaigns keep removing, moved to a new provider.

So the fix for the drift is to stop writing the list by hand, not to widen
it. These tests pin both halves: derived-from-the-adapters, and each list
naming only what it can actually do.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services import managed_launch, managed_launch_v2

client = TestClient(app, base_url="http://localhost")


def _capabilities() -> dict:
    response = client.get("/managed-launch/capabilities")
    assert response.status_code == 200
    return response.json()


class TestReadinessProvidersIsDerivedFromItsAdapters:
    def test_the_published_list_is_exactly_the_v1_adapter_map(self):
        """No second source of truth.

        The pair used to be written out by hand in both the service and
        the API, and the copies drifted. Derivation is what makes the
        advertisement unable to outrun the adapters.
        """
        assert _capabilities()["readiness_providers"] == sorted(
            managed_launch._READINESS_RECEIPT_KINDS
        )

    def test_every_advertised_provider_can_actually_be_launched_bridged(self):
        """The gate behind the advertisement, exercised.

        ``launch()`` refuses any provider without a v1 readiness adapter,
        so a provider named here that it would refuse is an advertisement
        with nothing behind it.
        """
        for provider in _capabilities()["readiness_providers"]:
            assert provider in managed_launch.READINESS_PROVIDERS

    def test_claude_is_absent_because_v1_has_no_adapter_for_it(self):
        """Able to fail in the direction that matters.

        If someone adds ``claude_code`` to the published list without
        adding a v1 readiness receipt kind, this fails — which is the
        exact regression that would let a bridged Claude launch through a
        gate and into a doomed reservation.
        """
        capabilities = _capabilities()
        assert "claude_code" not in managed_launch._READINESS_RECEIPT_KINDS
        assert "claude_code" not in capabilities["readiness_providers"]


class TestNativeReadinessIsPublishedWhereNativeCallersRead:
    def test_claude_native_readiness_is_advertised_under_native_tui(self):
        """The other half of the same question, answered truthfully.

        Claude readiness exists — it is the SessionStart hook naming the
        exact session id — and this is the key a native caller reads. The
        v1 list being silent about Claude is not the server withholding a
        capability; it is two lists answering two questions.
        """
        capabilities = _capabilities()
        native = capabilities["native_tui"]["providers"]
        assert native["claude_code"]["supported"] is True
        assert native["claude_code"]["readiness_receipt_kind"]
        assert "native_tui" in capabilities["v2_launchable_execution_modes"]

    def test_the_native_block_is_derived_too(self):
        assert sorted(_capabilities()["native_tui"]["providers"]) == sorted(
            managed_launch_v2.NATIVE_TUI_PROVIDERS
        )

    def test_the_two_lists_are_not_the_same_question(self):
        """Pinned so a later 'simplification' has to argue with a test.

        Merging them reads as tidying. It would make one list wrong for
        whichever caller read it next, and the wrongness only shows up
        after a reservation exists.
        """
        capabilities = _capabilities()
        assert set(capabilities["readiness_providers"]) != set(
            capabilities["native_tui"]["providers"]
        )
