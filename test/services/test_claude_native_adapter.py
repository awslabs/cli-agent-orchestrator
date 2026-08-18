"""The managed Claude native-TUI adapter: identity, readiness, delivery.

Claude's native path is not Kimi's with a different binary name, and the
tests that matter are the ones that pin the differences.

*Identity is chosen, not discovered.* A canonical uuid is minted before
any provider I/O and handed to the TUI as ``--session-id``; only a
recovery uses ``--resume``. Both flags on the installed build have a
hazard the tests below encode: ``--resume``'s value is *optional*, so a
resume that loses its id opens an interactive picker and waits instead of
failing — a managed pane sitting at a menu, indistinguishable from a slow
start.

*Readiness is the provider's own SessionStart hook naming that exact
uuid.* Not the pane existing, not elapsed time, not a caller's boolean,
and not a rendered composer — a composer belongs to whatever session
Claude actually opened, which is precisely what goes wrong when a resume
falls back to a picker.

*The composer facts are this build's.* ``C-j`` is pinned from the
installed bundle's own hint text; the alternatives it also mentions are
terminal keybindings installed by ``/terminal-setup`` and cannot be
assumed in a managed pane.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import claude_native_control as control
from cli_agent_orchestrator.services import claude_native_launch as launch
from cli_agent_orchestrator.services import claude_native_readiness as readiness
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import native_attachment as na
from cli_agent_orchestrator.services import native_tui_launch as ntl
from cli_agent_orchestrator.services import provider_contracts

PINNED = "2.1.220"


class TestChosenIdentity:
    def test_a_launch_assigns_the_minted_uuid(self):
        session_id = launch.mint_session_id()
        argv = launch.build_launch_argv(session_id=session_id, extra_args=["--model", "opus"])
        assert argv == ["claude", "--session-id", session_id, "--model", "opus"]
        assert launch.launches_exactly(argv, session_id)
        # A launch is not a resume, and the two must not be confusable:
        # only one of them is lawful for a session that does not exist yet.
        assert not launch.resumes_exactly(argv, session_id)

    def test_a_recovery_names_the_same_uuid(self):
        session_id = launch.mint_session_id()
        argv = launch.build_resume_argv(session_id=session_id)
        assert argv == ["claude", "--resume", session_id]
        assert launch.resumes_exactly(argv, session_id)
        assert launch.binds_exactly(argv, session_id)

    def test_a_resume_without_its_value_is_not_a_bound_session(self):
        """The picker hazard, stated as a check.

        ``-r, --resume [value]`` takes an *optional* argument on the
        installed build: with the value missing it opens an interactive
        picker rather than failing. A check that stopped at "the flag is
        present" would call that menu a bound session.
        """
        session_id = launch.mint_session_id()
        assert not launch.resumes_exactly(["claude", "--resume"], session_id)
        assert not launch.binds_exactly(["claude", "--resume"], session_id)

    def test_the_short_resume_spelling_is_recognised(self):
        """``-r`` is the same option and must not slip past detection."""
        session_id = launch.mint_session_id()
        assert launch.resumes_exactly(["claude", "-r", session_id], session_id)
        # ...and a second identity option, however spelled, means the
        # effective session is decided by Claude's option precedence
        # rather than by this argv.
        assert not launch.binds_exactly(
            ["claude", "--session-id", session_id, "-r", session_id], session_id
        )

    @pytest.mark.parametrize(
        "forbidden", ["--continue", "-c", "--fork-session", "--from-pr", "--ephemeral"]
    )
    def test_recency_and_indirect_forms_are_refused(self, forbidden):
        """None of these names a session; each resolves one."""
        session_id = launch.mint_session_id()
        with pytest.raises(launch.ClaudeNativeLaunchError):
            launch.build_launch_argv(session_id=session_id, extra_args=[forbidden])
        assert not launch.binds_exactly(["claude", "--resume", session_id, forbidden], session_id)

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-uuid",
            "6D1F0E34-0000-4000-8000-00000000ABCD",
            "",
            "{6d1f0e34-0000-4000-8000-00000000abcd}",
        ],
    )
    def test_only_a_canonical_lowercase_uuid_is_accepted(self, bad):
        """Parseable is not enough — the recorded id is compared as a string.

        The launch argv, the resume argv, the hook payload and the stored
        identity all have to compare equal as text; an uppercase or
        brace-wrapped spelling parses to the same uuid and fails every one
        of those comparisons.
        """
        with pytest.raises(launch.ClaudeNativeLaunchError):
            launch.build_launch_argv(session_id=bad)

    def test_the_resume_form_is_validated_by_the_shared_contract(self):
        """The builder cannot emit a form the contract would reject."""
        session_id = launch.mint_session_id()
        argv = launch.build_resume_argv(session_id=session_id)
        form = provider_contracts.validate_resume_argv(provider_contracts.PROVIDER_CLAUDE, argv[1:])
        assert form.native_id == session_id


class TestTheLaunchIntentTheStoreWillAccept:
    """The intent the v2 Claude branch hands to the attachment store.

    Worth its own test because the failure it guards against is silent
    until launch: an intent the store refuses produces a reservation that
    blocks at preflight with no pane, and nothing earlier in the pipeline
    would have noticed.
    """

    def test_the_claude_launch_intent_is_one_the_attachment_store_accepts(self):
        intent = v2._claude_bootstrap_intent(
            {
                "native_session_id": launch.mint_session_id(),
                "id_source": "cli_session_id",
                "provider_version": PINNED,
                "working_directory": "/tmp",
            },
            reservation_id="reservation-1",
        )

        assert intent["schema"] == na.INTENT_SCHEMA
        assert intent["acquisition_method"] == na.ACQUISITION_CHOSEN_SESSION_ID

    def test_the_claude_specific_facts_survive_inside_the_receipt(self):
        """The generic intent shape must not cost the provider's own facts."""
        session_id = launch.mint_session_id()

        intent = v2._claude_bootstrap_intent(
            {
                "native_session_id": session_id,
                "id_source": "cli_session_id",
                "provider_version": PINNED,
                "working_directory": "/tmp",
            },
            reservation_id="reservation-2",
        )

        receipt = intent["acquisition_receipt"]
        assert receipt["provider"] == "claude_code"
        assert receipt["native_session_id"] == session_id
        assert receipt["provider_version"] == PINNED
        assert receipt["task_bytes_submitted"] is False
        assert "reservation-2" in intent["note"]

    def test_a_chosen_id_is_not_recorded_as_a_bootstrap_or_a_resume(self):
        """Both of those describe acquiring an id the provider already had.

        A bootstrap reads one back out of a transport and a resume names one
        that exists; recording either for an id minted here would make the
        journaled receipt say the session pre-dated its own launch.
        """
        assert na.ACQUISITION_CHOSEN_SESSION_ID not in (
            na.ACQUISITION_ACP_BOOTSTRAP,
            na.ACQUISITION_RESUME,
        )
        assert na.ACQUISITION_CHOSEN_SESSION_ID in na.ACQUISITION_METHODS


