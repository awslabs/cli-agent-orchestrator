"""Unit tests for classify_reason: the total, deterministic prompt classifier."""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.services.agui.handoff_approval import classify_reason

# ---------------------------------------------------------------------------
# Format validation helpers
# ---------------------------------------------------------------------------

NAMESPACE_RE = re.compile(r"^[a-z0-9-]+$")
LOCAL_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def assert_valid_reason(reason: str) -> None:
    """Validate that a reason string has the correct format."""
    parts = reason.split(":", 1)
    assert len(parts) == 2, f"Expected 'namespace:local_name', got: {reason}"
    namespace, local_name = parts
    assert NAMESPACE_RE.match(namespace), f"Invalid namespace: {namespace}"
    assert LOCAL_NAME_RE.match(local_name), f"Invalid local_name: {local_name}"
    assert namespace != "core", f"namespace must never be 'core': {reason}"


# ---------------------------------------------------------------------------
# Claude Code provider fixtures
# ---------------------------------------------------------------------------


class TestClaudeCodeClassification:
    """Tests for claude_code provider classification."""

    def test_permission_request_from_waiting_pattern(self):
        """The TUI footer pattern triggers permission_request."""
        prompt = "Do you want to allow this? Use \u2191/\u2193 to navigate"
        result = classify_reason("claude_code", prompt)
        assert result == "claude-code:permission_request"
        assert_valid_reason(result)

    def test_trust_prompt(self):
        prompt = "Yes, I trust this folder and allow execution"
        result = classify_reason("claude_code", prompt)
        assert result == "claude-code:trust_prompt"
        assert_valid_reason(result)

    def test_unknown_prompt(self):
        prompt = "Some random output that does not match any pattern"
        result = classify_reason("claude_code", prompt)
        assert result == "claude-code:unknown_prompt"
        assert_valid_reason(result)

    def test_trust_takes_priority_over_waiting(self):
        """Trust prompt pattern takes priority even if waiting pattern also matches."""
        prompt = "Yes, I trust this folder \u2191/\u2193 to navigate"
        result = classify_reason("claude_code", prompt)
        assert result == "claude-code:trust_prompt"


# ---------------------------------------------------------------------------
# Kiro CLI provider fixtures
# ---------------------------------------------------------------------------


class TestKiroCliClassification:
    """Tests for kiro_cli provider classification."""

    def test_tui_permission_pattern(self):
        prompt = "  Yes  No  Always allow this action"
        result = classify_reason("kiro_cli", prompt)
        assert result == "kiro:permission_request"
        assert_valid_reason(result)

    def test_tui_permission_pattern_single_permission(self):
        prompt = "Yes, single permission\nSome text\nTrust, always allow\nMore\nNo"
        result = classify_reason("kiro_cli", prompt)
        assert result == "kiro:permission_request"
        assert_valid_reason(result)

    def test_legacy_permission_pattern(self):
        prompt = "Allow this action? [y/n/t]:"
        result = classify_reason("kiro_cli", prompt)
        assert result == "kiro:permission_request"
        assert_valid_reason(result)

    def test_trust_prompt(self):
        prompt = "Do you trust this workspace?"
        result = classify_reason("kiro_cli", prompt)
        assert result == "kiro:trust_prompt"
        assert_valid_reason(result)

    def test_unknown_prompt(self):
        prompt = "Processing your request..."
        result = classify_reason("kiro_cli", prompt)
        assert result == "kiro:unknown_prompt"
        assert_valid_reason(result)


# ---------------------------------------------------------------------------
# Codex provider fixtures
# ---------------------------------------------------------------------------


class TestCodexClassification:
    """Tests for codex provider classification."""

    def test_approval_request(self):
        prompt = "Approve execution of script.sh? (y/n)"
        result = classify_reason("codex", prompt)
        assert result == "codex:approval_request"
        assert_valid_reason(result)

    def test_allow_pattern(self):
        prompt = "Allow write to /tmp/file? yes/no"
        result = classify_reason("codex", prompt)
        assert result == "codex:approval_request"
        assert_valid_reason(result)

    def test_trust_prompt(self):
        prompt = "Would you like to allow Codex to work in this folder?"
        result = classify_reason("codex", prompt)
        assert result == "codex:trust_prompt"
        assert_valid_reason(result)

    def test_unknown_prompt(self):
        prompt = "Thinking about your question..."
        result = classify_reason("codex", prompt)
        assert result == "codex:unknown_prompt"
        assert_valid_reason(result)


