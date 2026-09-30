"""``POST /plugins`` cannot reach ``git clone`` with a transport or host CAO refuses.

The route builds the ``PluginSource`` itself and, with ``"kind": "git"`` in the
body, skips the CLI's shape heuristic entirely, so the allowlist has to live in
the resolver, not in the heuristic. These cases drive the real route with the
surface enabled and auth off (the default posture) and assert both the 400 and
that no git process was ever spawned.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.agent_plugins import resolver


@pytest.fixture
def plugins_enabled(monkeypatch):
    monkeypatch.setenv("CAO_AGENT_PLUGINS_ENABLED", "1")
    monkeypatch.delenv("CAO_PLUGIN_ALLOWED_HOSTS", raising=False)


@pytest.fixture
def no_git(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        raise AssertionError("git was spawned for a refused plugin source")

    monkeypatch.setattr(resolver.subprocess, "run", fake_run)
    return calls


@pytest.mark.parametrize(
    "source",
    [
        "file:///home/someone/private-repo.git",
        "git://127.0.0.1:9418/anything",
        "git://169.254.169.254/latest",
        "ext::sh -c id",
        "http://github.com/org/repo",
        "https://internal.corp.example/team/repo.git",
        "https://github.com.evil.example/org/repo",
        "https://user:token@github.com/org/repo",
        "/home/someone/private-repo",
    ],
)
def test_post_plugins_refuses_the_source_before_git_runs(client, plugins_enabled, no_git, source):
    resp = client.post("/plugins", json={"kind": "git", "source": source})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "Refusing plugin git source" in detail or "Unsupported git source form" in detail
    assert "CAO_PLUGIN_ALLOWED_HOSTS" in detail
    assert no_git == []


def test_validate_route_refuses_the_same_way(client, plugins_enabled, no_git):
    resp = client.post("/plugins/validate", json={"kind": "git", "source": "file:///tmp/x.git"})
    assert resp.status_code == 400, resp.text
    assert no_git == []


def test_an_allowed_https_source_does_reach_git(client, plugins_enabled, monkeypatch):
    """Control: the allowlist refuses by verdict, not by breaking the route."""
    seen = []
    envs = []

    def fake_run(command, **kwargs):
        seen.append(command)
        envs.append(kwargs.get("env"))
        import subprocess

        raise subprocess.CalledProcessError(128, command, stderr="fatal: could not read")

    monkeypatch.setattr(resolver.subprocess, "run", fake_run)
    resp = client.post(
        "/plugins", json={"kind": "git", "source": "https://github.com/org/repo.git"}
    )
    assert resp.status_code == 400, resp.text
    assert seen and seen[0][0] == "git" and "https://github.com/org/repo.git" in seen[0]
    flags = [seen[0][i + 1] for i, tok in enumerate(seen[0]) if tok == "-c"]
    assert "http.followRedirects=false" in flags
    assert envs and all(env and env.get("GIT_ALLOW_PROTOCOL") == "https:ssh" for env in envs)