class TestPerProviderArgvDispatch:
    """Which argv a native launch builds is decided by provider, not shape.

    The builder and its "does this bind exactly that session?" checker are
    registered as a pair on purpose: a builder verified against another
    provider's rules would construct a correct argv, pass, and mean
    nothing.
    """

    def test_claude_dispatches_to_its_own_builder_for_each_launch_kind(self):
        session_id = launch.mint_session_id()

        new = ntl._claude_argv(
            session_id=session_id,
            binary="/usr/local/bin/claude",
            extra_args=None,
            launch_kind=ntl.LAUNCH_KIND_NEW,
        )
        resumed = ntl._claude_argv(
            session_id=session_id,
            binary="/usr/local/bin/claude",
            extra_args=None,
            launch_kind=ntl.LAUNCH_KIND_RESUME,
        )

        assert new == ["/usr/local/bin/claude", "--session-id", session_id]
        assert resumed == ["/usr/local/bin/claude", "--resume", session_id]

    def test_a_launch_kind_is_named_because_it_cannot_be_recovered(self):
        """ "Is this the first launch?" is a fact only the caller holds.

        Guessing it would mean sometimes resuming a session that was never
        started — which on this build does not fail, it opens a picker.
        """
        assert ntl.LAUNCH_KINDS == (ntl.LAUNCH_KIND_NEW, ntl.LAUNCH_KIND_RESUME)

    def test_a_claude_builder_error_surfaces_as_a_launch_refusal(self):
        with pytest.raises(ntl.NativeLaunchInvalid):
            ntl._claude_argv(
                session_id="not-a-uuid",
                binary="/usr/local/bin/claude",
                extra_args=None,
                launch_kind=ntl.LAUNCH_KIND_NEW,
            )

    def test_kimi_has_no_lawful_new_launch(self):
        """Its session is minted by the ACP bootstrap before the TUI starts.

        The opposite of Claude's: there the id is chosen and a first launch
        creates the session; here the id is discovered and only a resume
        exists.
        """
        with pytest.raises(ntl.NativeLaunchInvalid, match="only lawful launch form is a resume"):
            ntl._kimi_argv(
                session_id="session_9f21ac30",
                binary="/usr/local/bin/kimi",
                extra_args=None,
                launch_kind=ntl.LAUNCH_KIND_NEW,
            )

    def test_kimi_builder_errors_surface_as_launch_refusals(self):
        with pytest.raises(ntl.NativeLaunchInvalid):
            ntl._kimi_argv(
                session_id="",
                binary="/usr/local/bin/kimi",
                extra_args=None,
                launch_kind=ntl.LAUNCH_KIND_RESUME,
            )

    def test_a_provider_with_no_adapter_is_refused_rather_than_defaulted(self):
        with pytest.raises(ntl.NativeLaunchInvalid, match="no native-TUI argv binding"):
            ntl._binder("no_such_provider")

    def test_the_supported_set_is_read_from_the_registered_binders(self):
        assert ntl.SUPPORTED_NATIVE_PROVIDERS == {
            "codex",
            "kimi_cli",
            "claude_code",
            "muse_cli",
        }
        for provider in ntl.SUPPORTED_NATIVE_PROVIDERS:
            binder = ntl._binder(provider)
            assert callable(binder["build"]) and callable(binder["binds_exactly"])


class TestLaunchRefusesBeforeItClaims:
    """Everything checkable is checked while a refusal still costs nothing.

    Each of these runs before ``declare``, so the cost of being wrong is a
    typed error rather than a claimed session with no pane behind it.
    """

    @staticmethod
    def _binary(tmp_path):
        path = tmp_path / "claude"
        path.write_bytes(b"#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        real = os.path.realpath(str(path))
        return real, hashlib.sha256(Path(real).read_bytes()).hexdigest()

    def _start(self, tmp_path, **overrides):
        binary, digest = self._binary(tmp_path)
        kwargs = {
            "provider": "claude_code",
            "native_session_id": launch.mint_session_id(),
            "terminal_id": "terminal_4d7b",
            "generation": "gen_1c0e",
            "execution_mode": "native_tui",
            "intent": {"schema": "unused-because-this-never-reaches-declare"},
            "binary": binary,
            "binary_sha256": digest,
            "working_directory": os.path.realpath(str(tmp_path)),
            "transport": object(),
            "launch_kind": ntl.LAUNCH_KIND_NEW,
        }
        kwargs.update(overrides)
        return ntl.start(**kwargs)

    def test_an_acp_caller_reaching_the_native_branch_is_rejected(self, tmp_path):
        """The two modes are separate branches and never fall back."""
        with pytest.raises(ntl.NativeLaunchInvalid, match="refuses execution_mode"):
            self._start(tmp_path, execution_mode="acp")

    def test_an_unknown_launch_kind_is_refused(self, tmp_path):
        with pytest.raises(ntl.NativeLaunchInvalid, match="launch_kind must be one of"):
            self._start(tmp_path, launch_kind="whatever-seems-right")

    def test_an_ambient_path_binary_is_refused(self, tmp_path):
        """Which provider ran would depend on the pane's inherited PATH."""
        with pytest.raises(ntl.NativeLaunchInvalid, match="canonical absolute path"):
            self._start(tmp_path, binary="claude")

    def test_a_digest_that_does_not_match_the_file_is_refused(self, tmp_path):
        with pytest.raises(ntl.NativeLaunchInvalid):
            self._start(tmp_path, binary_sha256="0" * 64)

    def test_a_hand_written_intent_cannot_claim_a_session(self, tmp_path, isolated_memory_db):
        """The attachment store accepts only an intent it built itself.

        The intent is the set of obligations that module validates, so a
        dict carrying its own schema would be a caller asserting them
        instead of having them checked.
        """
        with pytest.raises(ntl.NativeLaunchInvalid, match="acquire_intent"):
            self._start(tmp_path)


class TestSessionStartReadiness:
    def _hook(self, tmp_path: Path):
        return readiness.prepare(tmp_path, "abc12345", "gen-1")

    def test_the_settings_install_a_sessionstart_hook_writing_to_this_generation(self, tmp_path):
        prepared = self._hook(tmp_path)
        settings = prepared["settings"]
        assert list(settings["hooks"]) == ["SessionStart"]
        command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert str(prepared["readiness_path"]) in command
        # Generation-private: a replacement launch must not be able to
        # read its predecessor's readiness and conclude it is itself
        # ready. Recovery exists to replace a generation whose provider
        # may still be running, so a shared path would let the corpse
        # vouch for its successor.
        other = readiness.readiness_path(tmp_path, "abc12345", "gen-2")
        assert other != prepared["readiness_path"]

    def test_the_matching_session_id_is_what_ends_the_wait(self, tmp_path):
        prepared = self._hook(tmp_path)
        session_id = launch.mint_session_id()
        prepared["readiness_path"].write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "transcript_path": "/tmp/t.jsonl",
                    "source": "startup",
                }
            )
            + "\n"
        )

        record = readiness.await_session_start(
            prepared["readiness_path"], session_id, timeout=1.0, poll=0.01
        )

        assert record["schema"] == readiness.READINESS_SCHEMA
        assert record["native_session_id"] == session_id
        assert record["hook_event"] == "SessionStart"
        # Corroboration only; the match was made on the id.
        assert record["transcript_path"] == "/tmp/t.jsonl"

    def test_a_different_session_starting_is_not_partial_credit(self, tmp_path):
        """The single most important thing this can catch.

        A SessionStart for another id means Claude opened a session other
        than the one it was told to — what a resume falling back to its
        picker looks like from outside. It must not satisfy readiness, and
        the refusal must say so rather than reporting silence.
        """
        prepared = self._hook(tmp_path)
        ours = launch.mint_session_id()
        theirs = launch.mint_session_id()
        prepared["readiness_path"].write_text(json.dumps({"session_id": theirs}) + "\n")

        with pytest.raises(readiness.ClaudeNativeNotReady) as caught:
            readiness.await_session_start(prepared["readiness_path"], ours, timeout=0.2, poll=0.01)

        assert theirs in str(caught.value)
        assert "other sessions started instead" in str(caught.value)

    def test_no_hook_at_all_is_reported_as_absent_evidence(self, tmp_path):
        prepared = self._hook(tmp_path)
        with pytest.raises(readiness.ClaudeNativeNotReady) as caught:
            readiness.await_session_start(
                prepared["readiness_path"], launch.mint_session_id(), timeout=0.2, poll=0.01
            )
        assert "no SessionStart hook was recorded at all" in str(caught.value)
        # Stated because it is the property that makes refusing safe.
        assert "No task bytes were submitted" in str(caught.value)

    def test_a_half_written_line_is_not_yet_evidence(self, tmp_path):
        """The hook may be appending while this reads."""
        prepared = self._hook(tmp_path)
        session_id = launch.mint_session_id()
        prepared["readiness_path"].write_text('{"session_id": "' + session_id)
        with pytest.raises(readiness.ClaudeNativeNotReady):
            readiness.await_session_start(
                prepared["readiness_path"], session_id, timeout=0.2, poll=0.01
            )

    def test_inherited_hook_configuration_is_stripped(self):
        """The proof is only as good as knowing whose hook wrote the file."""
        env = readiness.child_environment({"CLAUDE_HOOKS": "/somewhere/else", "PATH": "/bin"})
        assert "CLAUDE_HOODS" not in env
        assert "CLAUDE_HOOKS" not in env
        assert env["PATH"] == "/bin"


