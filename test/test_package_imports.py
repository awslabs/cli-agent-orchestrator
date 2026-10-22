"""Guard: a dead-code gate that removes imports must also prove nothing still uses them.

Found in haofeif's review of #1006 (P2-1). That PR deleted ``Optional`` from the
``typing`` import in ``utils/agent_profiles.py`` because nothing used it. #996 then
landed on ``main`` with ``profile_declares_capability(profile_name: Optional[str], ...)``.
Git merged the two without a textual conflict, the module raised ``NameError`` at
import, 132 test modules failed collection, and ``ruff check`` was green, because the
gate selected only F401 (unused import) and F841 (unused local). F401's autofix REMOVES
names; nothing in the gate checked that a removed name was not still READ.

Two checks lock the class:

1. ``test_ruff_gate_reports_an_undefined_name`` -- the repo's own ruff configuration
   (``pyproject.toml``, not ``--isolated``) must report F821 for a module that uses a
   name it never imported. This pins the rule in ``[tool.ruff.lint] select``; dropping
   it fails here before it fails in CI's lint step.
2. ``test_every_module_under_src_imports_in_a_fresh_interpreter`` -- every module in
   every package shipped from ``src/`` imports, in a subprocess with a scratch ``HOME``
   and ``CAO_HOME_DIR``. This is the runtime half: it also catches the shapes a static
   name check cannot see, such as ``from a import X`` where ``X`` was removed from ``a``
   as an "unused" re-export, and an import-time error in a module no test happens to
   import. The subprocess is deliberate: in-process, a module ``conftest.py`` already
   imported would pass without being exercised here.

Neither check pins a module count. Counts drift with every file added; the assertion
is "no module fails", plus a floor that the walk really did reach the module that broke.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# The two top-level packages under src/. Walked by the subprocess below, so a new
# package added here is covered by adding its name; a module added inside either
# package is covered automatically.
SRC_PACKAGES = ("cli_agent_orchestrator", "cao_workflow")

# The module whose import broke in the #1006 merge. The walk must reach it, so the
# test cannot pass vacuously by walking an empty or wrong tree.
SENTINEL_MODULE = "cli_agent_orchestrator.utils.agent_profiles"

_IMPORT_ALL = r"""
import importlib, json, pkgutil, sys, traceback
packages = sys.argv[1:]
walked, failures = [], []


def record(name):
    exc = sys.exc_info()[1]
    failures.append(
        {
            "module": name,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=3),
        }
    )


def attempt(name):
    walked.append(name)
    try:
        importlib.import_module(name)
    except BaseException:  # report every failure shape, SystemExit included
        record(name)


for package_name in packages:
    attempt(package_name)
    package = sys.modules.get(package_name)
    if package is None:
        continue
    # walk_packages imports each sub-PACKAGE itself to find its children. Without
    # onerror it swallows an ImportError there and skips the whole subtree, so a
    # broken __init__.py would hide every module under it. With onerror, every
    # exception during that import is recorded against the package instead.
    for info in pkgutil.walk_packages(package.__path__, package.__name__ + ".", onerror=record):
        attempt(info.name)
print(json.dumps({"walked": walked, "failures": failures}))
"""


def test_ruff_gate_reports_an_undefined_name(tmp_path: Path) -> None:
    """The repo's ruff config must report F821. This is the static half of the guard:
    F401's autofix removes an import, F821 is what says a removed name is still used."""
    broken = tmp_path / "uses_a_name_it_never_imported.py"
    broken.write_text(
        "def profile_declares_capability(profile_name: Optional[str]) -> bool:\n"
        "    return profile_name is not None\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--config",
            str(PYPROJECT),
            "--output-format",
            "concise",
            str(broken),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "ruff passed a module that reads a name it never imported; "
        "F821 has left [tool.ruff.lint] select\n" + result.stdout + result.stderr
    )
    assert "F821" in result.stdout and "Optional" in result.stdout, result.stdout + result.stderr


def test_every_module_under_src_imports_in_a_fresh_interpreter(tmp_path: Path) -> None:
    """Every module shipped from src/ imports. Runtime half of the guard: catches the
    NameError from #1006's merge, and the ImportError shapes F821 cannot see."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["CAO_HOME_DIR"] = str(tmp_path / ".cao")
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_ALL, *SRC_PACKAGES],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])

    assert (
        SENTINEL_MODULE in report["walked"]
    ), f"the walk never reached {SENTINEL_MODULE}; walked {len(report['walked'])} modules"
    assert not report["failures"], "modules under src/ that do not import:\n" + "\n".join(
        f"  {f['module']}: {f['error']}\n{f['traceback']}" for f in report["failures"]
    )
