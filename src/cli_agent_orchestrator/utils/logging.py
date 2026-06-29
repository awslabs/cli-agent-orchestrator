import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.constants import LOG_DIR


def setup_logging() -> None:
    """Setup logging configuration."""
    log_level = os.getenv("CAO_LOG_LEVEL", "INFO").upper()

    # Ensure log directory exists
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = LOG_DIR / f"cao_{timestamp}.log"

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(log_file)],
    )

    print(f"Server logs: {log_file}")
    print("For debug logs: export CAO_LOG_LEVEL=DEBUG && cao-server")
    logging.info(f"Logging to: {log_file}")


def latest_server_log_path() -> Optional[Path]:
    """Return the most recently modified ``cao_*.log`` in LOG_DIR, or None.

    Used to point error messages at the server log that explains a failure
    (e.g. a launch that 500s on provider init) so users can read the truth
    without leaving ``cao``.
    """
    try:
        logs = list(LOG_DIR.glob("cao_*.log"))
    except OSError:
        return None
    if not logs:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime)