class TestComposerFacts:
    def test_the_pinned_newline_keystroke_is_the_builds_own_hint(self):
        plan = control.plan_composer_keystrokes("a\nb", provider_version=PINNED)
        assert plan["soft_newline_keystroke"] == "C-j"
        assert plan["deliverable"] is True
        assert plan["encoding"] == control.ENCODING_SOFT_NEWLINE
        # The evidence travels with the plan, so a receipt reader can see
        # what the keystroke was pinned from rather than trusting it.
        assert plan["composer_evidence"]["composer_hint"] == "ctrl+j for newline"
        assert plan["composer_evidence"]["bundle_sha256"].startswith("8addc857")

    def test_an_unpinned_build_refuses_multi_line_rather_than_improvising(self):
        """Not raised — recorded, so the refusal is durable and typed.

        Splitting across turns, pasting, and flattening the newlines away
        are each a way of appearing to deliver a message that was not
        delivered, so none of them is the alternative.
        """
        plan = control.plan_composer_keystrokes("a\nb", provider_version="2.1.219")
        assert plan["deliverable"] is False
        assert "no composer newline keystroke is proven" in plan["undeliverable_reason"]

    def test_a_single_line_needs_no_pinned_keystroke(self):
        plan = control.plan_composer_keystrokes("just one line", provider_version="2.1.219")
        assert plan["deliverable"] is True
        assert plan["encoding"] == control.ENCODING_SINGLE_LINE

    def test_the_model_input_digest_is_stated_only_when_it_cannot_be_wrong(self):
        """This build's submit-time normalization was not read.

        A payload with no leading or trailing whitespace is invariant
        under trimming, so whether Claude trims cannot change what the
        model receives and the digest is safe to state. A payload that is
        not invariant would differ between the two possibilities, so no
        digest is recorded — a receipt that names one is read as evidence,
        and guessing there is worse than saying nothing.
        """
        exact = control.plan_composer_keystrokes("clean", provider_version=PINNED)
        assert exact["model_input_sha256"] == exact["composer_sha256"]
        assert exact["model_input_is_composer_exact"] is True

        padded = control.plan_composer_keystrokes("  padded  ", provider_version=PINNED)
        assert padded["model_input_sha256"] is None
        assert padded["model_input_is_composer_exact"] is None
        assert padded["submit_normalization_proven"] is False

    def test_the_payload_digest_is_over_the_callers_own_bytes(self):
        """No encoding decision here may redefine what was asked for."""
        with_terminator = control.plan_composer_keystrokes("hello\n", provider_version=PINNED)
        without = control.plan_composer_keystrokes("hello", provider_version=PINNED)
        assert with_terminator["payload_sha256"] != without["payload_sha256"]
        # ...while the composer holds the same thing either way: the
        # trailing newline is the submit keystroke, not content.
        assert with_terminator["composer_sha256"] == without["composer_sha256"]
        assert with_terminator["trailing_terminator"] == "\n"

    @pytest.mark.parametrize("artifact", ["\x1b[200~x", "^[[200~x", "a\rb", "a\x1bb"])
    def test_artifacts_never_reach_a_composer(self, artifact):
        with pytest.raises(control.NativeControlInvalid):
            control.plan_composer_keystrokes(artifact, provider_version=PINNED)

    def test_a_terminator_alone_is_not_a_message(self):
        with pytest.raises(control.NativeControlInvalid):
            control.plan_composer_keystrokes("\n", provider_version=PINNED)

    def test_the_settle_is_carried_from_the_observed_ink_behaviour(self):
        """A too-early Enter is swallowed and the message never sends."""
        plan = control.plan_composer_keystrokes("x", provider_version=PINNED)
        assert plan["submit_settle_seconds"] == 2.0


