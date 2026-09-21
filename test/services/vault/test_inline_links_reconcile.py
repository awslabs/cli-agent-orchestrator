"""Real-process integration coverage for relative inline Markdown vault links."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_reconcile_relative_inline_markdown_link_creates_vault_edge(tmp_path):
    repo_root = Path(__file__).resolve().parents[3]
    isolated_home = tmp_path / "cao-home"
    vault_root = tmp_path / "vault"
    lock_dir = tmp_path / "locks"
    env = os.environ.copy()
    env.update(
        {
            "CAO_HOME": str(isolated_home),
            "CAO_HOME_DIR": str(isolated_home),
            "PYTHONPATH": str(repo_root / "src"),
            "S7_LOCK_DIR": str(lock_dir),
            "S7_VAULT_ROOT": str(vault_root),
        }
    )
    env.pop("CAO_TERMINAL_ID", None)
    program = textwrap.dedent("""
        import asyncio
        import json
        import os
        from pathlib import Path

        from cli_agent_orchestrator.clients import database
        from cli_agent_orchestrator.clients.database import (
            MemoryRelationshipModel,
            VaultFindingModel,
            VaultNoteModel,
        )
        from cli_agent_orchestrator.services import memory_service, settings_service
        from cli_agent_orchestrator.services.memory_service import MemoryService
        from cli_agent_orchestrator.services.vault import reconcile as reconcile_module
        from cli_agent_orchestrator.services.vault import vault_lock
        from cli_agent_orchestrator.services.vault.config import (
            FolderMapping,
            VaultConfig,
            VaultSpec,
        )
        from cli_agent_orchestrator.utils import atomic_file

        root = Path(os.environ["S7_VAULT_ROOT"])
        mapped = root / "Mapped"
        target_dir = mapped / "Sub"
        target_dir.mkdir(parents=True)
        nested = mapped / "Nested"
        nested_target_dir = nested / "Sub"
        nested_target_dir.mkdir(parents=True)
        (root / "CAO").mkdir()
        (mapped / "Source.md").write_text(
            "[Target](Sub/Target.md)", encoding="utf-8"
        )
        (target_dir / "DotSource.md").write_text(
            "dot-source-needle [Target](./Target.md)", encoding="utf-8"
        )
        (nested / "ParentSource.md").write_text(
            "parent-source-needle [Target](../Sub/Target.md)", encoding="utf-8"
        )
        (nested / "MissingParentSource.md").write_text(
            "[missing](../Missing.md)", encoding="utf-8"
        )
        (nested / "DepthEscapeSource.md").write_text(
            "[escape](../../../Sub/Target.md)", encoding="utf-8"
        )
        (mapped / "ReentryEscapeSource.md").write_text(
            "[escape](../../Mapped/Sub/Target.md)", encoding="utf-8"
        )
        (mapped / "WikiSource.md").write_text("[[Target]]", encoding="utf-8")
        (target_dir / "Target.md").write_text("target", encoding="utf-8")
        (nested / "WrongInlineSource.md").write_text(
            "[wrong](Sub/Lookalike.md)", encoding="utf-8"
        )
        (target_dir / "Lookalike.md").write_text("lookalike", encoding="utf-8")
        (nested / "CorrectInlineSource.md").write_text(
            "[correct](Sub/Duplicate.md)", encoding="utf-8"
        )
        (target_dir / "Duplicate.md").write_text("root duplicate", encoding="utf-8")
        (nested_target_dir / "Duplicate.md").write_text(
            "nested duplicate", encoding="utf-8"
        )
        (nested / "WikiSuffixSource.md").write_text(
            "[[Sub/Lookalike]]", encoding="utf-8"
        )
        (nested / "BareWikiSource.md").write_text("[[Lookalike]]", encoding="utf-8")
        (nested / "ExactWikiSource.md").write_text(
            "[[Mapped/Sub/Lookalike.md]]", encoding="utf-8"
        )

        lock_dir = Path(os.environ["S7_LOCK_DIR"])
        lock_dir.mkdir()
        atomic_file.LOCK_DIR = lock_dir
        vault_lock.LOCK_DIR = lock_dir
        database.init_db()

        vault = VaultSpec(
            id="inline-link-test",
            root=str(root),
            managed_folder="CAO",
            max_note_bytes=4096,
            max_notes=100,
            max_frontmatter_bytes=1024,
            mappings=[
                FolderMapping(
                    folder="Mapped",
                    scope="global",
                ),
                FolderMapping(
                    folder="CAO",
                    scope="agent",
                    scope_id="writer",
                    writable=True,
                ),
            ],
        )
        reconcile_module.reconcile(vault, apply=True, run_id="inline-link-run")
        config = VaultConfig(enabled=True, vaults=[vault])
        settings_service.get_vault_config = lambda: config
        memory_service._is_memory_enabled = lambda: True
        service = MemoryService(base_dir=Path(os.environ["CAO_HOME"]) / "wiki")

        related = {}
        for query in ("dot-source-needle", "parent-source-needle"):
            recalled = asyncio.run(
                service.recall(
                    query=query,
                    scope="global",
                    search_mode="metadata",
                    include_related=True,
                    limit=1,
                )
            )
            related[query] = [
                (item.key, bool(getattr(item, "is_related", False)))
                for item in recalled
            ]

        with database.SessionLocal() as db:
            notes = {
                row.vault_relpath: row.cao_key
                for row in db.query(VaultNoteModel).all()
            }
            edges = [
                (row.source_key, row.target_key, row.status)
                for row in db.query(MemoryRelationshipModel)
                .filter_by(origin="vault")
                .all()
            ]
            findings = [
                (row.code, row.vault_relpath)
                for row in db.query(VaultFindingModel).all()
            ]
        print(
            "S7_RESULT="
            + json.dumps(
                {
                    "edges": sorted(edges),
                    "findings": sorted(findings),
                    "notes": notes,
                    "related": related,
                },
                sort_keys=True,
            )
        )
        """)

    completed = subprocess.run(
        [sys.executable, "-c", program],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )
    result_line = next(
        line for line in completed.stdout.splitlines() if line.startswith("S7_RESULT=")
    )
    result = json.loads(result_line.removeprefix("S7_RESULT="))
    target_key = result["notes"]["Mapped/Sub/Target.md"]
    lookalike_key = result["notes"]["Mapped/Sub/Lookalike.md"]
    nested_duplicate_key = result["notes"]["Mapped/Nested/Sub/Duplicate.md"]

    assert result["edges"] == sorted(
        [
            [result["notes"]["Mapped/Source.md"], target_key, "active"],
            [result["notes"]["Mapped/Sub/DotSource.md"], target_key, "active"],
            [result["notes"]["Mapped/Nested/ParentSource.md"], target_key, "active"],
            [result["notes"]["Mapped/WikiSource.md"], target_key, "active"],
            [
                result["notes"]["Mapped/Nested/CorrectInlineSource.md"],
                nested_duplicate_key,
                "active",
            ],
            [
                result["notes"]["Mapped/Nested/BareWikiSource.md"],
                lookalike_key,
                "active",
            ],
            [
                result["notes"]["Mapped/Nested/ExactWikiSource.md"],
                lookalike_key,
                "active",
            ],
        ]
    ), result
    assert result["findings"] == sorted(
        [
            ["link_dangling", "Mapped/Nested/WikiSuffixSource.md"],
            ["link_dangling", "Mapped/Nested/WrongInlineSource.md"],
            ["link_dangling", "Mapped/Nested/MissingParentSource.md"],
            ["link_target_invalid", "Mapped/Nested/DepthEscapeSource.md"],
            ["link_target_invalid", "Mapped/ReentryEscapeSource.md"],
        ]
    )
    for query, source_path in (
        ("dot-source-needle", "Mapped/Sub/DotSource.md"),
        ("parent-source-needle", "Mapped/Nested/ParentSource.md"),
    ):
        assert result["related"][query] == [
            [result["notes"][source_path], False],
            [target_key, True],
        ]
