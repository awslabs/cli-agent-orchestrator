"""Credentials for the EKS broker's narrow worker-to-supervisor gateway."""

import os

from cli_agent_orchestrator.constants import (
    ELASTIC_RELEASE_TOKEN_ENV,
    ELASTIC_RELEASE_TOKEN_HEADER,
    ELASTIC_WORKER_ID_ENV,
    ELASTIC_WORKER_ID_HEADER,
)


def elastic_worker_gateway_headers() -> dict[str, str]:
    """Return gateway credentials inside an elastic worker, otherwise empty."""
    worker_id = os.environ.get(ELASTIC_WORKER_ID_ENV, "").strip()
    release_token = os.environ.get(ELASTIC_RELEASE_TOKEN_ENV, "").strip()
    if not worker_id or not release_token:
        return {}
    return {
        ELASTIC_WORKER_ID_HEADER: worker_id,
        ELASTIC_RELEASE_TOKEN_HEADER: release_token,
    }
