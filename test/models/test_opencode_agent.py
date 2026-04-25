"""Unit tests for OpenCodeAgentConfig Pydantic model."""

import frontmatter

from cli_agent_orchestrator.models.opencode_agent import OpenCodeAgentConfig


class TestOpenCodeAgentConfig:
    def test_default_mode_is_all(self):
        cfg = OpenCodeAgentConfig(description="A developer agent")
        assert cfg.mode == "all"

    def test_permission_defaults_to_none(self):
        cfg = OpenCodeAgentConfig(description="A developer agent")
        assert cfg.permission is None

    def test_permission_field_accepted(self):
        perm = {"bash": "allow", "read": "allow", "task": "deny"}
        cfg = OpenCodeAgentConfig(description="dev", permission=perm)
        assert cfg.permission == perm

    def test_mode_subagent(self):
        cfg = OpenCodeAgentConfig(description="sub", mode="subagent")
        assert cfg.mode == "subagent"

    def test_model_dump_excludes_none(self):
        cfg = OpenCodeAgentConfig(description="dev")
        dumped = cfg.model_dump(exclude_none=True)
        assert "permission" not in dumped
        assert dumped["description"] == "dev"
        assert dumped["mode"] == "all"

    def test_frontmatter_round_trip_without_permission(self):
        """Verify frontmatter.dumps() produces valid OpenCode markdown."""
        cfg = OpenCodeAgentConfig(description="A developer agent")
        body = "You are a skilled developer."
        post = frontmatter.Post(body, **cfg.model_dump(exclude_none=True))
        output = frontmatter.dumps(post)
        assert "---" in output
        assert "description: A developer agent" in output
        assert "mode: all" in output
        assert "permission:" not in output
        assert body in output

    def test_frontmatter_round_trip_with_permission(self):
        perm = {"bash": "allow", "read": "allow", "task": "deny"}
        cfg = OpenCodeAgentConfig(description="dev", permission=perm)
        body = "System prompt here."
        post = frontmatter.Post(body, **cfg.model_dump(exclude_none=True))
        output = frontmatter.dumps(post)
        assert "permission:" in output
        assert "bash: allow" in output
        assert body in output

    def test_frontmatter_parses_back(self):
        """A dumped file can be re-loaded by the frontmatter library."""
        cfg = OpenCodeAgentConfig(description="dev", permission={"bash": "allow"})
        body = "Body text."
        post = frontmatter.Post(body, **cfg.model_dump(exclude_none=True))
        raw = frontmatter.dumps(post)
        reloaded = frontmatter.loads(raw)
        assert reloaded["description"] == "dev"
        assert reloaded["mode"] == "all"
        assert reloaded.content == body
