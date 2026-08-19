# CAO node image — one container = one CAO node (cao-server + tmux + agents).
#
# Both the supervisor pod and worker pods run this same image; they differ only
# in env/config. In the k8s one-agent-per-pod topology (see k8s/):
#   - the single SUPERVISOR pod runs the code_supervisor agent and delegates
#     work to worker pods via the assign/handoff `target_host` parameter;
#   - each WORKER pod hosts exactly one agent (CAO_MAX_TERMINALS=1) and
#     rejects further placements, so pods stay one-agent-sized.
# See docs/fleet-coordinator.md for the cross-node architecture this image is
# a building block of.
#
# Build (from repo root):
#   docker build -t cao-node:dev -f docker/Dockerfile .
#
# Run a worker node:
#   docker run -d --name cao-worker-0 -p 9889:9889 \
#     -e CAO_ALLOWED_HOSTS=cao-worker-0 cao-node:dev

# --- Stage 1: build the pure-Python wheel -----------------------------------
# The TUI wheel-tag hook (scripts/hatch_build_tui_tag.py) is inert when no Rust
# binary is staged, so this produces a py3-none-any wheel without a Rust toolchain.
FROM python:3.12-slim AS builder

WORKDIR /build
RUN pip install --no-cache-dir build

# Only what the wheel build needs (keeps cache stable across doc/web changes).
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY scripts/hatch_build_tui_tag.py scripts/
COPY src/ src/

RUN python -m build --wheel --outdir /build/dist

# --- Stage 2: runtime --------------------------------------------------------
FROM python:3.12-slim

# tmux >= 3.3 required by CAO (bookworm ships 3.3a). git for agent workspaces,
# curl for healthchecks, procps for provider process detection.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tmux \
        git \
        curl \
        ca-certificates \
        procps \
    && rm -rf /var/lib/apt/lists/*

# No provider CLI is baked in. The previous `npm install -g
# @anthropic-ai/kiro-cli` here made this image UNBUILDABLE: that package does
# not exist on the public npm registry (404), so the build failed at this
# layer and produced nothing. nodejs/npm were installed only to serve it and
# are dropped with it.
#
# Ship a real provider by layering onto this image, e.g.:
#   FROM <this-image>
#   COPY --chmod=755 path/to/kiro-cli /usr/local/bin/kiro-cli
# or mount the binary in (see k8s/ for a hostPath/volume example) and set the
# profile's provider accordingly. The credentials-free mock_cli below is what
# the k8s manifests select by default, so the fleet is testable as-is.

# Never run the (unauthenticated) server as root: anyone who reaches the port
# gets command execution as this user.
RUN useradd --create-home --shell /bin/bash cao

RUN --mount=from=builder,source=/build/dist,target=/tmp/dist \
    pip install --no-cache-dir /tmp/dist/*.whl

# Credentials-free mock provider binary for smoke tests (`--provider mock_cli`).
# Real deployments install an authenticated provider CLI on top of this image.
COPY --chmod=755 test/providers/fixtures/bin/mock_cli /usr/local/bin/mock_cli

COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/cao-entrypoint

USER cao
WORKDIR /home/cao/workspace
RUN mkdir -p /home/cao/workspace

# ~/.cao holds SQLite state, logs, and installed profiles — mount a volume/PVC
# here for persistence across container restarts.
VOLUME ["/home/cao/.cao"]

EXPOSE 9889

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${CAO_API_PORT:-9889}/health" || exit 1

ENTRYPOINT ["cao-entrypoint"]
