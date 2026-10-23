"""Every page that documents eager inbox delivery states the dialog hold (#896).

WHY THIS EXISTS. #896's fix changed what eager delivery does while a Claude Code
dialog is open: the message is now held, not pasted into the dialog. The fix
updated ``docs/claude-code.md`` and ``docs/inbox-delivery.md`` and missed the
published docusaurus guide, which kept saying messages are delivered "even
during PROCESSING or WAITING_USER_ANSWER". The whole suite was green because
nothing tied the three restatements of the contract to each other, which is the
same shape ``test_workflows_doc_surface_is_not_stale.py`` guards for the
workflow tables.

So this test finds every Markdown page that mentions the eager-delivery switch
and requires that any paragraph on it naming WAITING_USER_ANSWER also says what
happens there: the state is excluded, the message is held, or the behaviour is
tied to the provider property that decides it. A page that documents the switch
without mentioning the state at all passes -- it makes no claim -- but a page
that names the state has to get it right.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parents[1]
DOC_ROOTS = (REPO / "docs", REPO / "docusaurus" / "docs")
SWITCH = "CAO_EAGER_INBOX_DELIVERY"
STATE = "WAITING_USER_ANSWER"

#: A paragraph that names the state must also carry one of these, i.e. say that
#: eager delivery does not type into the dialog. The property name counts because
#: it is the thing that actually decides the behaviour per provider.
HOLD_MARKERS = (
    "excluded",
    "held",
    "hold",
    "PENDING",
    "blocks_orchestrated_input_while_waiting_user_answer",
)

#: The exact stale shape the docusaurus guide shipped with. Named so the failure
#: message can point at it even when a paragraph happens to contain a hold word
#: for some other reason.
STALE_CLAIM = re.compile(r"during\s+PROCESSING\s+or\s+WAITING_USER_ANSWER", re.IGNORECASE)


def _eager_delivery_pages() -> List[Path]:
    pages: List[Path] = []
    for root in DOC_ROOTS:
        assert root.is_dir(), f"expected a docs tree at {root}"
        for path in sorted(root.rglob("*.md")):
            if SWITCH in path.read_text(encoding="utf-8"):
                pages.append(path)
    return pages


#: A paragraph is an eager-delivery claim when it mentions the feature or its
#: switch. Paragraphs that name the state for another reason (the status
#: detection table, the trust-prompt exclusion) make no delivery claim and are
#: not this test's business.
EAGER_CLAIM = re.compile(r"eager|" + re.escape(SWITCH), re.IGNORECASE)


def _paragraphs_naming_the_state(page: Path) -> Dict[int, str]:
    """``{first_line_number: paragraph}`` for each eager-delivery paragraph naming the state."""
    out: Dict[int, str] = {}
    lines = page.read_text(encoding="utf-8").splitlines()
    start = 0
    for index in range(len(lines) + 1):
        at_end = index == len(lines)
        if at_end or not lines[index].strip():
            block = "\n".join(lines[start:index])
            if STATE in block and EAGER_CLAIM.search(block):
                out[start + 1] = block
            start = index + 1
    return out


def test_the_scan_is_not_vacuous() -> None:
    """Guard the guard: the two pages this test was written for must be in the scan."""
    pages = {p.relative_to(REPO).as_posix() for p in _eager_delivery_pages()}
    assert "docs/claude-code.md" in pages, pages
    assert "docusaurus/docs/guides/building-with-claude-code.md" in pages, pages
    checked = sum(len(_paragraphs_naming_the_state(p)) for p in _eager_delivery_pages())
    assert checked >= 2, "no eager-delivery page names WAITING_USER_ANSWER; the parse is broken"


def test_every_eager_delivery_page_states_the_dialog_hold() -> None:
    problems: List[str] = []
    for page in _eager_delivery_pages():
        rel = page.relative_to(REPO).as_posix()
        for line, paragraph in _paragraphs_naming_the_state(page).items():
            if STALE_CLAIM.search(paragraph):
                problems.append(
                    f"{rel}:{line} still claims delivery happens during WAITING_USER_ANSWER; "
                    "match the wording in docs/claude-code.md (#896)"
                )
            elif not any(marker in paragraph for marker in HOLD_MARKERS):
                problems.append(
                    f"{rel}:{line} names WAITING_USER_ANSWER without saying the message is "
                    f"held there; add one of {HOLD_MARKERS} or drop the claim (#896)"
                )
    assert not problems, "\n".join(problems)
