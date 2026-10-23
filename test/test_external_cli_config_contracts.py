"""Guard test: when CAO restates an external CLI's config-file contract, every
operator-facing surface must carry the layout that CLI actually accepts.

#967 was a documentation-contract drift, not a logic bug. ``codexProfile`` is
the only route to a sandboxed Codex worker, and CAO told operators to put the
settings in a ``[profiles.<name>]`` table inside ``~/.codex/config.toml`` --
the layout codex-cli 0.162 refuses outright. The flag CAO *builds*
(``--profile <name>``) never changed, every provider test asserts on that argv,
so the whole suite stayed green while the instructions for the file the flag
*reads* went stale at eight independent sites: ``docs/codex-cli.md`` (x3),
``docs/hermes.md``, the ``codexProfile`` field comment, two comments in
``providers/codex.py``, and the published ``docusaurus/docs`` mirror.

The class is "the same external-tool contract is restated in prose at N
hand-counted sites with no single source of truth". The cure is to stop
counting by hand: this test enumerates operator-facing files *mechanically*, so
a surface nobody remembered -- including one added after the fix -- is covered
without anyone adding it to a list. The first fix for #967 did rewrite seven
sites and ship a regression test, and still missed
``docusaurus/docs/features/profiles.md`` precisely because both the fix and the
test named their sites by hand.

Add a ``Contract`` entry whenever CAO documents the layout of a config file
owned by another CLI.

Scope, stated honestly: ``CONTRACTS`` is a hand-maintained table of one entry,
and each entry's oracle is lexical, not semantic -- it catches the rejected
layout and the verbs an operator is likely to write, not every possible
paraphrase.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Tuple

import pytest

import cli_agent_orchestrator

_REPO_ROOT = Path(cli_agent_orchestrator.__file__).parents[2]

# Operator-facing text. ``test/`` is excluded on purpose: a test legitimately
# names the rejected layout in order to assert against it, and CHANGELOG.md is
# a historical record of what the docs used to say.
_TEXT_SUFFIXES = frozenset({".md", ".mdx", ".py", ".ts", ".tsx", ".txt"})
_PRUNED_DIRS = frozenset(
    {
        ".git",
        ".sweep",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "test",
        "vendor",
    }
)
_EXCLUDED_FILES = frozenset({"CHANGELOG.md"})

# How many lines count as "next to". A stale instruction stands alone; a
# correct warning about the rejected layout always sits beside the real one.
_WINDOW = 6


@dataclass(frozen=True)
class Contract:
    """An external CLI's config-file layout that CAO restates to operators."""

    knob: str
    # The layout the external CLI rejects (or no longer reads).
    rejected: "re.Pattern[str]"
    # The layout it does read. Must appear within _WINDOW lines of any
    # mention of ``rejected``, and of any line that defines ``knob``.
    accepted: "re.Pattern[str]"
    # A line that tells an operator what ``knob`` points at.
    defines_knob: "re.Pattern[str]"
    why: str


CONTRACTS: Tuple[Contract, ...] = (
    Contract(
        knob="codexProfile",
        rejected=re.compile(
            r"\[profiles\.|(?<![\w./-])profiles\.[\w<>-]+\."
            # The top-level legacy selector, also rejected by codex-cli 0.162+:
            # a whole TOML key/value line, `profile = "<name>"` (optionally
            # indented, optionally with a trailing comment) and nothing else.
            # Matching the bare `profile="..."` token anywhere on a line flagged
            # CAO's own Python keyword argument (`set_table(..., profile="worker")`)
            # as the codex layout; a kwarg is never alone on its line without a
            # trailing `,` or `)`, and a copyable TOML line never has one.
            r"|^\s*profile\s*=\s*[\"'][\w<>.-]*[\"']\s*(?:#.*)?$"
        ),
        accepted=re.compile(
            r"(?:\$CODEX_HOME|~/\.codex|\$\{?CODEX_HOME\}?)/[\w<>.*-]+\.config\.toml"
        ),  # tied to the codex home so an unrelated *.config.toml cannot satisfy the witness
        defines_knob=re.compile(
            r"codexProfile.*\b(?:Names|names|points|block|table|inside|selects|reads|resolves|refers|maps)\b"
            r"|\b(?:Names|names|points|selects|reads|resolves|refers|maps)\b.*codexProfile"
        ),
        why=(
            "codex-cli 0.162+ loads profile settings only from "
            "$CODEX_HOME/<name>.config.toml; with a legacy [profiles.<name>] table "
            "present, `codex --profile <name>` exits 1 before drawing a banner (#967)"
        ),
    ),
)


