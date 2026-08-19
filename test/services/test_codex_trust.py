from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.providers.codex import (
    CodexProvider,
    render_trusted_project_override,
)
from cli_agent_orchestrator.services import codex_trust, provider_contracts
from cli_agent_orchestrator.services.codex_trust import (
    CodexTrustProbeError,
    attest_trusted_project,
)


def _app_server_stdout(root: str) -> str:
    responses = [
        {"id": 1, "result": {"serverInfo": {"name": "codex"}}},
        {
            "id": 2,
            "result": {
                "config": {"projects": {root: {"trust_level": "trusted"}}},
                "origins": {"projects": {root: {"trust_level": "sessionFlags"}}},
                "layers": [],
            },
        },
        {
            "id": 3,
            "result": {
                "cwd": root,
                "model": "gpt-5.6-sol",
                "modelProvider": "openai",
                "reasoningEffort": "xhigh",
                "thread": {"id": "thread-zero-turn"},
            },
        },
    ]
    return "".join(json.dumps(item) + "\n" for item in responses)


def test_trust_override_renders_dotted_worktree_key_byte_exact(tmp_path):
    target = tmp_path / "fixture.with.dot" / ".worktrees" / "review"
    target.mkdir(parents=True)
    root = str(target.resolve())
    assert render_trusted_project_override(root) == (
        f'projects={{"{root}"={{trust_level="trusted"}}}}'
    )


def test_trust_override_rejects_noncanonical_or_relative_path(tmp_path):
    target = tmp_path / "worktree"
    target.mkdir()
    with pytest.raises(ValueError):
        render_trusted_project_override("relative/path")
    with pytest.raises(ValueError):
        render_trusted_project_override(str(target / ".." / "worktree"))


def test_probe_verifies_config_origin_route_and_zero_turn(tmp_path, monkeypatch):
    target = tmp_path / "fixture.with.dot" / ".worktrees" / "review"
    target.mkdir(parents=True)
    root = str(target.resolve())
    user_config = tmp_path / "config.toml"
    user_config.write_text('model = "placeholder"\n')
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="codex-cli 0.146.0\n", stderr="")

    def fake_app_server(argv, requests, timeout):
        calls.append((argv, {"requests": requests, "timeout": timeout}))
        return _app_server_stdout(root), "", -15

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.codex_trust._run_app_server_probe",
        fake_app_server,
    )
    receipt = attest_trusted_project(
        root,
        expected_model="gpt-5.6-sol",
        expected_effort="xhigh",
        user_config_path=user_config,
    )

    assert receipt["project_root"] == root
    assert receipt["config_origin"] == "sessionFlags"
    assert receipt["model"] == "gpt-5.6-sol"
    assert receipt["reasoning_effort"] == "xhigh"
    assert receipt["no_turn_started"] is True
    app_argv, app_kwargs = calls[1]
    assert render_trusted_project_override(root) in app_argv
    requests = app_kwargs["requests"]
    assert [item.get("method") for item in requests] == [
        "initialize",
        "initialized",
        "config/read",
        "thread/start",
    ]
    assert all(item.get("method") != "turn/start" for item in requests)


@pytest.mark.parametrize("banner", ["codex-cli 0.146.0\n", "codex-cli 0.147.0\n"])
def test_zero_turn_receipt_identity_is_exact_and_version_scoped(tmp_path, monkeypatch, banner):
    """The route-attest capability admits 0.147.0 with a byte-exact receipt.

    Both accepted builds run the identical zero-turn exchange — initialize,
    config/read, ephemeral thread/start, never a turn/start — so no prompt
    or task is ever admitted, the trust root resolves exactly ``trusted``
    from sessionFlags provenance, and the two receipts differ only in the
    ``codex_version`` key that records which build attested.
    """
    target = tmp_path / "fixture.with.dot" / ".worktrees" / "review"
    target.mkdir(parents=True)
    root = str(target.resolve())
    user_config = tmp_path / "config.toml"
    user_config.write_text('model = "placeholder"\n')
    app_requests: list = []

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=banner, stderr="")

    def fake_app_server(argv, requests, timeout):
        app_requests.append(requests)
        return _app_server_stdout(root), "", -15

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.codex_trust._run_app_server_probe",
        fake_app_server,
    )

    receipt = attest_trusted_project(
        root,
        expected_model="gpt-5.6-sol",
        expected_effort="xhigh",
        user_config_path=user_config,
    )

    assert receipt["codex_version"] == banner.strip()
    assert receipt["no_turn_started"] is True
    # The zero-turn trust/route identity stays exact for both accepted builds.
    assert receipt["project_root"] == root
    assert receipt["trust_level"] == "trusted"
    assert receipt["config_origin"] == "sessionFlags"
    assert receipt["model"] == "gpt-5.6-sol"
    assert receipt["model_provider"] == "openai"
    assert receipt["reasoning_effort"] == "xhigh"
    assert receipt["cwd"] == root
    assert receipt["thread_id"] == "thread-zero-turn"
    assert receipt["probe_version"] == codex_trust.PROBE_VERSION
    assert len(receipt["argv_sha256"]) == 64
    # The same zero-task request sequence, never a turn/start.
    assert [item.get("method") for item in app_requests[0]] == [
        "initialize",
        "initialized",
        "config/read",
        "thread/start",
    ]
    assert all(item.get("method") != "turn/start" for item in app_requests[0])


