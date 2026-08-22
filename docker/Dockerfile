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

# --- Provider CLI: Claude Code ------------------------------------------------
#
# Installed here from the public npm registry, which is the whole reason this
# image needs no second layer. The provider used to be kiro-cli, which cannot be
# installed this way: `npm install -g @anthropic-ai/kiro-cli` 404s (the previous
# version of this file recorded that it made the image unbuildable), and the
# real installer produces an ~800 MB tree that had to be vendored in from an
# authenticated local install via a separate Dockerfile.provider. That made the
# image non-redistributable and the build non-reproducible.
#
# It also could not authenticate unattended: `kiro-cli login` offers only
# --license/--identity-provider/--region/--use-device-flow, so every pod needed
# a human at a browser. Claude Code on Bedrock authenticates with SigV4 from the
# pod's own EKS Pod Identity credentials — see k8s/deployment-supervisor.yaml.
#
# Node 22+ is required. bookworm's nodejs is too old, so install from NodeSource.
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/* \
    # Fail the build here rather than at pod startup if the CLI is unusable.
    && claude --version

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

# Mark Claude Code's first-run onboarding as done.
#
# Without this the CLI opens with the interactive THEME PICKER, which is not one
# of the dialogs CAO's startup handler knows how to answer (see
# providers/claude_code.py:_handle_startup_prompts), so `cao launch` never gets a
# REPL and fails with "Claude Code initialization timed out". A container is
# always a first run — nobody has ever completed onboarding in this image.
#
# `hasCompletedOnboarding` is the whole gate; verified by disassembling the
# installed CLI, whose check is `if(!config.hasCompletedOnboarding|| ...)`.
# `lastOnboardingVersion` is written alongside it by the real onboarding flow but
# is never read back, so it is deliberately NOT set here — it would couple this
# image to a CLI version for no effect.
#
# This file lives on the container filesystem, not on the state PVC, so it must
# be baked into the image: seeding it in a running pod is lost on the next
# restart.
RUN printf '{\n  "hasCompletedOnboarding": true\n}\n' > /home/cao/.claude.json \
    && chmod 600 /home/cao/.claude.json

# --- Pre-initialized state seed ------------------------------------------------
#
# `cao init` plus one `cao install` costs ~3.8s and produces a byte-identical
# tree every time. In the elastic k8s topology the state directory is a fresh
# emptyDir per TASK, so that 3.8s is paid on every delegation a participant
# watches — not once at startup. Do the work here instead, and let the entrypoint
# extract it.
#
# A tarball rather than a baked-in directory, because the runtime mounts a volume
# over /home/cao/.cao: anything this layer leaves at CAO_STATE_SEED_ROOT is
# invisible the moment a PVC or emptyDir is mounted there. The seed therefore
# lives OUTSIDE that path and is unpacked into it at start.
#
# Built at the real runtime path rather than a staging dir. Verified that the
# result is relocatable as it stands — the DB is schema-only and no file in the
# seed contains an absolute path — so this costs nothing and means the entrypoint
# never has to care. It is then removed, so the image ships no state at the mount
# point and the entrypoint's "is the state dir empty" check means what it says.
#
# MUST stay above the VOLUME instruction below. Docker discards filesystem
# changes made to a declared VOLUME path in LATER layers, so moving this down
# would silently produce an image whose seed tar exists and whose contents were
# thrown away — the extract then succeeds and seeds nothing.
ENV CAO_STATE_SEED=/home/cao/cao-state-seed.tar \
    CAO_STATE_SEED_ROOT=/home/cao/.cao/state

# Every built-in profile, pinned to claude_code — the only provider this image
# contains. The broker accepts any profile name, so seeding just `developer`
# would leave the others paying the runtime install; seeding all six costs a few
# hundred KB. A profile the seed does not carry, or one requested for another
# provider, still installs at start: the entrypoint matches on the resolved
# `provider:` line, not on mere presence.
RUN set -eux; \
    export CAO_HOME_DIR="${CAO_STATE_SEED_ROOT}"; \
    cao init; \
    for profile in code_supervisor developer memory_manager retrospector reviewer workflow_scout; do \
        cao install "$profile" --provider claude_code; \
    done; \
    tar -cf "${CAO_STATE_SEED}" \
        -C "$(dirname "${CAO_STATE_SEED_ROOT}")" "$(basename "${CAO_STATE_SEED_ROOT}")"; \
    rm -rf "${CAO_STATE_SEED_ROOT}"; \
    # Fail the build here rather than shipping an image whose entrypoint quietly
    # falls back to the slow path on every pod.
    tar -tf "${CAO_STATE_SEED}" | grep -q '^state/agent-store/developer.md$'

# ~/.cao holds SQLite state, logs, and installed profiles — mount a volume/PVC
# here for persistence across container restarts.
VOLUME ["/home/cao/.cao"]

EXPOSE 9889

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${CAO_API_PORT:-9889}/health" || exit 1

ENTRYPOINT ["cao-entrypoint"]