def _operator_facing_files() -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _PRUNED_DIRS]
        for filename in filenames:
            if filename in _EXCLUDED_FILES:
                continue
            path = Path(dirpath) / filename
            if path.suffix in _TEXT_SUFFIXES:
                yield path


def _violations(contract: Contract, trigger: "re.Pattern[str]") -> list[str]:
    """Lines matching ``trigger`` with no accepted layout within the window."""
    found: list[str] = []
    for path in _operator_facing_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):  # pragma: no cover - binary/unreadable
            continue
        for index, line in enumerate(lines):
            if not trigger.search(line):
                continue
            window = "\n".join(
                lines[max(0, index - _WINDOW) : min(len(lines), index + _WINDOW + 1)]
            )
            if not contract.accepted.search(window):
                rel = path.relative_to(_REPO_ROOT)
                found.append(f"{rel}:{index + 1}: {line.strip()[:120]}")
    return found


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c.knob)
def test_no_surface_restates_the_rejected_layout_alone(contract: Contract) -> None:
    """Naming the rejected layout is fine; naming it without the real one is not."""
    offenders = _violations(contract, contract.rejected)
    assert not offenders, (
        f"{contract.knob}: these surfaces mention the rejected config layout with no "
        f"mention of {contract.accepted.pattern} within {_WINDOW} lines, so an operator "
        f"copies the layout that fails -- {contract.why}:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c.knob)
def test_every_surface_defining_the_knob_names_the_file_it_reads(contract: Contract) -> None:
    """A field table that says what the knob names must say which file to create."""
    offenders = _violations(contract, contract.defines_knob)
    assert not offenders, (
        f"{contract.knob}: these surfaces say what the knob points at without naming "
        f"{contract.accepted.pattern} -- {contract.why}:\n  " + "\n  ".join(offenders)
    )


# Lines a scanned surface may legitimately contain. The ``rejected`` oracle must
# be exactly the external CLI's grammar: a guard that also matches CAO's own
# API spelled with the same word flags a correct example and tells its author
# the line "copies the rejected codex layout", which is false.
_NOT_THE_REJECTED_LAYOUT = (
    'set_table("model.route", "small", profile="worker")',
    '    profile="worker",',  # a kwarg on its own line inside a multi-line call
    "    profile='worker')",
    'Example: `set_table("model.route", "small", profile="worker")`',
    'launch(profile = "worker")',
    '    const launch = { profile: "worker" };',
    "the `profile` field selects which agent profile runs",
    'codex --profile cao_reviewer  # not `profile = "..."` inside config.toml',
)
_THE_REJECTED_LAYOUT = (
    'profile = "cao_reviewer"',
    "profile = 'cao_reviewer'",
    '  profile = "cao_reviewer"',
    'profile="cao_reviewer"',
    'profile = "<name>"',
    'profile = "cao_reviewer"  # legacy selector',
    "[profiles.cao_reviewer]",
    'profiles.cao_reviewer.model = "gpt-5"',
)


@pytest.mark.parametrize("line", _NOT_THE_REJECTED_LAYOUT)
def test_codex_rejected_layout_oracle_ignores_caos_own_profile_keyword(line: str) -> None:
    assert not CONTRACTS[0].rejected.search(line), line


@pytest.mark.parametrize("line", _THE_REJECTED_LAYOUT)
def test_codex_rejected_layout_oracle_matches_the_toml_forms(line: str) -> None:
    assert CONTRACTS[0].rejected.search(line), line
