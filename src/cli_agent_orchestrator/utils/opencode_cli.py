"""Probe the installed OpenCode interface before starting an interactive worker."""

import re
import subprocess


def detect_opencode_major() -> int:
    """Accept known generations only, and verify the flags CAO depends on.

    Do not cache: PATH or the installed executable can change between launches.
    Help probes do not start a model session or change agent configuration.
    """

    def probe(*args: str) -> str:
        try:
            result = subprocess.run(
                ["opencode", *args], capture_output=True, text=True, timeout=15, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Cannot probe OpenCode ({' '.join(args)}): {exc}") from exc
        if result.returncode:
            raise RuntimeError(
                f"OpenCode {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}"
            )
        return result.stdout + result.stderr

    version = probe("--version").strip()
    match = re.fullmatch(r"(?:opencode\s+)?v?(\d+)\.\d+\.\d+(?:[-+][\w.-]+)?", version)
    if not match or int(match[1]) not in (1, 2):
        raise RuntimeError(
            f"Unsupported OpenCode version {version!r}; CAO supports OpenCode v1 and v2 Mini. "
            "Check which opencode executable is on PATH."
        )
    major = int(match[1])
    help_text = probe(*(["mini", "--help"] if major == 2 else ["--help"]))
    required = ["--agent", "--model"] + (["--standalone"] if major == 2 else [])
    missing = [
        flag for flag in required if not re.search(re.escape(flag) + r"(?=[\s=,]|$)", help_text)
    ]
    if missing:
        raise RuntimeError(
            f"OpenCode {version} lacks required interactive flags: {', '.join(missing)}. "
            "Check opencode mini --help (v2) or opencode --help (v1)."
        )
    return major
