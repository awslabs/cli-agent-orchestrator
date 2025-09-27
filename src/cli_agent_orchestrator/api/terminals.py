"""
Terminal API endpoints for Basic Terminal Management and Terminal I/O Operations
"""
import asyncio
import logging
import os
import tempfile
from typing import List
from fastapi import APIRouter, HTTPException, status, Query, WebSocket, WebSocketDisconnect
import aiofiles
from cli_agent_orchestrator.api.models import (
    CreateTerminalRequest, TerminalResponse, TerminalInputRequest, 
    TerminalOutputResponse, TerminalScriptResponse
)
from cli_agent_orchestrator.core.session_manager import session_manager
from cli_agent_orchestrator.utils.session import get_terminal_log_path

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terminals"])


@router.post("/sessions/{session_id}/terminals", response_model=TerminalResponse, status_code=status.HTTP_201_CREATED)
async def create_terminal(session_id: str, request: CreateTerminalRequest) -> TerminalResponse:
    """Create a new terminal in a session."""
    try:
        terminal = await session_manager.create_terminal(session_id, request.provider, request.name, request.agent_profile)
        return TerminalResponse(
            id=terminal.id,
            name=terminal.name,
            provider=terminal.provider,
            status=terminal.status
        )
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
            detail=f"Failed to create terminal: {str(e)}"
        )


@router.get("/sessions/{session_id}/terminals", response_model=List[TerminalResponse])
async def list_terminals(session_id: str) -> List[TerminalResponse]:
    """List all terminals in a session."""
    try:
        terminals = await session_manager.list_terminals(session_id)
        return [
            TerminalResponse(
                id=terminal.id,
                name=terminal.name,
                provider=terminal.provider,
                status=terminal.status
            )
            for terminal in terminals
        ]
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
            detail=f"Failed to list terminals: {str(e)}"
        )


@router.delete("/terminals/{terminal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_terminal(terminal_id: str) -> None:
    """Delete a terminal."""
    try:
        await session_manager.delete_terminal(terminal_id)
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Terminal '{terminal_id}' not found"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete terminal: {str(e)}"
        )


@router.get("/terminals/{terminal_id}", response_model=TerminalResponse)
async def get_terminal(terminal_id: str) -> TerminalResponse:
    """Get terminal details including status."""
    try:
        terminal = await session_manager.get_terminal(terminal_id)
        return TerminalResponse(
            id=terminal.id,
            name=terminal.name,
            provider=terminal.provider,
            status=terminal.status
        )
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Terminal '{terminal_id}' not found"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get terminal: {str(e)}"
        )


@router.post("/terminals/{terminal_id}/input", status_code=status.HTTP_200_OK)
async def send_terminal_input(terminal_id: str, request: TerminalInputRequest) -> dict:
    """Send input to a terminal."""
    if not request.message.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message cannot be empty"
        )
    
    try:
        await session_manager.send_terminal_input(terminal_id, request.message)
        return {"status": "success", "message": "Input sent to terminal"}
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Terminal '{terminal_id}' not found"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send input: {str(e)}"
        )


@router.get("/terminals/{terminal_id}/output", response_model=TerminalOutputResponse)
async def get_terminal_output(
    terminal_id: str, 
    mode: str = Query("full", pattern="^(full|last)$")
) -> TerminalOutputResponse:
    """Get terminal output."""
    try:
        output = await session_manager.get_terminal_output(terminal_id, mode)
        return TerminalOutputResponse(output=output, mode=mode)
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Terminal '{terminal_id}' not found"
            )
        if "invalid mode" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid mode. Must be 'full' or 'last'"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get terminal output: {str(e)}"
        )


@router.get("/terminals/{terminal_id}/script", response_model=TerminalScriptResponse)
async def get_terminal_script(terminal_id: str) -> TerminalScriptResponse:
    """Get terminal script for frontend attachment."""
    try:
        script = await session_manager.get_terminal_script(terminal_id)
        return TerminalScriptResponse(script=script, terminal_id=terminal_id)
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Terminal '{terminal_id}' not found"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get terminal script: {str(e)}"
        )

# TODO: Most of the logic should be abstracted out to the session manager so that this function is thinner
@router.websocket("/terminals/{terminal_id}/stream")
async def websocket_terminal_stream(websocket: WebSocket, terminal_id: str):
    """WebSocket endpoint for streaming terminal output."""
    await websocket.accept()
    logger.info(f"WebSocket connected for terminal {terminal_id}")
    
    try:
        # Get terminal info
        terminal = await session_manager.get_terminal(terminal_id)
        session_id = terminal.session_id
        window_name = terminal.name
        
        # Get log file path
        log_file = get_terminal_log_path(terminal_id)
        if not os.path.isabs(log_file):
            log_file = os.path.abspath(log_file)
        
        logger.info(f"Terminal info: session={session_id}, window={window_name}, log_file={log_file}")
        
        # Send complete log history first
        log_content_sent = ""
        if os.path.exists(log_file):
            logger.info(f"Loading complete log history from: {log_file}")
            with open(log_file, 'rb') as f:
                log_content = f.read()
                if log_content:
                    log_content_sent = log_content.decode('utf-8', errors='replace')
                    if log_content_sent.strip():
                        logger.info(f"Sending log history: {len(log_content_sent)} chars")
                        await websocket.send_text(log_content_sent)
        
        # Track what we've already sent
        last_log_size = len(log_content_sent.encode('utf-8', errors='replace'))
        
        # Start continuous streaming
        while True:
            # Check for incoming messages (commands from client)
            try:
                command = await asyncio.wait_for(websocket.receive_text(), timeout=0.2)
                logger.info(f"Received command for terminal {terminal_id}: {command}")
                
                # Send command to terminal
                await session_manager.send_terminal_input(terminal_id, command)
                
            except asyncio.TimeoutError:
                # No message received, continue with streaming
                pass
            except Exception as e:
                logger.error(f"WebSocket command error: {e}")
                break
            
            # Stream new content from log file
            if os.path.exists(log_file):
                current_log_size = os.path.getsize(log_file)
                if current_log_size > last_log_size:
                    with open(log_file, 'rb') as f:
                        f.seek(last_log_size)
                        new_content = f.read(current_log_size - last_log_size)
                        if new_content:
                            content_str = new_content.decode('utf-8', errors='replace')
                            if content_str.strip():
                                logger.debug(f"Sending new log content: {len(content_str)} chars")
                                await websocket.send_text(content_str)
                                last_log_size = current_log_size
            
            await asyncio.sleep(0.2)
    
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for terminal {terminal_id}")
    except ValueError as e:
        if "not found" in str(e):
            logger.error(f"Terminal not found: {terminal_id}")
            await websocket.close(code=4004)
        else:
            logger.error(f"WebSocket error for terminal {terminal_id}: {e}")
            await websocket.close(code=4000)
    except Exception as e:
        logger.error(f"WebSocket error for terminal {terminal_id}: {e}", exc_info=True)
        await websocket.close(code=4000)
    finally:
        logger.info(f"WebSocket cleanup for terminal {terminal_id}")
