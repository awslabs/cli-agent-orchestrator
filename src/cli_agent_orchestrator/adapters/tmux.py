"""Tmux adapter using libtmux."""

import logging
import os
import subprocess
import uuid
from typing import List, Optional

import libtmux

from cli_agent_orchestrator.models.session import Session, SessionStatus
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.constants import TMUX_HISTORY_LINES

logger = logging.getLogger(__name__)


class TmuxAdapter:
    """Adapter for tmux session and terminal operations using libtmux."""
    
    def __init__(self):
        self.server = libtmux.Server()
    
    def create_session_with_terminal(self, session_name: str, terminal_name: str, terminal_id: str = None) -> bool:
        """Create a new detached tmux session with initial terminal window."""
        try:
            # Session name is required - no auto-generation
            if not session_name:
                raise ValueError("Session name is required")
            if not terminal_name:
                raise ValueError("Terminal name is required")
            
            # Create session with the terminal as the initial window
            environment = os.environ.copy()
            if terminal_id:
                environment['CAO_TERMINAL_ID'] = terminal_id
                
            session = self.server.new_session(
                session_name=session_name,
                window_name=terminal_name,
                detach=True,
                environment=environment
            )
            
            logger.info(f"Created tmux session: {session_name} with terminal: {terminal_name}, terminal_id: {terminal_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to create session {session_name} with terminal {terminal_name}: {e}")
            # Re-raise the original exception instead of returning False
            raise
    
    def list_sessions(self) -> List[Session]:
        """List all tmux sessions as Pydantic models."""
        try:
            sessions = []
            for session in self.server.sessions:
                # Check if session has attached clients
                is_attached = len(getattr(session, 'attached_sessions', [])) > 0
                
                session_model = Session(
                    id=session.name,
                    name=session.name,
                    status=SessionStatus.ACTIVE if is_attached else SessionStatus.DETACHED
                )
                sessions.append(session_model)
            
            return sessions
            
        except Exception as e:
            logger.error(f"Failed to list sessions: {e}")
            return []
    
    def get_session_terminals(self, session_name: str) -> List[Terminal]:
        """Get all terminals (windows) in a session as Pydantic models."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")
            
            terminals = []
            for window in session.windows:
                terminal = Terminal(
                    id=window.name,
                    name=window.name,
                    session_id=session_name,
                    status=TerminalStatus.IDLE
                )
                terminals.append(terminal)
            
            return terminals
            
        except Exception as e:
            logger.error(f"Failed to get terminals for session {session_name}: {e}")
            return []
    
    def session_exists(self, session_name: str) -> bool:
        """Check if a tmux session exists."""
        try:
            return self.server.has_session(session_name)
        except Exception:
            return False
    
    def kill_session(self, session_name: str) -> bool:
        """Kill a tmux session."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if session:
                session.kill()
                logger.info(f"Killed tmux session: {session_name}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to kill session {session_name}: {e}")
            return False
    
    def create_window(self, session_name: str, window_name: Optional[str] = None, agent_profile: Optional[str] = None, terminal_id: Optional[str] = None) -> Optional[str]:
        """Create a new window in session."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                return None
            
            # Generate window name if not provided using agent profile + 4-char ID
            if not window_name:
                if agent_profile:
                    window_id = uuid.uuid4().hex[:4]
                    window_name = f"{agent_profile}-{window_id}"
                else:
                    raise ValueError("Window name or agent_profile is required for window creation")
            
            # Create window with environment variable if terminal_id provided
            if terminal_id:
                window = session.new_window(window_name=window_name, environment={
                    'CAO_TERMINAL_ID': terminal_id
                })
            else:
                window = session.new_window(window_name=window_name)
            
            logger.info(f"Created window: {session_name}:{window_name} with terminal_id: {terminal_id}")
            return window.name
            
        except Exception as e:
            logger.error(f"Failed to create window in {session_name}: {e}")
            return None
    
    def list_windows(self, session_name: str) -> List[dict]:
        """List all windows in a session."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                return []
            
            windows = []
            for window in session.windows:
                windows.append({
                    'index': window.index,
                    'name': window.name,
                    'active': window == session.active_window
                })
            return windows
            
        except Exception as e:
            logger.error(f"Failed to list windows for {session_name}: {e}")
            return []
    
    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Kill a window by name."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                return False
            
            window = session.windows.get(window_name=window_name)
            if not window:
                return False
            
            window.kill()
            logger.info(f"Killed window: {session_name}:{window_name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to kill window {session_name}:{window_name}: {e}")
            return False
    
    def get_window_history(self, session_name: str, window_name: str) -> str:
        """Get terminal history for window."""
        try:
            # Use subprocess to capture with ANSI escape sequences (-e flag)
            # This is needed for Q CLI provider status detection
            result = subprocess.run([
                "tmux", "capture-pane", "-t", f"{session_name}:{window_name}", "-e", "-p", "-S", f"-{TMUX_HISTORY_LINES}"
            ], capture_output=True, text=True)
            
            if result.returncode == 0:
                return result.stdout
            else:
                # Return empty string instead of raising exception for non-existent sessions/windows
                return ""
            
        except Exception as e:
            logger.error(f"Failed to get history for {session_name}:{window_name}: {e}")
            return ""
    
    def send_keys(self, session_name: str, window_name: str, keys: str) -> bool:
        """Send keys to window."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                return False
            
            window = session.windows.get(window_name=window_name)
            if not window:
                return False
            
            pane = window.active_pane
            if pane:
                # Send keys with Enter
                pane.send_keys(keys, enter=True)
                return True
            return False
            
        except Exception as e:
            logger.error(f"Failed to send keys to {session_name}:{window_name}: {e}")
            return False
    
    def send_ctrl_c(self, session_name: str, window_name: str) -> bool:
        """Send Ctrl+C to window."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                return False
            
            window = session.windows.get(window_name=window_name)
            if not window:
                return False
            
            pane = window.active_pane
            if pane:
                pane.send_keys("C-c")
                return True
            return False
            
        except Exception as e:
            logger.error(f"Failed to send Ctrl+C to {session_name}:{window_name}: {e}")
            return False
    
    def start_pipe(self, session_name: str, window_name: str, pipe_file: str) -> bool:
        """Start piping terminal output to file."""
        try:
            subprocess.run([
                "tmux", "pipe-pane", "-t", f"{session_name}:{window_name}", 
                f"cat >> {pipe_file}"
            ], check=True)
            logger.info(f"Started pipe for {session_name}:{window_name} to {pipe_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to start pipe for {session_name}:{window_name}: {e}")
            return False

    def stop_pipe(self, session_name: str, window_name: str) -> bool:
        """Stop piping terminal output."""
        try:
            subprocess.run([
                "tmux", "pipe-pane", "-t", f"{session_name}:{window_name}"
            ], check=True)
            logger.info(f"Stopped pipe for {session_name}:{window_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to stop pipe for {session_name}:{window_name}: {e}")
            return False
