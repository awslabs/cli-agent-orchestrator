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
#                         e.g. "code_supervisor:claude_code developer:claude_code"
#   CAO_WARM_PROVIDER     set to 0 to skip the provider preflight below
#                         (default: run it)
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

# Provider preflight — one throwaway model call before the server accepts work.
#
# This is not a health check, it is a warm-up, and it exists because of two
# behaviours that otherwise both land on the first real prompt:
#
#   1. The first invocation of an Anthropic model in an account that has not
#      activated it makes Bedrock auto-initiate an AWS Marketplace subscription.
#      Activation can take up to two minutes, during which calls fail with a 403
#      naming marketplace actions. Paying that cost here, while the pod is
#      starting anyway, keeps it off the critical path.
#   2. Every distinct model needs its own activation, so this warms each pinned
#      tier rather than just the primary.
#
# Non-fatal on purpose, and note that `|| true` is doing real work under
# `set -euo pipefail`: an exit 127 anywhere in a pipeline becomes the pipeline's
# status, so a missing binary would otherwise kill the container. A pod that
# cannot reach Bedrock should still start and report a clear error to an operator
# rather than crash-looping with the reason buried in a previous container's log.
if [ "${CAO_WARM_PROVIDER:-1}" != "0" ] && [ "${CLAUDE_CODE_USE_BEDROCK:-}" = "1" ]; then
  for model_var in ANTHROPIC_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL; do
    model="${!model_var:-}"
    [ -n "${model}" ] || continue
    echo "[cao-entrypoint] warming ${model_var}=${model}"
    if ANTHROPIC_MODEL="${model}" timeout 180 claude -p "Reply with the single word: ok" \
         >/tmp/warm.log 2>&1; then
      echo "[cao-entrypoint] ${model_var} responded: $(tr -d '\n' < /tmp/warm.log | tail -c 120)"
    else
      echo "[cao-entrypoint] WARN: ${model_var} did not respond; first real prompt may be slow or fail"
      echo "[cao-entrypoint] WARN: $(tr -d '\n' < /tmp/warm.log | tail -c 300)"
    fi
  done
  rm -f /tmp/warm.log || true
fi

echo "[cao-entrypoint] starting cao-server on ${BIND_HOST}:${PORT}"
exec cao-server --host "${BIND_HOST}" --port "${PORT}"
