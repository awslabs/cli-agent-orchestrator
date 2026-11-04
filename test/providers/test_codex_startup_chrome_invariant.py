"""Guard test: Codex startup chrome is never read as turn evidence (#739).

**The class.** The Codex provider reads a terminal frame, and a handful of its
decisions rest on a glyph -- a leading ``•`` -- that the TUI draws for BOTH the
model's own reply and for chrome Codex prints before any turn exists. #739 was
two independent failures off that one glyph: an account notice
(``• You have 1 usage limit reset available.``) vetoed startup readiness
forever, and the same line made ``get_status`` answer COMPLETED for a worker
that had never been given a task. The defect class is **a decision site that
treats an ambiguous screen-scraped glyph as positive evidence of turn activity
without first establishing that a turn was ever dispatched** -- and every such
site is one notice wording away from the same pair of failures.

The per-instance regression lives in ``TestCodexAccountNoticeAtStartup``
(``test_codex_provider_unit.py``), pinned to the two reported captures. This
file guards the class in two directions that pinned captures cannot:

1. :class:`TestNoticeBulletChangesNoVerdict` -- a **differential** invariant
   over generated frames. Inserting a notice bullet into the startup chrome
   must not change any verdict, for any notice copy, banner version, composer
   glyph or footer geometry. Nothing here matches notice wording, so a notice
   Codex has not invented yet is covered; on the unfixed code 24 of the 40
   cases flip.
2. :class:`TestTurnEvidenceSitesAreEnumerated` -- an AST inventory of every
   function in ``codex.py`` that consults the ambiguous glyph. A NEW site must
   be classified in the allowlist below with the precondition that makes it
   safe, and any site that can emit a COMPLETED verdict or that reads the
   startup window must consult ``_is_pre_turn_startup_frame``. That second rule
   is derived from the code, not from the allowlist, so relabelling an entry
   does not buy a pass.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import codex as codex_module
from cli_agent_orchestrator.providers.codex import (
    CodexProvider,
    _has_startup_idle_composer,
    _is_pre_turn_startup_frame,
)

# --------------------------------------------------------------------------
# 1. Differential invariant: a notice bullet must not change a verdict.
# --------------------------------------------------------------------------

# Deliberately includes copy nobody has reported, because the fix is structural
# and must not depend on the wording somebody happened to see.
_NOTICES = (
    "• You have 1 usage limit reset available. Run /usage to use one.",
    "• Your plan was upgraded. Run /status for details.",
    "• Codex 0.161.0 is available. Restart to update.",
    "• Weekly limits reset Monday.\n• You have 1 usage limit reset available.",
)
_BANNERS = ("│ >_ OpenAI Codex (v0.153.2)  │", "│ >_ OpenAI Codex (v0.160.0)  │")
_COMPOSERS = ("› Ask Codex to do anything", "» Ask Codex to do anything")
# "" is the resized-viewport frame whose status bar is out of view -- see the
# comment in _body_rows. Such a frame is not recognised as an idle composer at
# all; the invariant is that the notice does not change THAT answer either.
_FOOTERS = ("", "  gpt-5.6-sol default · /tmp/cao/work", "  ? for shortcuts    62% context left")


def _frame(banner: str, composer: str, footer: str, notice: str = "") -> str:
    """Render a pre-turn startup frame, with or without a notice bullet."""
    rows = ["╭─────────────────────────────╮", banner, "╰─────────────────────────────╯"]
    if notice:
        rows += ["", notice]
    rows += ["", composer]
    if footer:
        rows += ["", footer]
    return "\n".join(rows)


_GEOMETRIES = tuple(itertools.product(_BANNERS, _COMPOSERS, _FOOTERS))


def _verdicts(pane: str) -> tuple[bool, bool, TerminalStatus]:
    """Every verdict #739 showed to be glyph-sensitive, read off one frame."""
    provider = CodexProvider("test1234", "test-session", "window-0")
    return (
        _is_pre_turn_startup_frame(pane),
        _has_startup_idle_composer(pane),
        provider.get_status(pane),
    )


