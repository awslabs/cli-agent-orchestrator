from contextlib import asynccontextmanager
import asyncio
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from cli_agent_orchestrator.utils.database import init_database
from cli_agent_orchestrator.utils.logging import setup_logging
from cli_agent_orchestrator.api.sessions import router as sessions_router
from cli_agent_orchestrator.api.terminals import router as terminals_router
from cli_agent_orchestrator.constants import SERVER_HOST, SERVER_PORT, SERVER_VERSION, CORS_ORIGINS

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info("Starting CLI Agent Orchestrator server...")
    init_database()
    yield
    # Shutdown
    logger.info("Shutting down CLI Agent Orchestrator server...")


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    app = FastAPI(
        title="CLI Agent Orchestrator",
        description="CLI Agent Orchestrator",
        version=SERVER_VERSION,
        lifespan=lifespan,
    )
    
    # Configure CORS for local development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Include routers
    app.include_router(sessions_router)
    app.include_router(terminals_router)
    
    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        return {"status": "ok", "service": "cli-agent-orchestrator"}
    
    logger.info("FastAPI application created successfully")
    return app


def main():
    """Main entry point for cli-agent-orchestrator-server command."""
    setup_logging()
    
    config = uvicorn.Config(
        create_app(),
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level="info"
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
