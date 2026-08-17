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
# Workspace bootstrap (git as the workspace transport — no volume mounts).
# Credentials are INFRASTRUCTURE (configured once, work for any repo on the
# git host); repos are RUNTIME DATA (pre-cloned as a warm cache, or cloned
# on demand by an agent mid-task). Set the same values on the supervisor and
# every worker so a workspace resolves to the same in-pod path fleet-wide.
#
#   CAO_GIT_TOKEN         optional token for private repos; written to a git
#                         credential store file (0600), NOT embedded in any
#                         remote URL, so it never shows in `git remote -v`
#                         or process lists. Source it from a k8s Secret.
#                         Works for every repo the token can access — agents
#                         can clone/push repos that were never pre-cloned.
#   CAO_GIT_HOST          git host the token applies to (default: github.com)
#   CAO_GIT_USERNAME      username paired with the token (default:
#                         x-access-token, which GitHub expects for PAT/app
#                         tokens)
#   CAO_GIT_USER_NAME /   commit identity for agent commits (defaults:
#   CAO_GIT_USER_EMAIL    "CAO Agent" / cao-agent@<hostname>)
#   CAO_WORKSPACE_REPOS   optional whitespace/comma-separated HTTPS git URLs
#                         to pre-clone under CAO_WORKSPACE_ROOT before the
#                         server starts, each into <root>/<repo-name>. This
#                         is a warm cache, not a limit — switching repos
#                         needs no redeploy: pass a different
#                         working_directory per assignment, or have the
#                         agent clone what it needs (credentials above
#                         already apply). Startup FAILS if a listed clone
#                         fails: a half-provisioned node would otherwise
#                         accept placements it cannot honor.
#   CAO_WORKSPACE_ROOT    parent dir for clones (default: $HOME/workspace)
#
# The server starts from CAO_WORKSPACE_ROOT when exactly ZERO or MULTIPLE
# repos are pre-cloned, and from the single repo's directory when exactly ONE
# is — so the common one-repo fleet gets working-directory inheritance with
# no per-call parameters, while multi-repo fleets select a repo per
# assignment via working_directory.
#
# SECURITY: cao-server has no per-request auth by default. Anyone who can reach
# the port can launch agents (command execution) in this container. Restrict
# reachability with Docker networks / k8s NetworkPolicy, or enable CAO's OAuth
# layer (AUTH0_DOMAIN / CAO_AUTH_JWKS_URI).
set -euo pipefail

BIND_HOST="${CAO_BIND_HOST:-0.0.0.0}"
PORT="${CAO_API_PORT:-9889}"

# --- Git credentials + identity (once per node, valid for ANY repo) ---------
if [ -n "${CAO_GIT_TOKEN:-}" ]; then
  GIT_HOST="${CAO_GIT_HOST:-github.com}"
  GIT_USERNAME="${CAO_GIT_USERNAME:-x-access-token}"
  CRED_FILE="${HOME}/.cao-git-credentials"
  # Store the token in a credential-store file rather than embedding it in
  # remote URLs: URLs leak via `git remote -v`, logs, and process lists.
  umask 077
  printf 'https://%s:%s@%s\n' "${GIT_USERNAME}" "${CAO_GIT_TOKEN}" "${GIT_HOST}" > "${CRED_FILE}"
  umask 022
  git config --global credential.helper "store --file=${CRED_FILE}"
  echo "[cao-entrypoint] git credentials configured for ${GIT_HOST}"
fi
git config --global user.name "${CAO_GIT_USER_NAME:-CAO Agent}"
git config --global user.email "${CAO_GIT_USER_EMAIL:-cao-agent@$(hostname)}"
git config --global init.defaultBranch main

# --- Workspace pre-clone (warm cache; agents may clone more at runtime) -----
WORKSPACE_ROOT="${CAO_WORKSPACE_ROOT:-${HOME}/workspace}"
mkdir -p "${WORKSPACE_ROOT}"
SERVER_CWD="${WORKSPACE_ROOT}"
if [ -n "${CAO_WORKSPACE_REPOS:-}" ]; then
  CLONED=0
  LAST_DIR=""
  # Accept commas or whitespace as separators.
  for repo in $(printf '%s' "${CAO_WORKSPACE_REPOS}" | tr ',' ' '); do
    name="$(basename "${repo}" .git)"
    dest="${WORKSPACE_ROOT}/${name}"
    if [ -d "${dest}/.git" ]; then
      echo "[cao-entrypoint] workspace '${name}' already present, fetching"
      git -C "${dest}" fetch --all --prune \
        || echo "[cao-entrypoint] WARN: fetch failed for '${name}' (continuing with existing clone)"
    else
      echo "[cao-entrypoint] cloning ${repo} -> ${dest}"
      # Fail loudly (set -e): a node missing a listed workspace would accept
      # agent placements it cannot honor.
      git clone "${repo}" "${dest}"
    fi
    CLONED=$((CLONED + 1))
    LAST_DIR="${dest}"
  done
  # Single-repo fleets get working-directory inheritance for free by starting
  # the server inside the repo; multi-repo fleets start at the root and pick
  # a repo per assignment via working_directory.
  if [ "${CLONED}" -eq 1 ]; then
    SERVER_CWD="${LAST_DIR}"
  fi
fi

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

echo "[cao-entrypoint] starting cao-server on ${BIND_HOST}:${PORT} (cwd: ${SERVER_CWD})"
cd "${SERVER_CWD}"
exec cao-server --host "${BIND_HOST}" --port "${PORT}"