# ---------------------------------------------------------------------------
# Unknown / fallback provider
# ---------------------------------------------------------------------------


class TestUnknownProvider:
    """Tests for unknown providers (kebab-case fallback)."""

    def test_unknown_provider_kebab_case(self):
        result = classify_reason("my_custom_provider", "some prompt")
        assert result == "my-custom-provider:unknown_prompt"
        assert_valid_reason(result)

    def test_empty_provider(self):
        result = classify_reason("", "some prompt")
        assert_valid_reason(result)

    def test_provider_named_core_is_safe(self):
        """A provider literally named 'core' must NOT produce core: namespace."""
        result = classify_reason("core", "some prompt")
        assert not result.startswith("core:")
        assert_valid_reason(result)

    def test_provider_with_special_chars(self):
        result = classify_reason("Provider_With_CAPS!", "prompt")
        assert_valid_reason(result)

    def test_empty_prompt(self):
        result = classify_reason("claude_code", "")
        assert result == "claude-code:unknown_prompt"
        assert_valid_reason(result)


# ---------------------------------------------------------------------------
# Closed-set enforcement
# ---------------------------------------------------------------------------


class TestClosedSets:
    """Verify that known providers only produce their closed local-name sets."""

    CLAUDE_CODE_SET = {"permission_request", "trust_prompt", "unknown_prompt"}
    KIRO_SET = {"permission_request", "trust_prompt", "unknown_prompt"}
    CODEX_SET = {"approval_request", "trust_prompt", "unknown_prompt"}

    @pytest.mark.parametrize(
        "prompt",
        [
            "",
            "random text",
            "\u2191/\u2193 to navigate",
            "Yes, I trust this folder",
            "x" * 10000,
        ],
    )
    def test_claude_code_closed_set(self, prompt):
        result = classify_reason("claude_code", prompt)
        local = result.split(":")[1]
        assert local in self.CLAUDE_CODE_SET

    @pytest.mark.parametrize(
        "prompt",
        [
            "",
            "random text",
            "Yes  No  Always allow",
            "Allow this action? [y/n/t]:",
            "trust this workspace",
        ],
    )
    def test_kiro_closed_set(self, prompt):
        result = classify_reason("kiro_cli", prompt)
        local = result.split(":")[1]
        assert local in self.KIRO_SET

    @pytest.mark.parametrize(
        "prompt",
        [
            "",
            "random text",
            "Approve this? (y/n)",
            "allow Codex to work in this folder",
        ],
    )
    def test_codex_closed_set(self, prompt):
        result = classify_reason("codex", prompt)
        local = result.split(":")[1]
        assert local in self.CODEX_SET


# ---------------------------------------------------------------------------
# Hypothesis property tests (P2: totality)
# ---------------------------------------------------------------------------


class TestClassifyReasonProperty:
    """Property-based tests for classify_reason totality."""

    @given(
        provider=st.text(min_size=0, max_size=100),
        raw_prompt=st.text(min_size=0, max_size=5000),
    )
    @settings(max_examples=200)
    def test_never_raises(self, provider, raw_prompt):
        """classify_reason is total: never raises for any input."""
        result = classify_reason(provider, raw_prompt)
        # Must always return a well-formed string
        assert isinstance(result, str)
        assert_valid_reason(result)

    @given(
        provider=st.text(min_size=0, max_size=100),
        raw_prompt=st.text(min_size=0, max_size=5000),
    )
    @settings(max_examples=200)
    def test_never_returns_core(self, provider, raw_prompt):
        """classify_reason never returns a core: namespace."""
        result = classify_reason(provider, raw_prompt)
        assert not result.startswith("core:")

    @given(
        provider=st.sampled_from(["claude_code", "kiro_cli", "codex"]),
        raw_prompt=st.text(min_size=0, max_size=5000),
    )
    @settings(max_examples=200)
    def test_deterministic(self, provider, raw_prompt):
        """Same inputs always produce same output."""
        r1 = classify_reason(provider, raw_prompt)
        r2 = classify_reason(provider, raw_prompt)
        assert r1 == r2