class TestSchemasAreNotShared:
    def test_claude_schemas_are_distinct_from_kimi_schemas(self):
        from cli_agent_orchestrator.services import kimi_native_control as kimi

        for claude_schema, kimi_schema in (
            (control.RECORD_SCHEMA, kimi.RECORD_SCHEMA),
            (control.INTENT_SCHEMA, kimi.INTENT_SCHEMA),
            (control.TURN_OBSERVATION_SCHEMA, kimi.TURN_OBSERVATION_SCHEMA),
            (control.PROVIDER_OBSERVATION_SCHEMA, kimi.PROVIDER_OBSERVATION_SCHEMA),
            (control.KEYSTROKE_PLAN_SCHEMA, kimi.KEYSTROKE_PLAN_SCHEMA),
        ):
            assert claude_schema != kimi_schema
            assert claude_schema.startswith("cao-claude-native-")

    def test_a_kimi_observation_cannot_satisfy_a_claude_gate(self):
        """Structural, not a rule someone has to remember to apply."""
        from cli_agent_orchestrator.services import kimi_native_control as kimi

        foreign = kimi.turn_observation(active_turn_id=None, observed_at="now", observer="test")
        with pytest.raises(control.NativeControlInvalid):
            control._validated_turn_observation(foreign)

    def test_the_two_adapters_use_separate_stores(self):
        assert (
            database.ClaudeNativeControlOperationModel.__tablename__
            != database.KimiNativeControlOperationModel.__tablename__
        )

    def test_the_observation_keyword_matches_the_kimi_adapter(self):
        """One call site dispatches to both, so the keywords must agree."""
        observation = control.turn_observation(
            active_turn_id=None, observed_at="now", observer="test"
        )
        assert observation["observer"] == "test"
        assert observation["provider"] == "claude_code"


