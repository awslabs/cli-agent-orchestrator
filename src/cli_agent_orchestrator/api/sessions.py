"""
Session API endpoints for Core Session Management and Bulk Session Operations
"""
from typing import List
from fastapi import APIRouter, HTTPException, status
from cli_agent_orchestrator.api.models import CreateSessionRequest, SessionResponse, BulkSessionResponse
from cli_agent_orchestrator.core.session_manager import session_manager

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=BulkSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(request: CreateSessionRequest) -> BulkSessionResponse:
    """Create a new session with terminals. Sessions must have at least one terminal."""
    try:
        # Limit to single terminal for performance
        if len(request.terminals) > 1:
            raise ValueError("Session creation limited to 1 terminal")
        
        # Create session with terminal
        terminal_config = {
            "provider": request.terminals[0].provider,
            "name": request.terminals[0].name,
            "agent_profile": request.terminals[0].agent_profile
        }
        session, terminals = await session_manager.create_session_with_terminals(
            request.name, [terminal_config]
        )
        return BulkSessionResponse(
            id=session.id,
            terminals=[
                {
                    "id": terminal.id,
                    "name": terminal.name,
                    "provider": terminal.provider,
                    "status": terminal.status
                }
                for terminal in terminals
            ]
        )
    except ValueError as e:
        if "already exists" in str(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Session with name '{request.name}' already exists"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create session: {str(e)}"
        )


@router.get("", response_model=List[SessionResponse])
async def list_sessions() -> List[SessionResponse]:
    """List all active sessions."""
    try:
        sessions = await session_manager.list_sessions()
        return [
            SessionResponse(
                id=session.id,
                name=session.name,
                status=session.status
            )
            for session in sessions
        ]
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list sessions: {str(e)}"
        )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: str) -> None:
    """Delete a session."""
    try:
        await session_manager.delete_session(session_id)
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session '{session_id}' not found"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete session: {str(e)}"
        )
