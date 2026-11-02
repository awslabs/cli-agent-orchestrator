"""Root guard: registered provider status/extraction regexes stay free of ReDoS shapes.

Status detection and response extraction run regexes over text an agent CLI
painted on its terminal, so a pattern whose backtracking is superlinear lets
that text stall every status poll (CWE-1333). The provider-specific tests in
test/security/ prove each fix (step growth, old-vs-new differential); this
guard is the cheap, always-on half: every pattern registered below must pass
``structural_issues`` (test/security/redos_analysis.py) in the mode its callers
use, so an edit that reintroduces a rescanning or ambiguous shape fails here
with the reason, before anyone measures anything.

Scope is the registry, nothing more. It holds the patterns the tier-1 ReDoS
fixes made linear (Kiro, Claude Code, Codex) plus Codex's #771 fixes. Other
providers' patterns are not covered until the tier-2 fixes register them here:
add ``(attribute, mode, flags)`` under the provider module. Code that does not
use a regex (Kiro's legacy permission-prompt scan) is covered by its step-count
test instead.
"""

import importlib
import re
from test.security.redos_analysis import MODES, structural_issues

import pytest

#: provider module -> (attribute path, how callers run it, flags they add)
REGISTRY: dict[str, list[tuple[str, str, int]]] = {
    "kiro_cli": [
        ("TUI_INITIALIZING_PATTERN", "finditer", 0),
        ("KiroCliProvider._COMPOSER_CHROME_RE", "search", 0),
    ],
    "claude_code": [
        ("PROCESSING_PATTERN", "finditer", 0),
        ("GET_STATUS_COMPLETION_PATTERN", "finditer", 0),
        ("NEW_TUI_BOX_PATTERN", "search", 0),
        ("_NEW_TUI_BOX_RULE_RUN", "search", 0),
        ("_NEW_TUI_BOX_BELOW_TOP_RE", "match", 0),
        ("EXTRACTION_RESPONSE_PATTERN", "finditer", 0),
        ("EFFORT_FOOTER_LINE_PATTERN", "finditer", 0),
    ],
    "codex": [
        ("IDLE_PROMPT_STRICT_PATTERN", "search", re.MULTILINE),
        ("_IDLE_PROMPT_STRICT_RE", "search", 0),
        ("TUI_PROGRESS_PATTERN", "search", re.MULTILINE),
        ("TUI_FOOTER_PATTERN", "search", 0),
        ("UPDATE_DIALOG_PATTERN", "search", 0),
    ],
}

ENTRIES = [
    pytest.param(module, attr, mode, flags, id=f"{module}.{attr}")
    for module, attrs in REGISTRY.items()
    for attr, mode, flags in attrs
]


def _resolve(module: str, attr: str) -> object:
    value: object = importlib.import_module(f"cli_agent_orchestrator.providers.{module}")
    for part in attr.split("."):
        value = getattr(value, part)
    return value


@pytest.mark.parametrize("module,attr,mode,flags", ENTRIES)
def test_registered_pattern_has_no_redos_shape(module, attr, mode, flags):
    pattern = _resolve(module, attr)
    assert isinstance(pattern, (str, re.Pattern)), f"{module}.{attr} is not a regex"
    issues = structural_issues(pattern, mode, flags)
    assert issues == [], (
        f"{module}.{attr} ({mode}) holds a shape that backtracks superlinearly on "
        f"agent-controlled text: {issues}. See test/security/redos_analysis.py for what "
        f"each finding means and test/security/test_*_status_pattern_redos.py for how "
        f"the tier-1 fixes removed them."
    )


def test_registry_entries_are_well_formed():
    for module, attrs in REGISTRY.items():
        assert attrs, f"{module} is registered with no patterns"
        for attr, mode, _flags in attrs:
            assert mode in MODES, f"{module}.{attr}: unknown mode {mode!r}"
