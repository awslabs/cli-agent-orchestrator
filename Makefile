#!/usr/bin/env make -f
# CLI Agent Orchestrator — maintenance targets.
#
# Offline vendoring of the upstream MCP Apps builder skills
# (modelcontextprotocol/ext-apps). See skills/vendor/ext-apps/README.md.

.PHONY: refresh-ext-apps-skills check-ext-apps-skills \
        refresh-agent-plugins-schemas check-agent-plugins-schemas \
        agent-plugin check-agent-plugin

# Re-vendor the ext-apps builder skills from the pinned tag and rewrite NOTICE.
# To move to a newer upstream release, bump PINNED_REF/PINNED_SHA in
# scripts/vendor_ext_apps_skills.py first, then run this target.
refresh-ext-apps-skills:
	uv run python scripts/vendor_ext_apps_skills.py

# Verify the on-disk vendored copy still matches the pin (CI / pre-commit).
# Exit 0 = in sync, 1 = drift, 2 = network-gated (could not verify).
check-ext-apps-skills:
	uv run python scripts/vendor_ext_apps_skills.py --check

# --- Agent Plugins 1.0.0 pinned schemas -------------------------------------
# Agent Plugins §5.2 forbids retrieving a schema while loading a plugin, so CAO
# validates against bytes committed to this repo. See docs/agent-plugins.md.

# Re-fetch the canonical 1.0.0 schemas and rewrite PIN.json. Requires network.
refresh-agent-plugins-schemas:
	uv run python scripts/vendor_agent_plugins_schemas.py

# Verify the vendored schema bytes still hash to PIN.json. Offline by design —
# this is the CI-on-every-PR guard. Exit 0 = in sync, 1 = drift.
check-agent-plugins-schemas:
	uv run python scripts/vendor_agent_plugins_schemas.py --check

# --- CAO's own Agent Plugins packages ---------------------------------------
# Two packages, `cao` (operator) and `cao-contributor`, generated from the
# canonical skills/ tree and committed. See docs/agent-plugins.md.

# Regenerate both packages from their allowlists and CAO's package version.
agent-plugin:
	uv run python scripts/build_agent_plugin.py

# Verify both committed packages match their allowlists and load with zero fatal
# findings. Evaluates each package independently so one failure cannot mask the
# other. Exit 0 = in sync, 1 = drift or non-conformance.
check-agent-plugin:
	uv run python scripts/build_agent_plugin.py --check