class TestNoticeBulletChangesNoVerdict:
    """Startup chrome is chrome: adding a notice bullet decides nothing."""

    @pytest.mark.parametrize("notice", _NOTICES)
    @pytest.mark.parametrize("banner,composer,footer", _GEOMETRIES)
    def test_notice_changes_no_verdict(self, notice, banner, composer, footer):
        without = _frame(banner, composer, footer)
        with_notice = _frame(banner, composer, footer, notice)
        assert _verdicts(with_notice) == _verdicts(without)

    @pytest.mark.parametrize("banner,composer,footer", [g for g in _GEOMETRIES if g[-1]], ids=str)
    def test_a_submitted_turn_does_change_the_verdict(self, banner, composer, footer):
        """The invariant above must not be satisfiable by ignoring the frame.

        A dispatched user cell with a reply under it is real turn evidence, so
        it has to move at least one verdict off its pre-turn value. Footered
        geometries only: the placeholder-composer route in
        ``_has_startup_idle_composer`` deliberately requires the status footer
        below the composer, so a frame whose status bar is out of view carries
        no readiness verdict to discriminate.
        """
        pre_turn = _frame(banner, composer, footer)
        dispatched = _frame(
            banner, composer, footer, "› summarize the repo\n• Here is the summary."
        )
        assert _verdicts(dispatched) != _verdicts(pre_turn)


# --------------------------------------------------------------------------
# 2. AST inventory: every site that reads the ambiguous glyph is classified.
# --------------------------------------------------------------------------

# The names through which a `•` becomes "a turn happened".
_AMBIGUOUS_TURN_EVIDENCE = frozenset(
    {
        "STARTUP_ACTIVITY_PATTERN",
        "ASSISTANT_PREFIX_PATTERN",
        "TURN_ACTIVITY_PATTERN",
        "_find_assistant_marker",
    }
)
_PRE_TURN_GATE = "_is_pre_turn_startup_frame"

# function name -> what establishes that a turn exists before the glyph is
# believed. "pre_turn_frame" consults _PRE_TURN_GATE; "user_anchored" reads only
# below a submitted user cell; "dispatch_attributed" is reached only once a
# dispatch is attributed to this terminal; "not_a_verdict" locates or extracts
# text and returns no readiness/completion answer. Adding a site here is the
# point: it forces whoever adds one to say which of these they have.
_TURN_EVIDENCE_SITES = {
    "_has_startup_idle_composer": "pre_turn_frame",
    "_status_of_frame": "pre_turn_frame",
    "_turn_ended_with_footer": "user_anchored",
    "_has_current_turn_activity": "dispatch_attributed",
    "_has_visible_unattributed_reply": "dispatch_attributed",
    "_find_assistant_marker": "not_a_verdict",
    "_find_response_marker": "not_a_verdict",
    "_is_transcript_marker": "not_a_verdict",
    "extract_last_message_from_script": "not_a_verdict",
}

_SOURCE = Path(codex_module.__file__).read_text(encoding="utf-8")


def _referenced_names(node: ast.AST) -> set[str]:
    """Every bare name and attribute referenced inside a node."""
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    return names | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def _glyph_sites() -> dict[str, set[str]]:
    """Map each codex.py function that reads the glyph to the names it uses."""
    sites: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(_SOURCE)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = _referenced_names(node)
        if names & _AMBIGUOUS_TURN_EVIDENCE:
            sites[node.name] = names
    return sites


class TestTurnEvidenceSitesAreEnumerated:
    """Every reader of the ambiguous `•` is inventoried and preconditioned."""

    def test_inventory_matches_the_allowlist(self):
        found = set(_glyph_sites())
        allowed = set(_TURN_EVIDENCE_SITES)
        assert found == allowed, (
            "codex.py's readers of an ambiguous turn-activity glyph changed.\n"
            f"  new, unclassified: {sorted(found - allowed)}\n"
            f"  gone from codex.py: {sorted(allowed - found)}\n"
            "A new site must be added to _TURN_EVIDENCE_SITES with the "
            "precondition that establishes a turn was dispatched (#739). A `•` "
            "is also the account notice Codex prints before any turn exists."
        )

    @pytest.mark.parametrize("function", sorted(_TURN_EVIDENCE_SITES))
    def test_verdict_sites_consult_the_pre_turn_gate(self, function):
        """Derived from the code, so relabelling an allowlist entry cannot pass.

        Two shapes must consult ``_is_pre_turn_startup_frame``: anything that
        can return COMPLETED off the glyph (#739 defect 2), and anything that
        reads the startup window via ``STARTUP_ACTIVITY_PATTERN`` (defect 1).
        """
        names = _glyph_sites().get(function, set())
        emits_completed = "COMPLETED" in names
        reads_startup_window = "STARTUP_ACTIVITY_PATTERN" in names
        if not (emits_completed or reads_startup_window):
            pytest.skip(f"{function} emits no readiness/completion verdict")
        assert _PRE_TURN_GATE in names, (
            f"{function}() decides "
            f"{'COMPLETED' if emits_completed else 'startup readiness'} from a `•` "
            f"glyph without consulting {_PRE_TURN_GATE}(). On a frame that has had "
            "no turn that glyph is an account notice, which stalled every launch "
            "and reported untasked workers as free (#739)."
        )
