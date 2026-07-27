"""Unit tests for :mod:`cli_agent_orchestrator.tui.completion` (U2, W-4).

The completer is driven by a stub catalog (a tiny object exposing ``params``),
so these tests exercise completion logic without any subprocess or real ``cao``.
Covers: flag-name completion, choice-value completion when an option is focused,
full-set surfacing (RD-d=A / BR-3), prefix filtering, empty output when the
command has no params, and graceful degradation when the catalog raises
:class:`CatalogError`.
"""

from __future__ import annotations

from typing import List, Sequence

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from cli_agent_orchestrator.tui.command_catalog import CatalogError, Param
from cli_agent_orchestrator.tui.completion import ArgCompleter


class _StubCatalog:
    """Minimal stand-in exposing the single method the completer calls."""

    def __init__(self, params: Sequence[Param]) -> None:
        self._params = list(params)
        self.calls: List[List[str]] = []

    def params(self, command_path: Sequence[str]) -> List[Param]:
        self.calls.append(list(command_path))
        return list(self._params)


class _RaisingCatalog:
    """Stub whose ``params`` always raises, to test graceful degradation."""

    def params(self, command_path: Sequence[str]) -> List[Param]:
        raise CatalogError(["cao", *command_path, "--help"], message="boom")


# A representative param set: two flags, one flag with choices, one positional.
PARAMS = [
    Param("--terminal", "option", False, True, None, "Send to a specific terminal ID"),
    Param("--async", "option", False, False, None, "Send and return immediately"),
    Param("--scope", "option", False, True, ["global", "project", "session"], "Scope to search"),
    Param("SESSION_NAME", "argument", True, True, None, ""),
]


def _complete(completer: ArgCompleter, text: str) -> List[str]:
    """Return the completion texts the completer yields for ``text`` before cursor."""

    doc = Document(text=text, cursor_position=len(text))
    return [c.text for c in completer.get_completions(doc, CompleteEvent())]


def test_yields_flag_names_on_empty_word() -> None:
    completer = ArgCompleter(_StubCatalog(PARAMS), path=["session", "send"])
    results = _complete(completer, "")

    # All three option flag names, plus the choice values of --scope (RD-d=A).
    assert "--terminal" in results
    assert "--async" in results
    assert "--scope" in results
    assert {"global", "project", "session"}.issubset(set(results))


def test_prefix_filters_flags() -> None:
    completer = ArgCompleter(_StubCatalog(PARAMS), path=["session", "send"])
    results = _complete(completer, "--te")

    assert results == ["--terminal"]


def test_choice_values_after_focused_option() -> None:
    completer = ArgCompleter(_StubCatalog(PARAMS), path=["memory", "show"])
    # Cursor sits after "--scope " => complete the option's VALUE, not more flags.
    results = _complete(completer, "--scope ")

    assert results == ["global", "project", "session"]
    assert not any(r.startswith("--") for r in results)


def test_choice_values_prefix_filtered() -> None:
    completer = ArgCompleter(_StubCatalog(PARAMS), path=["memory", "show"])
    results = _complete(completer, "--scope pro")

    assert results == ["project"]


def test_empty_when_no_params() -> None:
    completer = ArgCompleter(_StubCatalog([]), path=["info"])
    assert _complete(completer, "") == []
    assert _complete(completer, "--") == []


def test_start_position_replaces_current_word() -> None:
    completer = ArgCompleter(_StubCatalog(PARAMS), path=["session", "send"])
    doc = Document(text="--te", cursor_position=4)
    completions = list(completer.get_completions(doc, CompleteEvent()))

    assert completions, "expected at least one completion"
    # start_position must negate the typed prefix so the word is replaced, not doubled.
    assert all(c.start_position == -len("--te") for c in completions)


def test_path_callable_is_resolved_lazily() -> None:
    stub = _StubCatalog(PARAMS)
    focus = {"path": ["session", "list"]}
    completer = ArgCompleter(stub, path=lambda: focus["path"])

    _complete(completer, "")
    focus["path"] = ["session", "send"]
    _complete(completer, "")

    assert stub.calls == [["session", "list"], ["session", "send"]]


def test_catalog_error_degrades_to_no_completions() -> None:
    """A catalog failure must not surface into the completer as an exception."""

    completer = ArgCompleter(_RaisingCatalog(), path=["session", "send"])
    assert _complete(completer, "--") == []


def test_unrecognized_previous_option_falls_through_to_flag_completion() -> None:
    """A non-empty prev-token matching no option (`_match_option` fallthrough).

    When the token before the cursor is not one of the command's option flags,
    ``_match_option`` returns ``None`` and completion falls through to offering
    flag names (not choice values). Closes the ``completion.py`` line-147
    fallthrough branch — the only path the existing set never exercised, since
    those tests hit either the empty-token early return or a found match.
    """

    completer = ArgCompleter(_StubCatalog(PARAMS), path=["session", "send"])
    # "--unrecognized-flag " is a complete, unknown token before a fresh word:
    # _match_option finds no match -> we get flag completion, not choice values.
    results = _complete(completer, "--unrecognized-flag ")

    assert "--terminal" in results
    assert "--scope" in results
    # Not value-completion for a bogus option: the --scope choice values only
    # surface here via the flag-completion branch's choice-surfacing, alongside
    # flags — never as the exclusive result the focused-option branch returns.
    assert any(r.startswith("--") for r in results)
