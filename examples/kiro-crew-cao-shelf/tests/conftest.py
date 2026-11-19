"""Puts ``backend/`` on ``sys.path`` before any test module imports ``cao_shelf``."""

import sys
from pathlib import Path

BACKEND = str(Path(__file__).resolve().parents[1] / "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