class TestCapabilityAdvertisement:
    def test_native_support_is_derived_from_implemented_adapters(self):
        block = v2.native_tui_capabilities()
        assert block["schema_version"] == 1
        assert set(block["providers"]) == set(v2.NATIVE_TUI_PROVIDERS)
        claude = block["providers"]["claude_code"]
        assert claude["supported"] is True
        assert claude["id_source"] == "cli_session_id"
        # Canonical provider key in the map; the executable is a separate
        # namespace and appears only in its own field.
        assert claude["executable"] == "claude"
        assert "claude" not in block["providers"]

    def test_the_advertised_versions_are_derived_from_the_contract(self):
        """The handshake restates the contract tables, never a frozen copy.

        A hand-maintained expected set beside the derived one is a gate
        that decays: it would keep "passing" while the advertisement
        drifted from the contract.  So the expectation is derived from the
        same tables, and the pairing is what is asserted.
        """
        claude = v2.native_tui_capabilities()["providers"]["claude_code"]
        assert claude["supported_versions"] == list(provider_contracts.SUPPORTED_VERSIONS["claude"])
        assert claude["pinned_version"] == provider_contracts.PINNED_VERSIONS["claude"]
        assert claude["version_enforcement"] == provider_contracts.VERSION_ENFORCEMENT_OPEN
        # The pin is the advisory head of the quarantine set, in both
        # directions.
        assert claude["pinned_version"] == claude["supported_versions"][0]
        # The quarantine set's content itself is a literal a test may pin:
        # strict mode refuses exactly the builds absent from it.
        assert provider_contracts.SUPPORTED_VERSIONS["claude"] == ("2.1.233", PINNED)

    def test_an_unlisted_build_is_not_a_capability_refusal(self):
        """Open mode admits an unlisted build, and capability follows.

        The per-terminal control block for an unlisted-but-observed build
        carries the delivery surfaces with non-null defaults — unlisted is
        merely nothing written down, not a failed observation.
        """
        from cli_agent_orchestrator.services import provider_controls

        provider_contracts.check_pinned_version("claude", "2.1.219 (Claude Code)")
        block = provider_controls.controls_block_for(
            provider_contracts.PROVIDER_CLAUDE_CODE, "2.1.219 (Claude Code)"
        )
        assert block["operator_message"]["supported"] is True
        assert block["image"]["supported"] is True
        # Not the null value: the conservative default says what it is.
        assert block["image"]["build_proven"] is False

    def test_a_semver_drift_launches_but_unproven_features_remain_gated(self):
        provider_contracts.check_pinned_version("claude", "2.1.219 (Claude Code)")
        provider_contracts.check_pinned_version("claude", f"{PINNED} (Claude Code)")


