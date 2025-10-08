"""Simplified tmux client as module singleton."""

import logging
import os
import re
import time
from typing import List, Dict, Optional
import libtmux
from cli_agent_orchestrator.constants import TMUX_HISTORY_LINES

logger = logging.getLogger(__name__)

# Delay between chunks when sending long key strings
SEND_KEYS_CHUNK_INTERVAL = 0.5


class TmuxClient:
    """Simplified tmux client for basic operations."""
    
    def __init__(self):
        self.server = libtmux.Server()
    
    def create_session(self, session_name: str, window_name: str, terminal_id: str) -> str:
        """Create detached tmux session with initial window and return window name."""
        try:
            environment = os.environ.copy()
            environment['CAO_TERMINAL_ID'] = terminal_id
            
            session = self.server.new_session(
                session_name=session_name,
                window_name=window_name,
                detach=True,
                environment=environment
            )
            logger.info(f"Created tmux session: {session_name} with window: {window_name}")
            return session.windows[0].name
        except Exception as e:
            logger.error(f"Failed to create session {session_name}: {e}")
            raise
    
    def create_window(self, session_name: str, window_name: str, terminal_id: str) -> str:
        """Create window in session and return window name."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")
            
            window = session.new_window(window_name=window_name, environment={
                'CAO_TERMINAL_ID': terminal_id
            })
            
            logger.info(f"Created window '{window.name}' in session '{session_name}'")
            return window.name
        except Exception as e:
            logger.error(f"Failed to create window in session {session_name}: {e}")
            raise
    def send_keys(self, session_name: str, window_name: str, keys: str):
        """Send keys to window with chunking for long messages."""
        try:
            logger.info(f"send_keys: {session_name}:{window_name} - keys: {keys}")
            
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")
            
            window = session.windows.get(window_name=window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")
            
            pane = window.active_pane
            if pane:
                # Split keys into chunks of ~100 characters at whitespace boundaries
                chunks = []
                start = 0
                
                while start < len(keys):
                    target_pos = start + 100
                    
                    if target_pos >= len(keys):
                        chunks.append(keys[start:])
                        break
                    
                    # Look forward from target position to find next whitespace
                    match = re.search(r'\s', keys[target_pos:])
                    
                    if match:
                        split_pos = target_pos + match.start()
                        chunks.append(keys[start:split_pos])
                        start = split_pos
                    else:
                        chunks.append(keys[start:])
                        break
                
                # Send chunks with delay between them
                for chunk in chunks:
                    pane.send_keys(chunk, enter=False)
                    time.sleep(SEND_KEYS_CHUNK_INTERVAL)
                
                # Send carriage return as separate command
                pane.send_keys("C-m", enter=False)
                logger.debug(f"Sent keys to {session_name}:{window_name}")
        except Exception as e:
            logger.error(f"Failed to send keys to {session_name}:{window_name}: {e}")
            raise

    def get_history(self, session_name: str, window_name: str, tail_lines: int = TMUX_HISTORY_LINES) -> str:
        """Get window history.
        
        Args:
            session_name: Name of tmux session
            window_name: Name of window in session
            tail_lines: Number of lines to capture from end (default: TMUX_HISTORY_LINES)
        """
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")
            
            window = session.windows.get(window_name=window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")
            
            # Use cmd to run capture-pane with -e (escape sequences) and -p (print) flags
            pane = window.panes[0]
            result = pane.cmd('capture-pane', '-e', '-p', '-S', f'-{tail_lines}')
            # Join all lines with newlines to get complete output
            return '\n'.join(result.stdout) if result.stdout else ""
        except Exception as e:
            logger.error(f"Failed to get history from {session_name}:{window_name}: {e}")
            raise
    
    def list_sessions(self) -> List[Dict]:
        """List all tmux sessions."""
        try:
            sessions = []
            for session in self.server.sessions:
                # Check if session has attached clients
                is_attached = len(getattr(session, 'attached_sessions', [])) > 0
                
                sessions.append({
                    "id": session.name,
                    "name": session.name,
                    "status": "active" if is_attached else "detached"
                })
            
            return sessions
        except Exception as e:
            logger.error(f"Failed to list sessions: {e}")
            return []
    
    def get_session_windows(self, session_name: str) -> List[Dict]:
        """Get all windows in a session."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                return []
            
            windows = []
            for window in session.windows:
                windows.append({
                    "name": window.name,
                    "index": window.index
                })
            
            return windows
        except Exception as e:
            logger.error(f"Failed to get windows for session {session_name}: {e}")
            return []
    
    def kill_session(self, session_name: str) -> bool:
        """Kill tmux session."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if session:
                session.kill_session()
                logger.info(f"Killed tmux session: {session_name}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to kill session {session_name}: {e}")
            return False
    
    def session_exists(self, session_name: str) -> bool:
        """Check if session exists."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            return session is not None
        except Exception:
            return False
    
    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        """Start piping pane output to file.
        
        Args:
            session_name: Tmux session name
            window_name: Tmux window name
            file_path: Absolute path to log file
        """
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")
            
            window = session.windows.get(window_name=window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")
            
            pane = window.active_pane
            if pane:
                pane.cmd('pipe-pane', '-o', f'cat >> {file_path}')
                logger.info(f"Started pipe-pane for {session_name}:{window_name} to {file_path}")
        except Exception as e:
            logger.error(f"Failed to start pipe-pane for {session_name}:{window_name}: {e}")
            raise
    
    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        """Stop piping pane output.
        
        Args:
            session_name: Tmux session name
            window_name: Tmux window name
        """
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")
            
            window = session.windows.get(window_name=window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")
            
            pane = window.active_pane
            if pane:
                pane.cmd('pipe-pane')
                logger.info(f"Stopped pipe-pane for {session_name}:{window_name}")
        except Exception as e:
            logger.error(f"Failed to stop pipe-pane for {session_name}:{window_name}: {e}")
            raise


# Module-level singleton
tmux_client = TmuxClient()
