# CAO node image — one container = one CAO node (cao-server + tmux + agents).
#
# Both the supervisor pod and worker pods run this same image; they differ only
# in env/config. In the EKS one-agent-per-pod topology (see this directory):
#   - the single SUPERVISOR pod runs the code_supervisor agent and delegates
#     work to worker pods via the assign/handoff `target_host` parameter;
#   - each WORKER pod hosts exactly one agent (CAO_MAX_TERMINALS=1) and
#     rejects further placements, so pods stay one-agent-sized.
# See docs/fleet-coordinator.md for the cross-node architecture this image is
# a building block of.
#
# Build (from repo root):
#   docker build -t cao-node:dev \
#     -f examples/cao-clusters/kubernetes/eks/Dockerfile .
#
# Run a worker node:
#   docker run -d --name cao-worker-0 -p 9889:9889 \
#     -e CAO_ALLOWED_HOSTS=cao-worker-0 cao-node:dev

# The runtime base image, and the reason this file is not Claude-Code-only.
# Declared before the first FROM so it is in scope for stage 2's.
#
# Default builds a self-contained Claude Code image from public npm — no second
# image to produce, and reproducible from this repository alone. Point it at a
# base image that already carries a different provider CLI, together with
# INSTALL_CLAUDE_CODE=0 below, to build that provider's node instead:
#
#   docker build -f examples/cao-clusters/kubernetes/eks/Dockerfile \
#     --build-arg BASE_PROVIDER_IMAGE=<image-with-kiro-cli> \
#     --build-arg INSTALL_CLAUDE_CODE=0 \
#     --build-arg CAO_PROFILE_PROVIDER=<provider-name> .
ARG BASE_PROVIDER_IMAGE=python:3.12-slim

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
FROM ${BASE_PROVIDER_IMAGE}

# A provider base image may run as its own user; the layers below need root.
USER root