class TestReserveAcceptsTheCanonicalProvider:
    def test_claude_code_is_a_reservable_provider(self):
        from cli_agent_orchestrator.models.managed_launch_v2 import (
            PROTOCOL_VERSION_V2,
            ManagedLaunchV2ReserveRequest,
        )

        request = ManagedLaunchV2ReserveRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            reservation_id=str(uuid.uuid4()),
            session_name="cao-claude",
            provider="claude_code",
            agent_profile="developer",
            caller_id="abc12345",
            working_directory="/tmp",
            expected_model="opus",
            expected_effort="high",
            provider_executable="/usr/local/bin/claude",
            provider_executable_sha256="0" * 64,
            worker_class="persistent",
            execution_mode="native_tui",
            obligation_generation=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()),
            delivery_id=str(uuid.uuid4()),
            launch_nonce=str(uuid.uuid4()),
        )
        assert request.provider == "claude_code"

    def test_the_executable_name_is_not_a_provider(self):
        """``claude`` is an executable. Accepting it here would make
        "which provider is this?" answerable two different ways."""
        from pydantic import ValidationError

        from cli_agent_orchestrator.models.managed_launch_v2 import (
            PROTOCOL_VERSION_V2,
            ManagedLaunchV2ReserveRequest,
        )

        with pytest.raises(ValidationError):
            ManagedLaunchV2ReserveRequest(
                protocol_version=PROTOCOL_VERSION_V2,
                reservation_id=str(uuid.uuid4()),
                session_name="cao-claude",
                provider="claude",
                agent_profile="developer",
                caller_id="abc12345",
                working_directory="/tmp",
                expected_model="opus",
                expected_effort="high",
                provider_executable="/usr/local/bin/claude",
                provider_executable_sha256="0" * 64,
                worker_class="persistent",
                execution_mode="native_tui",
                obligation_generation=str(uuid.uuid4()),
                run_id=str(uuid.uuid4()),
                delivery_id=str(uuid.uuid4()),
                launch_nonce=str(uuid.uuid4()),
            )


