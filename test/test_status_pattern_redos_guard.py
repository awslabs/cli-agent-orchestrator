"""Root guard: provider regexes stay free of ReDoS shapes, including ones added later.

Status detection and response extraction run regexes over text an agent CLI
painted on its terminal, so a pattern whose backtracking is superlinear lets
that text stall every status poll (CWE-1333). The provider-specific tests in
test/security/ prove each fix (step growth, old-vs-new differential); this
guard is the always-on half, and it does not rely on anyone remembering to
list a new pattern:

* every regex in the modules below is DISCOVERED: module-level and class-level
  ``re.Pattern`` objects and constant-named strings that compile, plus the
  ``*_pattern``/``*_re`` attributes a provider builds per instance;
* a discovered pattern that is in ``REGISTRY`` is checked in the mode its
  callers use (``structural_issues``), and its backtracking steps must grow
  linearly on inputs generated from its own structure;
* any other discovered pattern is checked strictly (as a ``search`` with
  re.MULTILINE, the mode with the most ways to be slow) and must be clean;
* a pattern that fails the strict check without being slow where it is used
  (a fragment that is only ever spliced into a registered pattern, a pattern
  nothing applies) needs an entry in ``EXEMPT`` that says why. An exemption
  that no longer applies -- the pattern is gone, or now passes -- fails too,
  so the list cannot rot.

Modules are covered once they are listed in ``MODULES``; providers not yet
listed here are not covered (an inlined ``re.search(r"...")`` literal is not a
discovered pattern either). To add one, list it, run this file, and register
or exempt what it reports.
"""

import importlib
import inspect
import re
from test.security.redos_analysis import (
    MODES,
    input_shapes,
    nonlinear_shapes,
    pattern_probe,
    structural_issues,
)
from typing import Optional, Union

import pytest

#: Module (under cli_agent_orchestrator) -> the provider class to instantiate, and its
#: arguments, so its per-instance patterns are discovered too (None: module level only).
MODULES: dict[str, Optional[tuple[str, tuple[str, ...]]]] = {
    "providers.claude_code": ("ClaudeCodeProvider", ("t", "s", "w")),
    "providers.codex": ("CodexProvider", ("t", "s", "w")),
    "providers.kiro_cli": ("KiroCliProvider", ("t", "s", "w", "developer")),
    # Classifies the same providers' prompts, with their patterns.
    "services.agui.handoff_approval": None,
}

#: Discovered name -> (how callers run it, the flags they add).
REGISTRY: dict[str, tuple[str, int]] = {
    # claude_code
    "claude_code.PROCESSING_PATTERN": ("finditer", 0),
    "claude_code.GET_STATUS_COMPLETION_PATTERN": ("finditer", 0),
    "claude_code.COMPLETION_SUMMARY_PATTERN": ("search", 0),
    "claude_code.NEW_TUI_BOX_PATTERN": ("search", 0),
    "claude_code._NEW_TUI_BOX_RULE_RUN": ("search", 0),
    "claude_code._NEW_TUI_BOX_BELOW_TOP_RE": ("match", 0),
    "claude_code.EXTRACTION_RESPONSE_PATTERN": ("finditer", 0),
    "claude_code.EFFORT_FOOTER_LINE_PATTERN": ("finditer", 0),
    "claude_code.REDUCED_MOTION_SPINNER_PATTERN": ("search", 0),
    "claude_code.AFTER_SUMMARY_SPINNER_PATTERN": ("search", 0),
    "claude_code.SEPARATOR_WALK_SPINNER_PATTERN": ("search", 0),
    "claude_code.NEW_TUI_BOX_SPINNER_PATTERN": ("search", 0),
    "claude_code.ClaudeCodeProvider._COMPOSER_PROMPT_RE": ("match", 0),
    "claude_code.ClaudeCodeProvider._COMPOSER_RAIL_RE": ("search", 0),
    "claude_code.ClaudeCodeProvider._SOL_IDLE_RE": ("match", 0),
    # codex
    "codex.IDLE_PROMPT_STRICT_PATTERN": ("search", re.MULTILINE),
    "codex._IDLE_PROMPT_STRICT_RE": ("search", 0),
    "codex.TUI_PROGRESS_PATTERN": ("search", re.MULTILINE),
    "codex.TUI_FOOTER_PATTERN": ("search", 0),
    "codex.UPDATE_DIALOG_PATTERN": ("search", 0),
    "codex.STARTUP_ACTIVITY_PATTERN": ("search", re.MULTILINE),
    "codex._CONFIG_PROFILE_ERROR_COMPACT": ("search", 0),
    "codex.COMPLETION_FOOTER_PATTERN": ("match", 0),
    "codex.LAUNCH_COMMAND_PATTERN": ("match", 0),
    "codex.STARTUP_IDLE_PLACEHOLDER_PATTERN": ("match", 0),
    # kiro_cli
    "kiro_cli.TUI_INITIALIZING_PATTERN": ("finditer", 0),
    "kiro_cli.TUI_225_CHILD_ROW_PATTERN": ("search", 0),
    "kiro_cli.KiroCliProvider._COMPOSER_CHROME_RE": ("search", 0),
    "kiro_cli.KiroCliProvider._COMPOSER_PROMPT_RE": ("match", 0),
    "kiro_cli.KiroCliProvider()._new_tui_header_pattern": ("search", 0),
}