# tmux >= 3.3 required by CAO (bookworm ships 3.3a). git for agent workspaces,
# curl for healthchecks, procps for provider process detection.
#
# Unconditional, unlike the provider layer below: CAO needs all five whatever
# provider runs. That does assume a Debian-family base — an apt-less
# BASE_PROVIDER_IMAGE fails here, loudly, which is better than a node that
# starts and then cannot open a tmux session.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tmux \
        git \
        curl \
        ca-certificates \
        procps \
    && rm -rf /var/lib/apt/lists/*

# --- Provider CLI: Claude Code (default; opt out for another provider) --------
#
# Set INSTALL_CLAUDE_CODE=0 when BASE_PROVIDER_IMAGE already carries a provider
# CLI, and this layer is skipped entirely — the image is then that provider's
# node, with CAO installed on top.
#
# Claude Code is the default because it installs from the public npm registry,
# so the image is reproducible from this repository alone with no second image
# to produce, and it authenticates with SigV4 from the pod's own EKS Pod
# Identity credentials, so nothing needs to reach Secrets Manager.
#
# Whichever provider you use, it must be able to authenticate WITHOUT a human:
# every pod here is disposable and nobody is at a browser when one starts. A CLI
# whose only login path is an interactive device flow cannot run in this
# topology, however the credential reaches the pod.
#
# Node 22+ is required. bookworm's nodejs is too old, so install from NodeSource.
# Pin Claude Code because this image is rebuilt for every CAO release and a
# mutable provider version can break an otherwise unchanged cluster. Version
# 2.1.250 fails before its first Bedrock request with `O is not defined`;
# 2.1.235 was validated against this example's Bedrock configuration.
ARG INSTALL_CLAUDE_CODE=1
ARG CLAUDE_CODE_VERSION=2.1.235
RUN if [ "${INSTALL_CLAUDE_CODE}" != "1" ]; then \
      echo "skipping Claude Code install; provider comes from BASE_PROVIDER_IMAGE"; \
    else \
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/* \
    # Fail the build here rather than at pod startup if the CLI is unusable.
    && claude --version; \
    fi

# --- Provider CLI: kiro-cli (opt in) ------------------------------------------
#
#   docker build -f examples/cao-clusters/kubernetes/eks/Dockerfile \
#     --build-arg INSTALL_KIRO_CLI=1 --build-arg INSTALL_CLAUDE_CODE=0 \
#     --build-arg CAO_PROFILE_PROVIDER=kiro_cli .
#
# Off by default because of what it costs: 722MB compressed, 1.1GB installed
# (kiro-cli-chat alone is 943MB) against roughly 100MB for Claude Code. The node
# pays that once per image, but a node joining the group pays it before the first
# worker on it can start.
#
# The MUSL build is deliberate, not a preference. The aarch64 *gnu* archive
# requires glibc 2.39 and bookworm ships 2.36, so its own installer refuses with
# "try installing the musl version"; for a musl target the installer skips the
# glibc check entirely. The archive URL uses $(uname -m), so this builds on either
# architecture -- x86_64 and aarch64 musl archives both exist at this version
# (693MB and 689MB) -- and musl is kept for both rather than special-casing one.
#
# The two variables are what make this unattended: Q_INSTALL_GLOBAL installs to
# /usr/local/bin instead of ~/.local/bin, and Q_SKIP_SETUP skips
# `integrations install dotfiles` and `setup --global` — the steps that expect a
# shell to rewrite and a human to answer.
#
# Pinned rather than `latest`, which the release bucket also serves: a mutable
# tag is treated as a defect everywhere else here (see kustomization.yaml), and
# a provider CLI that changes under a rebuild would be the least visible version
# of that mistake.
#
# NOTE: this gets the CLI into the image; it does NOT make it usable in a
# disposable pod. kiro-cli's API key authenticates `chat --no-interactive` only,
# and CAO drives the interactive TUI. See "Provider credentials" in README.md.
ARG INSTALL_KIRO_CLI=0
ARG KIRO_CLI_VERSION=2.19.1
RUN if [ "${INSTALL_KIRO_CLI}" = "1" ]; then \
      set -eux; \
      apt-get update; \
      apt-get install -y --no-install-recommends unzip; \
      curl --proto '=https' --tlsv1.2 -fsSL -o /tmp/kirocli.zip \
        "https://desktop-release.q.us-east-1.amazonaws.com/${KIRO_CLI_VERSION}/kirocli-$(uname -m)-linux-musl.zip"; \
      unzip -q /tmp/kirocli.zip -d /tmp; \
      Q_INSTALL_GLOBAL=1 Q_SKIP_SETUP=1 sh /tmp/kirocli/install.sh; \
      rm -rf /tmp/kirocli.zip /tmp/kirocli; \
      apt-get purge -y unzip; \
      apt-get autoremove -y; \
      rm -rf /var/lib/apt/lists/*; \
      # Fail the build here rather than at pod startup. This also catches the
      # musl-on-glibc question: if the binary cannot run on this base, it cannot
      # report a version either.
      kiro-cli --version; \
    fi

# Never run the (unauthenticated) server as root: anyone who reaches the port
# gets command execution as this user.
#
# Idempotent because a provider BASE_PROVIDER_IMAGE may already define this
# user; a bare `useradd` would fail the build on one that does.
RUN id -u cao >/dev/null 2>&1 || useradd --create-home --shell /bin/bash cao

RUN --mount=from=builder,source=/build/dist,target=/tmp/dist \
    pip install --no-cache-dir /tmp/dist/*.whl

# Credentials-free mock provider binary for smoke tests (`--provider mock_cli`).
# Real deployments install an authenticated provider CLI on top of this image.
COPY --chmod=755 test/providers/fixtures/bin/mock_cli /usr/local/bin/mock_cli

COPY --chmod=755 examples/cao-clusters/kubernetes/eks/entrypoint.sh /usr/local/bin/cao-entrypoint

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

# Every built-in profile, pinned to the provider this image contains. The
# default remains claude_code; an alternate-provider build must set this
# alongside the provider install args so the image never seeds profiles for a
# CLI it does not contain. The broker accepts any profile name, so seeding just
# `developer` would leave the others paying the runtime install; seeding all six
# costs a few hundred KB. A profile the seed does not carry, or one requested
# for another provider, still installs at start: the entrypoint matches on the
# resolved `provider:` line, not on mere presence.
ARG CAO_PROFILE_PROVIDER=claude_code
RUN set -eux; \
    export CAO_HOME_DIR="${CAO_STATE_SEED_ROOT}"; \
    cao init; \
    for profile in code_supervisor developer memory_manager retrospector reviewer workflow_scout; do \
        cao install "$profile" --provider "${CAO_PROFILE_PROVIDER}"; \
    done; \
    tar -cf "${CAO_STATE_SEED}" \
        -C "$(dirname "${CAO_STATE_SEED_ROOT}")" "$(basename "${CAO_STATE_SEED_ROOT}")"; \
    rm -rf "${CAO_STATE_SEED_ROOT}"; \
    # Fail the build here rather than shipping an image whose entrypoint quietly
    # falls back to the slow path on every pod, or whose provider pin drifted.
    tar -xOf "${CAO_STATE_SEED}" state/agent-store/developer.md \
      | grep -qE "^provider:[[:space:]]*${CAO_PROFILE_PROVIDER}[[:space:]]*$"

# ~/.cao holds SQLite state, logs, and installed profiles — mount a volume/PVC
# here for persistence across container restarts.
VOLUME ["/home/cao/.cao"]

EXPOSE 9889

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${CAO_API_PORT:-9889}/health" || exit 1

ENTRYPOINT ["cao-entrypoint"]
