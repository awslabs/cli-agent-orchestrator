"""Hermetic retrieval measurements for the synthetic memory fixture corpus.

The harness deliberately uses the public MemoryService store/recall path.  It
never reads a configured CAO home and accepts only a home below the platform
temporary directory.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from sqlalchemy import create_engine

if TYPE_CHECKING:
    from cli_agent_orchestrator.services.memory_service import MemoryService

FIXTURE_DIR = Path(__file__).parent / "fixtures"
_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
_TIMESTAMP = re.compile(r"## \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

# Later phases extend these named arms.  Each arm has its own settings home and
# process, so a production default change cannot silently alter another arm.
ARMS: dict[str, dict[str, Any]] = {
    "default": {
        "settings": {"memory": {"parser": {"lenient": False}}},
        "recall": {
            "search_mode": "hybrid",
            "sort_by": "recency",
            "include_related": False,
            "limit": 10,
        },
    },
    "bm25": {
        "settings": {},
        "recall": {
            "search_mode": "bm25",
            "sort_by": "recency",
            "include_related": False,
            "limit": 10,
        },
    },
    "p1a_visibility": {
        "settings": {"memory": {"parser": {"lenient": True}}},
        "recall": {
            "search_mode": "hybrid",
            "sort_by": "recency",
            "include_related": False,
            "limit": 10,
        },
    },
    "p3_bm25": {
        "settings": {"memory": {"ranking": {"default_sort": "bm25"}}},
        "recall": {"include_related": False, "limit": 10},
    },
    "p4_relationships": {
        "settings": {},
        "recall": {
            "search_mode": "bm25",
            "sort_by": "recency",
            "include_related": True,
            "limit": 10,
        },
    },
}


def assert_temporary_home(home: Path) -> Path:
    """Return a resolved home only when it is safely below the temp directory."""
    resolved = home.resolve()
    if resolved == _TEMP_ROOT or _TEMP_ROOT not in resolved.parents:
        raise ValueError("memory eval home must be below the temporary directory")
    return resolved


def _read_json(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _context(home: Path) -> dict[str, str]:
    return {
        "terminal_id": "synthetic-memory-eval",
        "session_name": "synthetic-eval-session",
        "agent_profile": "synthetic-eval-agent",
        "cwd": str(home / "fictional-atlas-project"),
    }


def _fixture_content(note: dict[str, Any]) -> str:
    content = str(note["content"])
    if note.get("long_note"):
        content += (
            " "
            + (
                "The Atlas cartography team records this wholly fictional field observation "
                "for deterministic retrieval evaluation. "
            )
            * 160
        )
    return content


def _rewrite_fixture_shape(service: MemoryService, note: dict[str, Any], home: Path) -> None:
    """Apply intentional malformed historical shapes after the real store write."""
    key = str(note["key"])
    stored_key = service._sanitize_key(key)
    scope = str(note["scope"])
    scope_id = service.resolve_scope_id(scope, _context(home))
    wiki_path = service.get_wiki_path(scope, scope_id, stored_key)
    text = wiki_path.read_text(encoding="utf-8")
    shape = note["shape"]
    if shape == "compiled_without_heading":
        replacement = "## Compiled Atlas digest"
    elif shape == "minute_precision":
        replacement = f"## {str(note['date'])[:16]}Z"
    else:
        replacement = f"## {note['date']}"
    wiki_path.write_text(_TIMESTAMP.sub(replacement, text, count=1), encoding="utf-8")

    # The production writer truncates keys.  This fixture retains one
    # historical overlong on-disk key so later hygiene work has a stable input.
    if len(key) > len(stored_key):
        long_path = wiki_path.with_name(f"{key}.md")
        long_path.write_text(
            wiki_path.read_text(encoding="utf-8").replace(f"# {stored_key}", f"# {key}", 1),
            encoding="utf-8",
        )
        wiki_path.unlink()
        index_path = service.get_index_path(scope, scope_id)
        index_path.write_text(
            index_path.read_text(encoding="utf-8").replace(stored_key, key),
            encoding="utf-8",
        )


async def build_memory_home(home: Path, settings: dict[str, Any]) -> MemoryService:
    """Build the entire fixture corpus through the real in-process service."""
    resolved = assert_temporary_home(home)
    os.environ["CAO_HOME_DIR"] = str(resolved)

    from cli_agent_orchestrator.clients.database import Base
    from cli_agent_orchestrator.services.memory_service import MemoryService

    resolved.mkdir(parents=True, exist_ok=True)
    (resolved / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (resolved / "db").mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{resolved / 'db' / 'memory-eval.sqlite'}")
    Base.metadata.create_all(engine)
    service = MemoryService(base_dir=resolved / "memory", db_engine=engine)
    context = _context(resolved)

    for note in _read_json("notes.json"):
        await service.store(
            _fixture_content(note),
            scope=note["scope"],
            memory_type="project",
            key=note["key"],
            tags="synthetic,atlas",
            terminal_context=context,
        )
        _rewrite_fixture_shape(service, note, resolved)
    return service


def _rank(keys: list[str], gold: set[str]) -> int | None:
    for position, key in enumerate(keys, start=1):
        if key in gold:
            return position
    return None


@contextmanager
def _ordered_wiki_listing(*, reverse: bool):
    """Temporarily make the production wiki listing order explicit."""
    original_rglob = Path.rglob

    def ordered_rglob(path: Path, pattern: str):
        return iter(sorted(original_rglob(path, pattern), key=str, reverse=reverse))

    with patch.object(Path, "rglob", ordered_rglob):
        yield


def _uses_bm25_current_first(arm: dict[str, Any]) -> bool:
    recall = arm["recall"]
    settings = arm["settings"]
    return recall.get("search_mode") == "bm25" or (
        settings.get("memory", {}).get("ranking", {}).get("default_sort") == "bm25"
    )


async def _arm_metrics(
    service: MemoryService,
    home: Path,
    recall: dict[str, Any],
    *,
    strict_current_first: bool = False,
) -> dict[str, Any]:
    context = _context(home)
    questions = _read_json("questions.json")
    multihop = _read_json("questions_multihop.json")
    current_checks = _read_json("current_checks.json")
    ranks: dict[str, list[int | None]] = {"kw": [], "nl": []}
    payload_tokens: list[float] = []
    result_key_orders: list[list[str]] = []

    for question in questions:
        for wording in ("kw", "nl"):
            results = await service.recall(
                query=question[wording],
                terminal_context=context,
                scan_all=True,
                record_access=False,
                **recall,
            )
            keys = [memory.key for memory in results if not memory.is_related]
            result_key_orders.append(keys)
            ranks[wording].append(_rank(keys, set(question["gold"])))
            payload_tokens.append(sum(len(memory.content) for memory in results) / 4)

    coverage: list[float] = []
    full: list[bool] = []
    for question in multihop:
        results = await service.recall(
            query=question["query"],
            terminal_context=context,
            scan_all=True,
            record_access=False,
            **recall,
        )
        found = {memory.key for memory in results}
        needed = set(question["needs"])
        coverage.append(len(found & needed) / len(needed))
        full.append(needed <= found)

    async def current_first_for_listing() -> list[bool]:
        first: list[bool] = []
        for check in current_checks:
            results = await service.recall(
                query=check["query"],
                terminal_context=context,
                scan_all=True,
                record_access=False,
                **recall,
            )
            keys = [memory.key for memory in results if not memory.is_related]
            current_rank = _rank(keys, {check["current"]})
            stale_rank = _rank(keys, {check["stale"]})
            first.append(
                current_rank is not None and (stale_rank is None or current_rank < stale_rank)
            )
        return first

    if strict_current_first:
        with _ordered_wiki_listing(reverse=False):
            ascending_current_first = await current_first_for_listing()
        with _ordered_wiki_listing(reverse=True):
            descending_current_first = await current_first_for_listing()
        current_first = [
            ascending and descending
            for ascending, descending in zip(ascending_current_first, descending_current_first)
        ]
    else:
        current_first = await current_first_for_listing()

    def precision_at(wording: str, limit: int) -> float:
        return sum(rank is not None and rank <= limit for rank in ranks[wording]) / len(
            ranks[wording]
        )

    return {
        "kw_p1": precision_at("kw", 1),
        "kw_p3": precision_at("kw", 3),
        "nl_p1": precision_at("nl", 1),
        "nl_p3": precision_at("nl", 3),
        "multihop_full": sum(full) / len(full),
        "multihop_coverage": sum(coverage) / len(coverage),
        "current_first": sum(current_first) / len(current_first),
        "tokens_per_recall": sum(payload_tokens) / len(payload_tokens),
        # Keep hash-seed checks sensitive to retrieval-order regressions even
        # when aggregate ranking metrics happen to remain unchanged.
        "result_key_order_hash": hashlib.sha256(json.dumps(result_key_orders).encode()).hexdigest(),
    }


def _invisible_notes(service: MemoryService, home: Path) -> int:
    invisible = 0
    for note in _read_json("notes.json"):
        key = str(note["key"])
        scope = str(note["scope"])
        scope_id = service.resolve_scope_id(scope, _context(home))
        path = service.get_wiki_path(scope, scope_id, service._sanitize_key(key))
        if len(key) > 60:
            path = path.with_name(f"{key}.md")
        entry = {
            "key": key,
            "scope": scope,
            "scope_id": scope_id,
            "memory_type": "project",
            "tags": "synthetic,atlas",
        }
        if service._parse_wiki_file(path, path.read_text(encoding="utf-8"), entry) is None:
            invisible += 1
    return invisible


async def _evaluate_arm(home: Path, arm: dict[str, Any]) -> dict[str, Any]:
    service = await build_memory_home(home, dict(arm["settings"]))
    metrics = await _arm_metrics(
        service,
        home,
        dict(arm["recall"]),
        strict_current_first=_uses_bm25_current_first(arm),
    )
    metrics["invisible_notes"] = _invisible_notes(service, home)
    return metrics


def _evaluate_arm_in_child(home: Path, name: str, arm: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one arm where CAO_HOME_DIR is resolved before service imports."""
    child_env = os.environ.copy()
    child_env["CAO_HOME_DIR"] = str(home)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "test.memory_eval.harness",
            "--home",
            str(home),
            "--arm",
            name,
            "--arm-config",
            json.dumps(arm, sort_keys=True),
        ],
        check=True,
        capture_output=True,
        env=child_env,
        text=True,
    )
    return json.loads(completed.stdout)


def _evaluate(home: Path) -> dict[str, Any]:
    resolved = assert_temporary_home(home)
    if resolved.exists():
        raise ValueError("memory eval home must not already exist")
    return {
        "fixture_notes": len(_read_json("notes.json")),
        "arms": {
            name: _evaluate_arm_in_child(resolved / name, name, arm) for name, arm in ARMS.items()
        },
    }


def evaluate(home: Path) -> dict[str, Any]:
    """Seed and evaluate a synthetic memory home.  ``home`` must be fresh."""
    return _evaluate(assert_temporary_home(home))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True, help="fresh temporary fixture home")
    parser.add_argument("--arm", help="evaluate one arm")
    parser.add_argument("--arm-config", help="JSON configuration for --arm")
    args = parser.parse_args()
    home = assert_temporary_home(args.home)
    os.environ["CAO_HOME_DIR"] = str(home)
    if args.arm:
        arm = json.loads(args.arm_config) if args.arm_config else ARMS[args.arm]
        report = asyncio.run(_evaluate_arm(home, arm))
    else:
        if args.arm_config:
            parser.error("--arm-config requires --arm")
        report = evaluate(home)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
