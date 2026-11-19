#!/usr/bin/env python3
"""Entry point Kiro Crew starts: ``python backend/server.py`` with ``PORT`` and the proxy secret."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from cao_shelf.http import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(HERE.parent))