_FRAGMENT = "A fragment, never applied on its own: it is spliced into {}, which is registered."
_UNUSED = "Defined but applied nowhere. It is superlinear: fix it before anything uses it."

#: Discovered name -> why it may fail the strict check.
EXEMPT: dict[str, str] = {
    "claude_code._EFFORT_FOOTER_TAIL": _FRAGMENT.format(
        "EXTRACTION_RESPONSE_PATTERN and EFFORT_FOOTER_LINE_PATTERN, after the marker,"
    ),
    "claude_code._SPINNER_VERB": _FRAGMENT.format(
        "NEW_TUI_BOX_SPINNER_PATTERN and AFTER_SUMMARY_SPINNER_PATTERN, after the glyph,"
    ),
    "claude_code._REDUCED_MOTION_STATUS": _FRAGMENT.format("REDUCED_MOTION_SPINNER_PATTERN"),
    "claude_code._REDUCED_MOTION_AFTER_ELLIPSIS": _FRAGMENT.format(
        "REDUCED_MOTION_SPINNER_PATTERN, after a '…',"
    ),
    "claude_code._NEW_TUI_BOX_BELOW_TOP": _FRAGMENT.format(
        "NEW_TUI_BOX_PATTERN and _NEW_TUI_BOX_BELOW_TOP_RE (applied anchored, with .match)"
    ),
    "claude_code.THINKING_BEFORE_SEPARATOR_PATTERN": _UNUSED,
    "claude_code.NEW_TUI_SPINNER_PATTERN": _UNUSED,
    "codex._FOOTER_DURATION": _FRAGMENT.format("COMPLETION_FOOTER_PATTERN"),
    "codex.CONFIG_PROFILE_ERROR_PATTERN": (
        "The phrase template, never applied itself: _CONFIG_PROFILE_ERROR_COMPACT (registered) "
        "is built from it with a line anchor and a skip to the first lead phrase."
    ),
    "kiro_cli.IDLE_PROMPT_PATTERN_LOG": (
        "Returned by get_idle_pattern_for_log(), which no production code calls (only unit "
        "tests do). It is superlinear: fix it before log scanning uses it."
    ),
}

_CONSTANT = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
_INSTANCE = re.compile(r"_(?:pattern|re)$")

Regex = Union[str, "re.Pattern[str]"]


def _source(value: Regex) -> tuple[str, int]:
    """The pattern text and the flags it carries itself (none, for a string)."""
    if isinstance(value, re.Pattern):
        return value.pattern, value.flags
    return value, 0


def _is_regex(value: object) -> bool:
    if isinstance(value, re.Pattern):
        return True
    if isinstance(value, str):
        try:
            re.compile(value, re.MULTILINE)
        except (re.error, OverflowError):  # not a regex: prose, a path, a fragment
            return False
        return True
    return False


