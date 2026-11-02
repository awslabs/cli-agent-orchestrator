#!/usr/bin/env make -f
# CLI Agent Orchestrator — maintenance targets.
#
# Offline vendoring of the upstream MCP Apps builder skills
# (modelcontextprotocol/ext-apps). See skills/vendor/ext-apps/README.md.

.PHONY: refresh-ext-apps-skills check-ext-apps-skills \
        refresh-agent-plugins-schemas check-agent-plugins-schemas \
        agent-plugin agent-plugin-keep-pin check-agent-plugin

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
# CAO's own packages (`cao`, `cao-contributor`, `cao-agent-plugin-lifecycle`),
# generated from the canonical skills/ tree and committed. See docs/agent-plugins.md.

# Regenerate every package from its allowlist at CAO's package version. This
# moves the pin; the release workflow's regenerate-agent-plugin job runs it.
agent-plugin:
	uv run python scripts/build_agent_plugin.py

# Regenerate every package at the version the committed packages already pin.
# Use this after editing a packaged skill: it never moves the pin and never
# contacts PyPI, and it refuses when the committed packages disagree (#1152).
agent-plugin-keep-pin:
	uv run python scripts/build_agent_plugin.py --keep-committed-pin

# Verify every committed package match their allowlists and load with zero fatal
# findings. Evaluates each package independently so one failure cannot mask
# another. Exit 0 = in sync, 1 = drift or non-conformance.
check-agent-plugin:
	uv run python scripts/build_agent_plugin.py --check
