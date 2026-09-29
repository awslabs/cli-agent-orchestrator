"""CLI Agent Orchestrator.

This package is POSIX-only. The guard below is the first thing it does, so an unsupported
platform gets a sentence instead of a traceback from whichever submodule happened to import
a POSIX-only stdlib module first.

WHY A RUNTIME GUARD AND NOT PACKAGING METADATA: there is no metadata that can stop the
install. `requires-python` constrains the interpreter, not the OS, and the `Operating System`
classifiers in pyproject.toml are purely informational — no installer enforces them. Not
publishing a `win_amd64` wheel only makes pip fall back to the sdist, which builds and
installs fine (the Rust build hook is inert without cargo). So the first honest failure point
available is import time, which is here.
"""

import sys

if sys.platform == "win32":  # pragma: no cover - asserted by a monkeypatched unit test
    raise ImportError(
        "cli-agent-orchestrator does not support Windows.\n"
        "\n"
        "This package imports POSIX-only stdlib modules (fcntl, termios) at module scope and "
        "orchestrates agents through tmux, which has no native Windows build. pip installed "
        "the source distribution because no Windows wheel is published; that install cannot "
        "run.\n"
        "\n"
        "Supported: macOS (arm64, x86_64) and Linux (x86_64). On Windows, run CAO under WSL2 "
        "or a Linux container, where tmux and the POSIX APIs are available."
    )
