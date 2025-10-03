import logging
import sys
from datetime import datetime
from typing import Optional
from cli_agent_orchestrator.constants import LOG_DIR

def setup_logging(level: Optional[str] = None) -> None:
    """Setup logging configuration."""
    log_level = getattr(logging, (level or "INFO").upper(), logging.INFO)
    
    # Ensure log directory exists
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = LOG_DIR / f"cao_{timestamp}.log"
    
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file)
        ]
    )
    
    logging.info(f"Logging to: {log_file}")