def _discover() -> dict[str, Regex]:
    """Every regex in MODULES, by name, as it is defined (a string or a compiled pattern)."""
    found: dict[str, Regex] = {}

    def take(name: str, value: object, named_like_a_regex: bool) -> None:
        if isinstance(value, re.Pattern) or (named_like_a_regex and _is_regex(value)):
            found[name] = value  # type: ignore[assignment]

    for module_path, instance_spec in MODULES.items():
        module = importlib.import_module(f"cli_agent_orchestrator.{module_path}")
        module_name = module_path.rsplit(".", 1)[-1]
        for name, value in vars(module).items():
            if inspect.isclass(value) and value.__module__ == module.__name__:
                for attr, attr_value in vars(value).items():
                    take(f"{module_name}.{name}.{attr}", attr_value, bool(_CONSTANT.match(attr)))
            else:
                take(f"{module_name}.{name}", value, bool(_CONSTANT.match(name)))
        if instance_spec is None:
            continue
        class_name, args = instance_spec
        instance = getattr(module, class_name)(*args)
        for attr, value in vars(instance).items():
            take(f"{module_name}.{class_name}().{attr}", value, bool(_INSTANCE.search(attr)))
    return found


DISCOVERED = _discover()


def _strict_issues(value: Regex) -> list[str]:
    source, flags = _source(value)
    return structural_issues(source, "search", flags | re.MULTILINE)


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_registered_pattern_is_linear_where_it_is_used(name):
    assert name in DISCOVERED, f"{name} is registered but no longer exists; update REGISTRY"
    mode, call_flags = REGISTRY[name]
    source, own_flags = _source(DISCOVERED[name])
    flags = own_flags | call_flags
    issues = structural_issues(source, mode, flags)
    assert issues == [], (
        f"{name} ({mode}) holds a shape that backtracks superlinearly on agent-controlled "
        f"text: {issues}. test/security/redos_analysis.py explains each finding; the "
        f"test/security/test_*_status_pattern_redos.py files show how the Kiro, Claude Code "
        f"and Codex fixes removed them."
    )
    # Backstop for shapes the structure check cannot see: count the steps.
    growth = nonlinear_shapes(pattern_probe(source, mode, flags), input_shapes(source, flags))
    assert growth == [], f"{name} ({mode}) does not grow linearly on: {growth[:3]}"


@pytest.mark.parametrize("name", sorted(set(DISCOVERED) - set(REGISTRY) - set(EXEMPT)))
def test_unregistered_pattern_passes_the_strict_check(name):
    issues = _strict_issues(DISCOVERED[name])
    assert issues == [], (
        f"{name} is a regex that fails the strict ReDoS check: {issues}. Fix it, register it "
        f"in REGISTRY with the mode and flags its callers use (match/fullmatch skip the "
        f"start-position checks), or, if it is never applied on its own, exempt it in EXEMPT "
        f"with the reason."
    )


@pytest.mark.parametrize("name", sorted(EXEMPT))
def test_exemption_still_applies(name):
    assert name in DISCOVERED, f"{name} no longer exists; drop it from EXEMPT"
    assert _strict_issues(DISCOVERED[name]), f"{name} passes now; drop it from EXEMPT"


def test_registry_and_exemptions_are_well_formed():
    assert not set(REGISTRY) & set(EXEMPT), "a pattern is both registered and exempt"
    for name, (mode, _flags) in REGISTRY.items():
        assert mode in MODES, f"{name}: unknown mode {mode!r}"
    for name, reason in EXEMPT.items():
        assert reason.strip(), f"{name}: an exemption needs a reason"


def test_discovery_sees_every_kind_of_pattern():
    """Module constants, class attributes and per-instance patterns are all found."""
    for name in (
        "claude_code.PROCESSING_PATTERN",
        "claude_code.ClaudeCodeProvider._COMPOSER_RAIL_RE",
        "kiro_cli.KiroCliProvider()._new_tui_header_pattern",
        "kiro_cli.KiroCliProvider()._idle_prompt_pattern",
    ):
        assert name in DISCOVERED, f"{name} not discovered"
