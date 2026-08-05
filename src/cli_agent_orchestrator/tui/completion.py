"""Argument completion for the ``cao tui`` thin shell (U2, W-4).

:class:`ArgCompleter` is a :class:`prompt_toolkit.completion.Completer` that offers
option flag names and choice values for the currently focused command. It is a
pure function of the (cached) :class:`~cli_agent_orchestrator.tui.command_catalog.CommandCatalog`
passed in — no network, no file I/O, and none of the forbidden heavy layers
(BR-1). Full introspection (RD-d=A / BR-3): it surfaces every parameter the
catalog parsed, never a curated subset.

Import rule (thin shell): only the standard library, ``prompt_toolkit``, and the
``tui`` package's own modules may be imported here (package docstring in
``__init__``; enforced by ``test/tui/test_thin_shell_boundary.py``).
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, List, Sequence, Union

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from cli_agent_orchestrator.tui.command_catalog import CatalogError, CommandCatalog, Param

logger = logging.getLogger(__name__)

# The focused command path is either fixed at construction or supplied lazily by a
# zero-arg callable (the App re-reads it as focus/selection changes).
PathLike = Union[Sequence[str], Callable[[], Sequence[str]]]


class ArgCompleter(Completer):
    """Yield flag/choice completions for the focused ``cao`` command (W-4).

    Behaviour:

    * When the token immediately before the cursor is an option that carries
      ``choices`` (parsed from help, BR-6), completing the *value* offers those
      choice values (prefix-filtered).
    * Otherwise, completing a token offers the command's option flag names, plus
      the choice values of any choice-bearing option (prefix-filtered). Typing a
      leading ``-`` naturally narrows to flags, since choice values do not start
      with a dash.

    The completer never raises into the UI: if the catalog cannot introspect the
    command (:class:`CatalogError`), it degrades to *no* completions — the fatal
    "cao not found" state is the App's job (U1), not the completer's.
    """

    def __init__(self, catalog: CommandCatalog, path: PathLike = ()) -> None:
        self._catalog = catalog
        self._path = path

    # -- path resolution ----------------------------------------------------- #

    def _resolve_path(self) -> List[str]:
        """Resolve the focused command path (accepts a fixed list or a callable)."""

        path = self._path() if callable(self._path) else self._path
        return list(path)

    def _params(self) -> List[Param]:
        """Fetch the focused command's params; degrade to ``[]`` on catalog error."""

        # Resolve the path ONCE: ``self._path`` may be a callable whose result
        # changes between calls, so resolving again in the ``except`` could log a
        # path that was never the one actually attempted (and repeats the work).
        path = self._resolve_path()
        try:
            return self._catalog.params(path)
        except CatalogError as exc:
            logger.debug("completion: catalog unavailable for %r: %s", path, exc)
            return []

    # -- Completer API ------------------------------------------------------- #

    def get_completions(self, document: Document, complete_event: object) -> Iterable[Completion]:
        """Yield :class:`Completion` candidates for the word before the cursor."""

        params = self._params()
        if not params:
            return

        text = document.text_before_cursor
        current_word = _trailing_word(text)
        prev_token = _previous_token(text, current_word)
        start = -len(current_word)

        options = [p for p in params if p.kind == "option"]

        # Value completion: the previous token is a choice-bearing option flag.
        active = _match_option(options, prev_token)
        if active is not None and active.choices:
            for choice in active.choices:
                if choice.startswith(current_word):
                    yield Completion(
                        choice,
                        start_position=start,
                        display_meta=f"choice for {active.name}",
                    )
            return

        # Flag completion (+ surfacing choice values of choice-bearing options).
        seen: set = set()
        for opt in options:
            if opt.name.startswith(current_word) and opt.name not in seen:
                seen.add(opt.name)
                yield Completion(
                    opt.name,
                    start_position=start,
                    display_meta=opt.help or ("required" if opt.required else "option"),
                )
        for opt in options:
            for choice in opt.choices or ():
                if choice.startswith(current_word) and choice not in seen:
                    seen.add(choice)
                    yield Completion(
                        choice,
                        start_position=start,
                        display_meta=f"choice for {opt.name}",
                    )


def _trailing_word(text: str) -> str:
    """Return the run of non-whitespace characters immediately before the cursor.

    Empty when the cursor sits just after whitespace (a fresh token position).
    """

    if not text or text[-1].isspace():
        return ""
    return text.split()[-1]


def _previous_token(text: str, current_word: str) -> str:
    """Return the last complete token before ``current_word`` (``""`` if none)."""

    head = text[: len(text) - len(current_word)] if current_word else text
    tokens = head.split()
    return tokens[-1] if tokens else ""


def _match_option(options: Sequence[Param], token: str) -> Union[Param, None]:
    """Return the option whose name equals ``token`` (a flag that takes a value)."""

    if not token:
        return None
    for opt in options:
        if opt.name == token and opt.takes_value:
            return opt
    return None
