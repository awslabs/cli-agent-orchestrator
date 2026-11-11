#!/usr/bin/env make -f
# CLI Agent Orchestrator — maintenance targets.
#
# Offline vendoring of the upstream MCP Apps builder skills
# (modelcontextprotocol/ext-apps). See skills/vendor/ext-apps/README.md.

.PHONY: refresh-ext-apps-skills check-ext-apps-skills \
        refresh-agent-plugins-schemas check-agent-plugins-schemas \
        agent-plugin agent-plugin-keep-pin check-agent-plugin \
        record-agui-trace check-agui-trace check-agui-trace-release

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
# CAO's own packages (`cao`, `cao-contributor`, `cao-agent-plugin-lifecycle`,
# `cao-ag-ui-builder`), generated from the canonical skills/ tree and committed.
# See docs/agent-plugins.md.

# Regenerate every package from its allowlist at CAO's package version. This
# moves the pin; the release workflow's regenerate-agent-plugin job runs it.
agent-plugin:
	uv run python scripts/build_agent_plugin.py

# Regenerate every package at the version the committed packages already pin.
# Use this after editing a packaged skill: it never moves the pin and never
# contacts PyPI, and it refuses when the committed packages disagree (#1152).
agent-plugin-keep-pin:
	uv run python scripts/build_agent_plugin.py --keep-committed-pin

# Verify every committed package matches its allowlist and loads with zero fatal
# findings. Evaluates each package independently so one failure cannot mask
# another. Exit 0 = in sync, 1 = drift or non-conformance.
check-agent-plugin:
	uv run python scripts/build_agent_plugin.py --check

# --- CAO Dojo AG-UI trace (#1116) --------------------------------------------
# See examples/ag-ui/cao-dojo/README.md, "Recorded trace and drift checks".
AGUI_TOOLS := examples/ag-ui/cao-dojo/tools
AGUI_TRACE := examples/ag-ui/cao-dojo/trace/cao-dojo-trace.ndjson
AGUI_NODE_DIR := examples/ag-ui/ag-ui-stock-client-live
RECORD_PORT ?= 19899
# The release-path check is stdlib-only, so the publish workflow runs it with a
# plain `python` (AGUI_PY=python) and no uv.
AGUI_PY ?= uv run python

# Record the published trace from a real Claude Code run. LOCAL ONLY: needs a
# logged-in `claude` on PATH and a clean checkout, and runs two short haiku
# sessions (see trace/README.md for the measured spend). Runs an isolated
# cao-server on RECORD_PORT (never 9889).
record-agui-trace:
	uv run python $(AGUI_TOOLS)/record_trace.py --live --port $(RECORD_PORT) --out $(AGUI_TRACE)

# The offline gates in one command: the leak rule, tier 1 in Python, tier 1 in
# node (upstream enforceEvents + verifyEvents over an offline capture and, once
# recorded, the published trace), then the re-record debt as a warning.
check-agui-trace:
	uv run pytest test/examples/test_cao_dojo_trace_leaks.py test/examples/test_cao_dojo_trace_replay.py test/examples/test_cao_dojo_trace_recorder.py -q --no-cov
	@if [ ! -d $(AGUI_NODE_DIR)/node_modules ]; then \
		echo "check-agui-trace: run 'npm ci' in $(AGUI_NODE_DIR) first (the node lane needs the pinned @ag-ui/client)" >&2; \
		exit 1; \
	fi
	@set -e; scratch="$$(mktemp -d)"; trap 'rm -rf "$$scratch"' EXIT; \
	uv run python $(AGUI_TOOLS)/record_trace.py --offline --emit "$$scratch/offline.ndjson"; \
	if [ -f $(AGUI_TRACE) ]; then published=$(AGUI_TRACE); else published=; fi; \
	node $(AGUI_NODE_DIR)/verify-trace.mjs "$$scratch/offline.ndjson" $$published
	uv run python $(AGUI_TOOLS)/record_trace.py --check-debt --warn

# The release path: blocks when the published trace is missing or was recorded
# against an older shape. Needs no server and no credentials.
check-agui-trace-release:
	$(AGUI_PY) $(AGUI_TOOLS)/record_trace.py --check-debt
