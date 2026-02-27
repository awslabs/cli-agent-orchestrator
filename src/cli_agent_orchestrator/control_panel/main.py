"""Control Panel FastAPI server - middleware layer between frontend and cao-server."""

import logging
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from cli_agent_orchestrator.constants import API_BASE_URL

logger = logging.getLogger(__name__)

# Control panel server configuration
CONTROL_PANEL_HOST = "localhost"
CONTROL_PANEL_PORT = 8000

# CAO server URL (the actual backend)
CAO_SERVER_URL = API_BASE_URL

# CORS origins for frontend
CONTROL_PANEL_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app = FastAPI(
    title="CAO Control Panel API",
    description="FastAPI interface layer for the CAO frontend control panel",
    version="1.0.0",
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=CONTROL_PANEL_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint for the control panel."""
    try:
        # Also check if cao-server is reachable
        response = requests.get(f"{CAO_SERVER_URL}/health", timeout=5)
        cao_status = "healthy" if response.status_code == 200 else "unhealthy"
    except Exception:
        cao_status = "unreachable"

    return {
        "status": "healthy",
        "cao_server_status": cao_status,
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_to_cao(request: Request, path: str) -> Response:
    """
    Proxy all requests to the cao-server.
    This acts as a middleware layer between the frontend and the actual CAO API.
    """
    # Construct the upstream URL
    upstream_url = f"{CAO_SERVER_URL}/{path}"

    # Forward query parameters
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # Prepare headers
    headers = {
        "Content-Type": "application/json",
    }

    # Get request body if present
    body = None
    if request.method in ["POST", "PUT", "PATCH"]:
        try:
            body = await request.body()
        except Exception:
            pass

    try:
        # Make request to cao-server
        response = requests.request(
            method=request.method,
            url=upstream_url,
            headers=headers,
            data=body,
            timeout=30,
        )

        # Return the response from cao-server
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers),
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"Error proxying request to cao-server: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach cao-server: {str(e)}",
        )


def main() -> None:
    """Run the control panel server."""
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    logger.info(f"Starting CAO Control Panel server on {CONTROL_PANEL_HOST}:{CONTROL_PANEL_PORT}")
    logger.info(f"Proxying requests to cao-server at {CAO_SERVER_URL}")

    uvicorn.run(
        app,
        host=CONTROL_PANEL_HOST,
        port=CONTROL_PANEL_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
