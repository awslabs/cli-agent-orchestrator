"""Session manager for CLI Agent Orchestrator."""

import logging
import os
import uuid
from typing import Dict, List, Optional

from cli_agent_orchestrator.adapters.tmux import TmuxAdapter
from cli_agent_orchestrator.adapters.database import SessionLocal, SessionModel, TerminalModel
from cli_agent_orchestrator.models.session import Session, SessionStatus
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.registry import provider_registry
from cli_agent_orchestrator.utils.session import get_terminal_log_path

logger = logging.getLogger(__name__)


class SessionManager:
    """Session manager for CRUD operations in CLI Agent Orchestrator."""
    
    def __init__(self):
        self.tmux_adapter = TmuxAdapter()
        self.sessions: Dict[str, Session] = {}  # In-memory cache
        self.provider_registry = provider_registry
    
    # Removed create_session() - sessions must be created with terminals
    # Use create_session_with_terminals() instead
    
    async def list_sessions(self) -> List[Session]:
        """List all tmux sessions."""
        try:
            # Get sessions from tmux
            tmux_sessions = self.tmux_adapter.list_sessions()
            
            # Filter only cao sessions and extract original names
            cao_sessions = []
            for session in tmux_sessions:
                if session.id.startswith("cao-"):
                    # Get session details from database using session ID
                    with SessionLocal() as db:
                        db_session = db.query(SessionModel).filter(SessionModel.id == session.id).first()
                        if db_session:
                            cao_session = Session(
                                id=session.id,
                                name=db_session.name,  # Use name from database
                                status=session.status
                            )
                            cao_sessions.append(cao_session)
                            self.sessions[session.id] = cao_session
            
            return cao_sessions
            
        except Exception as e:
            logger.error(f"Failed to list sessions: {e}")
            return []
    
    async def get_session(self, session_id: str) -> Optional[Session]:
        """Get session by ID."""
        try:
            # Refresh session list to get current state
            await self.list_sessions()
            return self.sessions.get(session_id)
            
        except Exception as e:
            logger.error(f"Failed to get session {session_id}: {e}")
            return None
    
    async def get_session_terminals(self, session_name: str) -> List[Terminal]:
        """Get all terminals for a session."""
        try:
            # Get terminals from tmux
            terminals = self.tmux_adapter.get_session_terminals(session_name)
            
            # Update database
            with SessionLocal() as db:
                for terminal in terminals:
                    db_terminal = db.query(TerminalModel).filter_by(id=terminal.id).first()
                    if not db_terminal:
                        db_terminal = TerminalModel(
                            id=terminal.id,
                            session_id=terminal.session_id,
                            name=terminal.name,
                            provider=terminal.provider.value if terminal.provider else None,
                            status=terminal.status
                        )
                        db.add(db_terminal)
                    else:
                        db_terminal.status = terminal.status
                db.commit()
            
            return terminals
            
        except Exception as e:
            logger.error(f"Failed to get terminals for session {session_name}: {e}")
            return []
    
    async def delete_session(self, session_id: str) -> None:
        """Delete tmux session."""
        try:
            # Check if session exists
            session = await self.get_session(session_id)
            if not session:
                raise ValueError(f"Session '{session_id}' not found")
            
            # Kill tmux session
            if not self.tmux_adapter.kill_session(session_id):
                raise RuntimeError(f"Failed to kill tmux session: {session_id}")
            
            # Remove from database
            with SessionLocal() as db:
                # Delete terminals first (cascade)
                db.query(TerminalModel).filter_by(session_id=session_id).delete()
                # Delete session
                db.query(SessionModel).filter_by(id=session_id).delete()
                db.commit()
            
            # Remove from cache
            self.sessions.pop(session_id, None)
            
            logger.info(f"Deleted session: {session_id}")
            
        except Exception as e:
            logger.error(f"Failed to delete session {session_id}: {e}")
            raise
    
    async def create_terminal(self, session_id: str, provider: Optional[str] = None, name: Optional[str] = None, agent_profile: Optional[str] = None) -> Terminal:
        """Create new terminal (tmux window) in session with provider integration."""
        try:
            # Check if session exists
            session = await self.get_session(session_id)
            if not session:
                raise ValueError(f"Session '{session_id}' not found")
            
            # Generate terminal ID
            terminal_id = f"terminal-{uuid.uuid4().hex[:8]}"
            
            # Create tmux window with agent profile for naming
            window_name = self.tmux_adapter.create_window(session_id, name, agent_profile, terminal_id)
            if window_name is None:
                raise RuntimeError(f"Failed to create tmux window in session: {session_id}")
            
            # Create provider instance
            provider_instance = provider_registry.create_provider(
                provider, terminal_id, session_id, window_name, agent_profile
            )
            
            # Initialize provider and get status
            if provider_instance:
                await provider_instance.initialize()
                terminal_status = await provider_instance.get_status()
            else:
                raise ValueError(f"Terminal created without provider - status undefined for terminal {terminal_id}")
            
            # Start continuous logging with pipe-pane
            pipe_file = get_terminal_log_path(terminal_id)
            os.makedirs(os.path.dirname(pipe_file), exist_ok=True)
            pipe_started = self.tmux_adapter.start_pipe(session_id, window_name, pipe_file)
            if not pipe_started:
                logger.warning(f"Failed to start pipe for terminal {terminal_id}")
            
            # Create terminal model
            terminal = Terminal(
                id=terminal_id,
                session_id=session_id,
                name=name or window_name,
                provider=provider,
                status=terminal_status
            )
            
            # Store in database with window name for tracking
            db = SessionLocal()
            try:
                db_terminal = TerminalModel(
                    id=terminal.id,
                    session_id=terminal.session_id,
                    name=f"{window_name}:{terminal.name}",  # Store window name
                    provider=terminal.provider,
                    status=terminal.status
                )
                db.add(db_terminal)
                db.commit()
            finally:
                db.close()
            
            logger.info(f"Created terminal: {terminal_id} in session: {session_id} with provider: {provider}, pipe: {pipe_started}")
            return terminal
            
        except Exception as e:
            logger.error(f"Failed to create terminal in session {session_id}: {e}")
            raise
    
    
    async def list_terminals(self, session_id: str) -> List[Terminal]:
        """List all terminals in a session."""
        try:
            # Check if session exists
            session = await self.get_session(session_id)
            if not session:
                raise ValueError(f"Session '{session_id}' not found")
            
            # Get terminals from database
            db = SessionLocal()
            try:
                terminal_records = db.query(TerminalModel).filter(
                    TerminalModel.session_id == session_id
                ).all()
                
                terminals = []
                for record in terminal_records:
                    # Get live status from provider and update database
                    provider_instance = provider_registry.get_provider(record.id)
                    if not provider_instance:
                        raise ValueError(f"No provider instance found for terminal {record.id}")
                    
                    current_status = await provider_instance.get_status()
                    # Update database with live status
                    record.status = current_status
                    db.commit()
                    
                    # Extract clean name (remove window index prefix)
                    clean_name = record.name
                    if ":" in record.name:
                        clean_name = record.name.split(":", 1)[1]
                    
                    terminal = Terminal(
                        id=record.id,
                        session_id=record.session_id,
                        name=clean_name,
                        provider=record.provider,
                        status=current_status
                    )
                    terminals.append(terminal)
                
                return terminals
            finally:
                db.close()
            
        except Exception as e:
            logger.error(f"Failed to list terminals for session {session_id}: {e}")
            raise
    
    async def delete_terminal(self, terminal_id: str) -> None:
        """Delete terminal (tmux window) and clean up provider."""
        try:
            # Clean up provider first
            await provider_registry.cleanup_provider(terminal_id)
            
            # Get terminal from database
            db = SessionLocal()
            try:
                terminal_record = db.query(TerminalModel).filter(
                    TerminalModel.id == terminal_id
                ).first()
                
                if not terminal_record:
                    raise ValueError(f"Terminal '{terminal_id}' not found")
                
                session_id = terminal_record.session_id
                
                # Extract window name from stored name (format: "window_name:display_name")
                if ":" in terminal_record.name:
                    window_name = terminal_record.name.split(":")[0]
                    
                    # Stop pipe before killing window
                    pipe_stopped = self.tmux_adapter.stop_pipe(session_id, window_name)
                    if not pipe_stopped:
                        logger.warning(f"Failed to stop pipe for terminal {terminal_id}")
                    
                    self.tmux_adapter.kill_window(session_id, window_name)
                
                # Remove from database
                db.delete(terminal_record)
                db.commit()
                
            finally:
                db.close()
            
            logger.info(f"Deleted terminal: {terminal_id}")
            
        except Exception as e:
            logger.error(f"Failed to delete terminal {terminal_id}: {e}")
            raise
    
    async def create_session_with_terminals(self, name: str, terminals_config: List[dict]) -> tuple[Session, List[Terminal]]:
        """Create session with multiple terminals in one operation."""
        try:
            # Limit to single terminal for performance
            if len(terminals_config) > 1:
                raise ValueError("Bulk session creation limited to 1 terminal")
            
            # Get first terminal config to use as initial window
            terminal_config = terminals_config[0]
            provider = terminal_config.get("provider")
            terminal_name = terminal_config.get("name")
            agent_profile = terminal_config.get("agent_profile")
            
            # Generate window name for the initial terminal
            if not terminal_name:
                if agent_profile:
                    window_id = uuid.uuid4().hex[:4]
                    terminal_name = f"{agent_profile}-{window_id}"
                else:
                    raise ValueError("Terminal name or agent_profile is required")
            
            # Create session with the terminal window as the initial window
            session_id = f"cao-{uuid.uuid4().hex[:8]}"
            terminal_id = f"terminal-{uuid.uuid4().hex[:8]}"
            
            # Check for duplicate session name in database first
            with SessionLocal() as db:
                existing_session = db.query(SessionModel).filter(SessionModel.name == name).first()
                if existing_session:
                    raise ValueError(f"Session with name '{name}' already exists")
            
            # Create tmux session with the terminal window as initial window
            try:
                self.tmux_adapter.create_session_with_terminal(session_id, terminal_name, terminal_id)
            except Exception as e:
                if "already exists" in str(e).lower():
                    raise ValueError(f"Session with ID '{session_id}' already exists")
                raise
            
            # Create session model
            session_model = Session(
                id=session_id,
                name=name,
                status=SessionStatus.DETACHED
            )
            
            # Store session in database
            with SessionLocal() as db:
                db_session = SessionModel(
                    id=session_model.id,
                    name=session_model.name,
                    status=session_model.status
                )
                db.add(db_session)
                db.commit()
            
            # Store in memory cache
            self.sessions[session_id] = session_model
            
            # Create terminal model for the initial window
            terminal = Terminal(
                id=terminal_id,
                session_id=session_id,
                name=terminal_name,
                status=TerminalStatus.IDLE,
                provider=provider
            )
            
            # Initialize provider if specified
            if provider:
                try:
                    provider_instance = self.provider_registry.create_provider(
                        ProviderType(provider), terminal_id, session_id, terminal_name, agent_profile
                    )
                    if provider_instance:
                        await provider_instance.initialize()
                        terminal.status = await provider_instance.get_status()
                        terminal.provider = provider
                except Exception as e:
                    logger.error(f"Failed to initialize provider {provider} for terminal {terminal_id}: {e}")
                    terminal.status = TerminalStatus.ERROR
            
            # Store terminal in database
            with SessionLocal() as db:
                db_terminal = TerminalModel(
                    id=terminal.id,
                    session_id=terminal.session_id,
                    name=terminal.name,
                    status=terminal.status,
                    provider=terminal.provider
                )
                db.add(db_terminal)
                db.commit()
            
            return session_model, [terminal]
            
        except Exception as e:
            logger.error(f"Failed to create session with terminals {name}: {e}")
            raise

    async def get_terminal(self, terminal_id: str) -> Terminal:
        """Get terminal details with current status."""
        try:
            # Get terminal from database
            db = SessionLocal()
            try:
                terminal_record = db.query(TerminalModel).filter(
                    TerminalModel.id == terminal_id
                ).first()
                
                if not terminal_record:
                    raise ValueError(f"Terminal '{terminal_id}' not found")
                
                # Get provider instance and current status
                provider_instance = provider_registry.get_provider(terminal_id)
                if provider_instance:
                    current_status = await provider_instance.get_status()
                else:
                    current_status = terminal_record.status
                
                # Extract clean name (remove window index prefix)
                clean_name = terminal_record.name
                if ":" in terminal_record.name:
                    clean_name = terminal_record.name.split(":", 1)[1]
                
                terminal = Terminal(
                    id=terminal_record.id,
                    session_id=terminal_record.session_id,
                    name=clean_name,
                    provider=terminal_record.provider,
                    status=current_status
                )
                
                return terminal
                
            finally:
                db.close()
            
        except Exception as e:
            logger.error(f"Failed to get terminal {terminal_id}: {e}")
            raise

    async def send_terminal_input(self, terminal_id: str, message: str) -> None:
        """Send input to a terminal."""
        try:
            # Get terminal from database
            db = SessionLocal()
            try:
                terminal_record = db.query(TerminalModel).filter(
                    TerminalModel.id == terminal_id
                ).first()
                
                if not terminal_record:
                    raise ValueError(f"Terminal '{terminal_id}' not found")
                
                session_id = terminal_record.session_id
                
                # Extract window name from stored name
                if ":" in terminal_record.name:
                    window_name = terminal_record.name.split(":")[0]
                else:
                    window_name = terminal_record.name
                
                # Send input to tmux window (common for all providers)
                self.tmux_adapter.send_keys(session_id, window_name, message)
                
            finally:
                db.close()
            
            logger.info(f"Sent input to terminal: {terminal_id}")
            
        except Exception as e:
            logger.error(f"Failed to send input to terminal {terminal_id}: {e}")
            raise


    
    async def get_terminal_output(self, terminal_id: str, mode: str = "full") -> str:
        """Get terminal output."""
        try:
            # Get terminal from database
            db = SessionLocal()
            try:
                terminal_record = db.query(TerminalModel).filter(
                    TerminalModel.id == terminal_id
                ).first()
                
                if not terminal_record:
                    raise ValueError(f"Terminal '{terminal_id}' not found")
                
                session_id = terminal_record.session_id
                
                # Extract window name from stored name
                if ":" in terminal_record.name:
                    window_name = terminal_record.name.split(":")[0]
                else:
                    window_name = terminal_record.name
                
                # Get output from tmux
                if mode == "full":
                    output = self.tmux_adapter.get_window_history(session_id, window_name)
                elif mode == "last":
                    # Check if terminal has a provider for smart last message extraction
                    provider_instance = provider_registry.get_provider(terminal_id)
                    if not provider_instance:
                        raise ValueError(f"Terminal {terminal_id} has no provider - cannot extract last message intelligently")
                    
                    # Use provider's extract_last_message_from_script method
                    full_output = self.tmux_adapter.get_window_history(session_id, window_name)
                    output = provider_instance.extract_last_message_from_script(full_output)
                else:
                    raise ValueError(f"Invalid mode '{mode}'. Must be 'full' or 'last'")
                
                return output
                
            finally:
                db.close()
            
        except Exception as e:
            logger.error(f"Failed to get output for terminal {terminal_id}: {e}")
            raise
    
    async def get_terminal_script(self, terminal_id: str) -> str:
        """Get terminal script for frontend attachment."""
        try:
            # Get terminal from database
            db = SessionLocal()
            try:
                terminal_record = db.query(TerminalModel).filter(
                    TerminalModel.id == terminal_id
                ).first()
                
                if not terminal_record:
                    raise ValueError(f"Terminal '{terminal_id}' not found")
                
                session_id = terminal_record.session_id
                
                # Extract window name from stored name
                if ":" in terminal_record.name:
                    window_name = terminal_record.name.split(":")[0]
                else:
                    window_name = terminal_record.name
                
                # Get complete terminal script/history
                script = self.tmux_adapter.get_window_history(session_id, window_name)
                
                return script
                
            finally:
                db.close()
            
        except Exception as e:
            logger.error(f"Failed to get script for terminal {terminal_id}: {e}")
            raise

    def get_window_history(self, session_name: str, window_name: str) -> str:
        """Get terminal history for window."""
        return self.tmux_adapter.get_window_history(session_name, window_name)
    
    def send_keys(self, session_name: str, window_name: str, keys: str) -> bool:
        """Send keys to window."""
        return self.tmux_adapter.send_keys(session_name, window_name, keys)
    
    def send_ctrl_c(self, session_name: str, window_name: str) -> bool:
        """Send Ctrl+C to window."""
        return self.tmux_adapter.send_ctrl_c(session_name, window_name)


# Global session manager instance
session_manager = SessionManager()
