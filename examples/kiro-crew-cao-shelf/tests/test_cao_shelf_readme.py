"""The README's remote-control promises match the manifest, the agent and the routes."""

from __future__ import annotations

import json
import re

from cao_shelf_support import ROOT

README = (ROOT / "README.md").read_text(encoding="utf-8")
MANIFEST = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))
OPS_TOOLS = (
    "send_session_message",
    "get_terminal_status",
    "read_session_output",
    "get_session_info",
)


def section(title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## )", README, flags=re.S | re.M)
    assert match, title
    return " ".join(match.group(1).split())  # prose wraps: compare with spaces collapsed


def test_the_allow_rule_names_match_the_manifest_server_name() -> None:
    agent_file = json.loads((ROOT / MANIFEST["agents"][0]).read_text(encoding="utf-8"))
    (entry,) = agent_file["mcpServers"]  # the only MCP server: "cao-shelf:ops"
    server = entry.split(":", 1)[1]
    names = [f"mcp__{MANIFEST['name']}_{server}__{tool}" for tool in OPS_TOOLS]
    rc = section("Remote control")
    for name in names + ["mcp__kirocrew-core__wait"]:
        assert f'"{name}"' in rc, name
    agent = json.loads((ROOT / MANIFEST["agents"][0]).read_text(encoding="utf-8"))
    assert agent["tools"] == [f"@{MANIFEST['name']}:{server}/{t}" for t in OPS_TOOLS] + [
        "@kirocrew-core/wait"
    ]


def test_every_new_route_is_listed() -> None:
    for route in (
        "GET /api/relays",
        "POST /api/relays",
        "POST /api/relays/{id}/end",
        "POST /api/relays/{id}/takeover",
    ):
        assert f"`{route}`" in README, route


def test_the_remote_control_section_says_what_holds_and_what_does_not() -> None:
    rc = section("Remote control")
    for phrase in (
        "held by the agent's instructions, not by code",
        "best effort on CAO 2.5.4",
        "30 status checks",
        "Answer box",
        "never approves",
        "session-approval consent",
        "model provider",
        "kiro-cli backend",
        "channels",
        "[UNVERIFIED]",
    ):
        assert phrase in rc, phrase


def test_the_secret_promise_is_amended() -> None:
    never = section("What it never does")
    assert "No secrets stored or shipped" not in README
    assert "App secret" in never and "never stores or logs" in never
    assert "Claude settings" in never


def test_privacy_how_it_talks_and_limits_mention_the_relay() -> None:
    assert "relays.json" in section("Privacy")
    assert "model provider" in section("Privacy")
    talks = section("How it talks to CAO")
    assert "cao-ops-mcp-server" in talks and "/api/chat" in talks
    assert "Remote control" in section("Limits and known unverified items")


def test_verified_behaviour_records_the_rig_journeys_and_what_stays_unverified() -> None:
    verified = section("Verified behaviour and limits")
    for phrase in (
        "test_j9_remote_control.py",
        "exactly the four",
        "negative control",
        "End relay",
        "Start and attach",
        "openChat({slotKey})",
        "113 `get_terminal_status` calls",
        "real model",
        "kiro-cli backend",
        "channels",
        "CAO v2.5.4",
    ):
        assert phrase in verified, phrase
    # the 7.1 placeholder for the measured count is gone
    assert "once\n  the rig journey has run" not in README
    assert "113 status checks" in section("Remote control")


def test_the_readme_states_the_relay_risk_word_for_word() -> None:
    from cao_shelf import relays

    text = README
    assert "### What the chat can reach (read this first)" in text
    assert "> " + relays.RISK in text
    for paragraph in relays.RISK_DETAIL:
        assert "> " + paragraph in text, paragraph[:40]
    # the lines the security review cited no longer say that normal mode asks for every built-in
    assert "makes each of those\n  ask you" not in text
    assert "only the `cao-remote-control` agent mounts these tools" not in text
    assert "approve_relay_tools" not in text


def test_the_readme_says_what_the_session_approval_grant_allows_and_how_the_app_uses_it() -> None:
    assert (
        "approve tool requests in, send messages to, and\nset the approval mode of **any** of your local Kiro Crew chats"
        in README
    )
    assert "uses it only for that: setting `normal` mode on its own relay chats" in README
    assert "also set\n`sandbox.autoAllowBashIfSandboxed: false` explicitly" in README
    assert "the default is false" not in README
    assert "can keep running after End relay or\n  Take over" in README
