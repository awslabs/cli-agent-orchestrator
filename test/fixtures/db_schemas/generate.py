"""Generate DDL-only fixtures with the actual released database initializers."""

import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRATCH = Path.home() / ".cache/cao-scratch/cao3-775-gen"
TAGS = ("v2.4.1", "v2.5.0", "v2.5.1", "v2.5.2", "v2.5.4", "v2.5.5")
PYTHON = ROOT / ".venv/bin/python"
INIT = "from cli_agent_orchestrator.clients.database import init_db; init_db()"


def generate() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    homes = SCRATCH / "homes"
    homes.mkdir(exist_ok=True)
    chain = homes / "chain"
    for tag in (*TAGS, "3.0-beta1"):
        ref = "cd9d69330d5977719b8b0a620211ac62b727d561" if tag == "3.0-beta1" else tag
        commit = subprocess.check_output(
            ("git", "-C", str(ROOT), "rev-parse", ref), text=True
        ).strip()
        checkout = SCRATCH / tag
        subprocess.run(
            ("git", "-C", str(ROOT), "worktree", "add", "--detach", str(checkout), commit),
            check=True,
        )
        try:
            home = homes / tag
            for target in (home, chain) if tag in TAGS else (home,):
                scratch_home = SCRATCH / "isolated-home"
                scratch_home.mkdir(exist_ok=True)
                subprocess.run(
                    (
                        "env",
                        "-i",
                        "PATH=/usr/bin:/bin",
                        f"HOME={scratch_home}",
                        f"CAO_HOME_DIR={target}",
                        f"PYTHONPATH={checkout / 'src'}",
                        str(PYTHON),
                        "-c",
                        INIT,
                    ),
                    check=True,
                )
            db = home / "db/cli-agent-orchestrator.db"
            write_ddl(tag, commit, db)
            if tag == "v2.5.5":
                write_ddl("v2.4.1-to-v2.5.5", commit, chain / "db/cli-agent-orchestrator.db")
        finally:
            subprocess.run(
                ("git", "-C", str(ROOT), "worktree", "remove", "--force", str(checkout)),
                check=True,
            )


def write_ddl(name: str, commit: str, db: Path) -> None:
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name"
        ).fetchall()
    output = Path(__file__).parent / f"{name}.sql"
    output.write_text(
        f"-- Schema from {name}, commit {commit}; DDL only, no rows.\n"
        + "\n\n".join(row[0].rstrip("; ") + ";" for row in rows)
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    generate()
