# CAO server image for the elastic Kubernetes topology.
ARG BASE_PROVIDER_IMAGE

FROM python:3.12-slim AS builder

WORKDIR /build
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY scripts/hatch_build_tui_tag.py scripts/
COPY src/ src/
RUN python -m build --wheel --outdir /build/dist

FROM ${BASE_PROVIDER_IMAGE}
USER root
RUN --mount=from=builder,source=/build/dist,target=/tmp/dist \
    pip install --no-cache-dir --force-reinstall --no-deps /tmp/dist/*.whl
USER cao
