#!/usr/bin/env python3
"""Check or explicitly remove the hosted default-setup blocker for advanced CodeQL."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import Optional, Sequence


class SetupError(Exception):
    """The CodeQL setup prerequisite could not be verified."""


def _gh(*arguments: str) -> str:
    return subprocess.check_output(["gh", *arguments], text=True).strip()


def _default_setup_state(endpoint: str) -> str:
    state = _gh("api", endpoint, "--jq", ".state")
    if state not in {"configured", "not-configured"}:
        raise SetupError("GitHub returned an unsupported or missing default-setup state.")
    return state


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Target repository as OWNER/REPO.")
    parser.add_argument("--ref", required=True, help="Full reviewed workflow commit SHA.")
    parser.add_argument(
        "--disable-default-setup",
        action="store_true",
        help="Change the hosted setting; use only during a coordinated migration.",
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", args.repo) or args.repo.endswith(
        ("/.", "/..")
    ):
        parser.error("--repo must be OWNER/REPO, not a URL or path.")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.ref):
        parser.error("--ref must be a full 40-character commit SHA.")

    endpoint = f"repos/{args.repo}/code-scanning/default-setup"
    disable_requested = False
    try:
        kind = _gh(
            "api",
            f"repos/{args.repo}/contents/.github/workflows/codeql.yml",
            "--method",
            "GET",
            "--raw-field",
            f"ref={args.ref}",
            "--jq",
            ".type",
        )
        if kind != "file":
            raise SetupError("The reviewed ref does not contain the CodeQL workflow file.")

        if _default_setup_state(endpoint) == "configured":
            if not args.disable_default_setup:
                raise SetupError(
                    "Default setup blocks advanced CodeQL uploads. Read the migration "
                    "procedure in SECURITY.md before using --disable-default-setup."
                )
            print(f"Requesting the default-setup switch for {args.repo}.", flush=True)
            disable_requested = True
            _gh(
                "api",
                endpoint,
                "--method",
                "PATCH",
                "--raw-field",
                "state=not-configured",
                "--silent",
            )
            if _default_setup_state(endpoint) != "not-configured":
                raise SetupError("The requested setup change could not be confirmed.")
    except (SetupError, subprocess.CalledProcessError, FileNotFoundError) as error:
        if isinstance(error, FileNotFoundError):
            message = "Install and authenticate the GitHub CLI (gh) before continuing."
        elif isinstance(error, subprocess.CalledProcessError):
            message = "GitHub CLI request failed; see its diagnostic above."
        else:
            message = str(error)
        print(f"ERROR: {message}", file=sys.stderr)
        if disable_requested:
            print(
                "The hosted setting may have changed. Verify it before continuing; "
                "the command will not retry or roll it back automatically.",
                file=sys.stderr,
            )
        return 1

    print(
        "Default setup is disabled. This does not prove scan coverage or merge "
        "protection; complete the workflow, baseline, and merge-rule checks."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
