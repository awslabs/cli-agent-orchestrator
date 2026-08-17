#!/usr/bin/env bash
# cao-entrypoint — start one CAO node inside a container.
#
# In the k8s one-agent-per-pod topology (k8s/), the same entrypoint serves two
# roles distinguished purely by env: the SUPERVISOR pod (code_supervisor
# profile, CAO_ADVERTISED_URL set, no terminal cap) delegates work to WORKER
# pods (worker profile, CAO_MAX_TERMINALS=1 — exactly one agent per pod) via
# the assign/handoff `target_host` parameter.
#
# Env:
#   CAO_BIND_HOST         bind address for cao-server (default 0.0.0.0 —
#                         container networking is expected to gate access;
#                         see the security note below)
#   CAO_API_PORT          server port (default 9889)
#   CAO_ALLOWED_HOSTS     comma-separated extra Host-header values (set this to
#                         the container/pod DNS name peers will use to reach it)
#   CAO_INSTALL_PROFILES  optional space-separated "profile:provider" pairs to
#                         install before the server starts,
#                         e.g. "code_supervisor:kiro_cli developer:claude_code"
#   CAO_MAX_TERMINALS     optional cap on live terminals this node will host
#                         (unset = unlimited). Worker pods set 1 so each pod
#                         hosts exactly one agent; extra placements get HTTP 429.
#   CAO_ADVERTISED_URL    base URL at which PEERS can reach this node's
#                         cao-server (e.g. http://cao-supervisor:9889). Set on
#                         the supervisor pod so remote workers' send_message
#                         callbacks can route results back cross-pod.
#
# SECURITY: cao-server has no per-request auth by default. Anyone who can reach
# the port can launch agents (command execution) in this container. Restrict
# reachability with Docker networks / k8s NetworkPolicy, or enable CAO's OAuth
# layer (AUTH0_DOMAIN / CAO_AUTH_JWKS_URI).
set -euo pipefail

BIND_HOST="${CAO_BIND_HOST:-0.0.0.0}"
PORT="${CAO_API_PORT:-9889}"

echo "[cao-entrypoint] initializing CAO state"
cao init || true

if [ -n "${CAO_INSTALL_PROFILES:-}" ]; then
  for spec in ${CAO_INSTALL_PROFILES}; do
    profile="${spec%%:*}"
    provider="${spec##*:}"
    echo "[cao-entrypoint] installing profile '${profile}' for provider '${provider}'"
    cao install "${profile}" --provider "${provider}" \
      || echo "[cao-entrypoint] WARN: install ${spec} failed (continuing)"
  done
fi

echo "[cao-entrypoint] starting cao-server on ${BIND_HOST}:${PORT}"
exec cao-server --host "${BIND_HOST}" --port "${PORT}"