class TestProviderDispatchRefusesAnUnknownProvider:
    """Each provider's rendering gets its own reader.

    A shared one would eventually read a Claude screen with Kimi's rules
    and call a busy pane idle, which is the class of mistake the whole
    separate-adapter design exists to prevent — so an unmapped provider is
    refused rather than defaulted to whichever adapter is first.
    """

    def test_the_control_adapter_is_chosen_by_canonical_provider(self):
        assert v2._control_adapter("claude_code") is control
        from cli_agent_orchestrator.services import codex_native_control

        assert v2._control_adapter("codex") is codex_native_control
        with pytest.raises(Exception, match="no native control adapter"):
            v2._control_adapter("no_such_provider")

    def test_the_turn_observer_is_chosen_by_canonical_provider(self):
        with pytest.raises(Exception, match="no native turn-state observer"):
            v2._observe_turn_state("no_such_provider", pane_id="%1")

    def test_the_executable_name_is_not_a_provider_key(self):
        """``claude`` is the binary; ``claude_code`` is the provider."""
        with pytest.raises(Exception, match="no native control adapter"):
            v2._control_adapter("claude")


class TestClaudeSessionIsMintedBeforeAnyProviderIO:
    """The identity exists before the provider does.

    So a launch that dies before its first turn still has a recorded
    identity — the structural opposite of the Kimi path, where the id is
    read back out of a transport and does not exist until it answers.
    """

    def _record(self, tmp_path):
        return {
            "terminal_id": "abc12345",
            "generation": "gen-claude-1",
            "working_directory": str(tmp_path),
        }

    def _request(self):
        return {"expected_model": "claude-opus-4-8", "expected_effort": "high"}

    def test_minting_spends_no_provider_io_and_prepares_the_hook(self, tmp_path, monkeypatch):
        monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")

        bootstrap, hook = v2._mint_claude_native_session(
            record=self._record(tmp_path),
            request=self._request(),
            version_output=f"{PINNED} (Claude Code)",
            digest="d" * 64,
        )

        assert bootstrap["provider"] == "claude_code"
        assert bootstrap["id_source"] == "cli_session_id"
        launch.validate_session_id(bootstrap["native_session_id"])
        # No turn can have been submitted: nothing was started at all.
        assert bootstrap["task_bytes_submitted"] is False
        # The hook file is prepared *before* the launch, so Claude has
        # somewhere to write the instant it starts.
        assert Path(hook["readiness_path"]).parent.exists()
        assert bootstrap["readiness_path"] == str(hook["readiness_path"])

    def test_an_unlisted_build_mints_an_identity(self, tmp_path, monkeypatch):
        """Unpinned: the SessionStart hook is the runtime proof.

        An unlisted build needs no table row: the hook this mint installs
        either fires on the real build (identity self-proven) or does not
        (readiness fails loudly).  What still refuses before minting is a
        failed version observation — refusing after minting would record a
        session never to be started.
        """
        monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")

        bootstrap, _ = v2._mint_claude_native_session(
            record=self._record(tmp_path),
            request=self._request(),
            version_output="2.0.1 (Claude Code)",
            digest="d" * 64,
        )

        assert bootstrap["native_session_id"]

    def test_an_unparseable_banner_is_refused_before_an_identity_is_minted(
        self, tmp_path, monkeypatch
    ):
        """Unparseable is a failed observation, distinct from unlisted."""
        monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")

        with pytest.raises(Exception):
            v2._mint_claude_native_session(
                record=self._record(tmp_path),
                request=self._request(),
                version_output="not-a-version",
                digest="d" * 64,
            )

    def test_each_mint_chooses_a_fresh_identity(self, tmp_path, monkeypatch):
        """A generation is never reused, so neither is the session it names."""
        monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")

        first, _ = v2._mint_claude_native_session(
            record=self._record(tmp_path),
            request=self._request(),
            version_output=f"{PINNED} (Claude Code)",
            digest="d" * 64,
        )
        second, _ = v2._mint_claude_native_session(
            record={**self._record(tmp_path), "generation": "gen-claude-2"},
            request=self._request(),
            version_output=f"{PINNED} (Claude Code)",
            digest="d" * 64,
        )

        assert first["native_session_id"] != second["native_session_id"]
