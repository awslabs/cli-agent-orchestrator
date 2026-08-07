"""Guard test: every API prefix the dashboard calls must be proxied in dev.

This gap is invisible in production and in every other test. cao-server serves
the built bundle from its own origin, so a missing proxy entry breaks nothing
there; the vitest suite stubs `fetch`, so it never touches the proxy either. It
appears only under `npm run dev`, where the unproxied call 404s against the Vite
dev server and the feature is simply dead — which is how the Projects tab
shipped with `/tracker` missing from the list.

Written in Python rather than as a vitest case on purpose: the check reads two
files off disk, which in TypeScript means `node:fs` and therefore an
`@types/node` dependency the web package does not otherwise need. It is a static
consistency check between two source files, which is what the other guards in
this directory already are.
"""

from __future__ import annotations

import re
from pathlib import Path

import cli_agent_orchestrator

_WEB = Path(cli_agent_orchestrator.__file__).parent.parent.parent / "web"
_VITE_CONFIG = _WEB / "vite.config.ts"
_API_CLIENT = _WEB / "src" / "api.ts"

_PROXY_ENTRY = re.compile(r"'(/[a-z-]+)':\s*\{")
# Both template literals and plain strings: `/tracker/issues/${key}` and
# '/agents/profiles' each contribute their first path segment.
_FETCH_PATH = re.compile(r"fetchJSON<[^>]*>\(\s*[`'\"](/[a-zA-Z0-9-]+)")


def _proxied_prefixes() -> set:
    text = _VITE_CONFIG.read_text(encoding="utf-8")
    block = text[text.index("proxy:") :]
    return set(_PROXY_ENTRY.findall(block))


def _called_prefixes() -> set:
    return set(_FETCH_PATH.findall(_API_CLIENT.read_text(encoding="utf-8")))


def test_every_called_prefix_is_proxied() -> None:
    missing = sorted(_called_prefixes() - _proxied_prefixes())
    assert missing == [], (
        "these API prefixes are called by web/src/api.ts but are not proxied in "
        f"web/vite.config.ts, so they 404 under `npm run dev`: {missing}"
    )


def test_both_sides_are_non_empty() -> None:
    # A comparison between two empty sets passes for the wrong reason and would
    # hide every future regression.
    assert len(_proxied_prefixes()) > 5
    assert len(_called_prefixes()) > 5


def test_the_tracker_routes_the_projects_tab_needs_are_present() -> None:
    assert "/tracker" in _proxied_prefixes()
