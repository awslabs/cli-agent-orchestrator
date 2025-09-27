import logging
import sys
from typing import Optional

# TODO: setup default logging file location
def setup_logging(level: Optional[str] = None) -> None:
    """Setup logging configuration."""
    log_level = getattr(logging, (level or "INFO").upper(), logging.INFO)
    
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )
