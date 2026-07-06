import logging
from datetime import datetime

from cli_agent_orchestrator.constants import LOG_DIR
from cli_agent_orchestrator.services.config_service import ConfigService


def setup_logging() -> None:
    """Setup logging configuration."""
    log_level = str(ConfigService.get("logging.level", default="INFO")).upper()

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