def test_an_unlisted_build_is_not_refused_for_its_version(tmp_path, monkeypatch):
    """A build nobody listed reaches the probe and is judged by the exchange.

    The former exact-set gate refused every future build by default, so a
    vendor release removed route attestation without anyone deciding to give
    it up. Capability is now decided by the exchange this probe actually runs
    — every response present and error-free, the exact project resolved as
    trusted, the protected config byte-identical — which is a fact about the
    installed binary rather than about a list.
    """
    target = tmp_path / "worktree"
    target.mkdir()
    root = str(target.resolve())
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="codex-cli 0.999.0\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        codex_trust, "_run_app_server_probe", lambda *a, **k: (_app_server_stdout(root), "", 0)
    )
    receipt = attest_trusted_project(
        root,
        expected_model="gpt-5.6-sol",
        expected_effort="xhigh",
        user_config_path=tmp_path / "absent.toml",
    )
    assert receipt["codex_version"] == "codex-cli 0.999.0"


@pytest.mark.parametrize("banner", ["", "codex-cli unknown", "codex-cli 0.147"])
def test_an_unreadable_version_still_fails_closed(tmp_path, monkeypatch, banner):
    """Open is not unconditional: a version that cannot be read is refused.

    The observation is recorded on the receipt, so a banner nobody can parse
    would put an unusable string where a version belongs.
    """
    target = tmp_path / "worktree"
    target.mkdir()
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=banner + "\n", stderr=""),
    )
    with pytest.raises(CodexTrustProbeError, match="semver-shaped Codex version"):
        attest_trusted_project(
            str(target.resolve()),
            expected_model="gpt-5.6-sol",
            expected_effort="xhigh",
            user_config_path=tmp_path / "absent.toml",
        )


def test_a_failed_version_probe_still_fails_closed(tmp_path, monkeypatch):
    target = tmp_path / "worktree"
    target.mkdir()
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(CodexTrustProbeError, match="semver-shaped Codex version"):
        attest_trusted_project(
            str(target.resolve()),
            expected_model="gpt-5.6-sol",
            expected_effort="xhigh",
            user_config_path=tmp_path / "absent.toml",
        )


def test_codex_command_carries_typed_trust_override(tmp_path, monkeypatch):
    target = tmp_path / ".worktrees" / "review"
    target.mkdir(parents=True)
    root = str(target.resolve())
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.codex.load_agent_profile",
        lambda _name: SimpleNamespace(
            codexProfile=None,
            model="gpt-5.6-sol",
            system_prompt="",
            mcpServers=None,
            codexConfig={"model_reasoning_effort": "xhigh"},
        ),
    )
    provider = CodexProvider(
        "deadbeef",
        "cao-test",
        "reviewer",
        "reviewer-sol-max",
        ["read"],
        trusted_project_root=root,
        expected_model="gpt-5.6-sol",
        expected_effort="xhigh",
    )
    command = provider._build_codex_command()
    assert render_trusted_project_override(root) in command
    # The unified composer emits the route last (--model then the
    # reasoning-effort override), after the canonical trust override.
    assert command.endswith("--model gpt-5.6-sol -c 'model_reasoning_effort=\"xhigh\"'")


class TestConfigDigestFollowsSymlinks:
    """The digest describes the config as the provider will read it.

    Its only use is a before/after comparison proving the operator's config did
    not change during the probe. A managed-dotfile installation symlinks this
    config into a version-controlled settings tree, and refusing links took the
    whole provider offline while protecting nothing the comparison did not
    already cover.
    """

    def test_a_symlinked_config_digests_as_its_target(self, tmp_path):
        target = tmp_path / "real-config.toml"
        target.write_text("model = 'gpt-5.6-sol'\n")
        link = tmp_path / "config.toml"
        link.symlink_to(target)

        assert codex_trust._digest_or_absent(link) == codex_trust._digest_or_absent(target)

    def test_a_change_through_the_symlink_is_still_detected(self, tmp_path):
        # This is the property the guard actually defends. It survives.
        target = tmp_path / "real-config.toml"
        target.write_text("model = 'a'\n")
        link = tmp_path / "config.toml"
        link.symlink_to(target)

        before = codex_trust._digest_or_absent(link)
        target.write_text("model = 'b'\n")
        assert codex_trust._digest_or_absent(link) != before

    def test_repointing_the_symlink_is_still_detected(self, tmp_path):
        # A swapped link is a changed config, and the comparison catches it.
        a = tmp_path / "a.toml"
        a.write_text("model = 'a'\n")
        b = tmp_path / "b.toml"
        b.write_text("model = 'b'\n")
        link = tmp_path / "config.toml"
        link.symlink_to(a)

        before = codex_trust._digest_or_absent(link)
        link.unlink()
        link.symlink_to(b)
        assert codex_trust._digest_or_absent(link) != before

    def test_a_symlink_to_a_directory_is_still_refused(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        link = tmp_path / "config.toml"
        link.symlink_to(d)

        with pytest.raises(CodexTrustProbeError, match="not a regular file"):
            codex_trust._digest_or_absent(link)

    def test_a_broken_symlink_reads_as_absent(self, tmp_path):
        # The provider sees nothing through it either.
        link = tmp_path / "config.toml"
        link.symlink_to(tmp_path / "does-not-exist.toml")

        assert codex_trust._digest_or_absent(link) == "absent"

    def test_a_plain_regular_file_is_unchanged(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text("model = 'x'\n")
        assert codex_trust._digest_or_absent(p) not in ("absent", "")
