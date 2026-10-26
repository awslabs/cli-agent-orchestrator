"""Guard: a dead-code gate that removes imports must also prove nothing still uses them.

Found in haofeif's review of #1006 (P2-1). That PR deleted ``Optional`` from the
``typing`` import in ``utils/agent_profiles.py`` because nothing used it. #996 then
landed on ``main`` with ``profile_declares_capability(profile_name: Optional[str], ...)``.
Git merged the two without a textual conflict, the module raised ``NameError`` at
import, 132 test modules failed collection, and ``ruff check`` was green, because the
gate selected only F401 (unused import) and F841 (unused local). F401's autofix REMOVES
names; nothing in the gate checked that a removed name was not still READ.

Two checks lock the class, and each covers a stated scope (haofeif, #1006 re-review
P3-2: an earlier wording claimed F821 "proves nothing still uses the removed name";
ruff is per-file and cannot):

1. ``test_ruff_gate_reports_an_undefined_name`` -- the repo's own ruff configuration
   (``pyproject.toml``, not ``--isolated``) must report F821 for a module that uses a
   name it never imported, and F822 for a module that lists one in ``__all__``. Scope:
   uses of the removed name IN THE SAME FILE, including quoted and
   ``from __future__ import annotations`` annotations. This pins both rules in
   ``[tool.ruff.lint] select``; dropping one fails here before CI's lint step.
2. ``test_every_module_under_src_imports_in_a_fresh_interpreter`` -- every ``.py`` file
   under ``src/`` imports as a module, in a subprocess with a scratch ``HOME`` and
   ``CAO_HOME_DIR``. Scope: anything that runs at import time, so a MODULE-LEVEL
   ``from a import X`` where ``X`` was removed from ``a``, and any import-time error in
   a module no test happens to import. The subprocess is deliberate: in-process, a
   module ``conftest.py`` already imported would pass without being exercised here.

   The module list comes from the FILES, not from ``pkgutil.walk_packages``. The walk
   only descends into directories that have an ``__init__.py``, and ``services/`` and
   ``clients/`` have none (they are namespace-package portions), so the first version
   of this test silently covered 180 of 277 modules (haofeif, #1006 re-review P3-1).
   The test now enumerates ``src/**/*.py`` on its own side as well and requires the
   subprocess to have attempted exactly that set, so the enumeration cannot drift from
   what is on disk without failing here.

   The same walk also resolves every name in each module's own ``__all__``, because
   check 1 cannot: stable ruff does not apply F822 to ``__init__.py`` files (``ruff rule
   F822``: "In preview, this rule will flag undefined names in ``__init__.py`` file"),
   and 13 of the 26 ``__all__`` lists under ``src/`` are in one. A stale entry there
   passed both checks and raised ``AttributeError`` only at the first
   ``from pkg import *`` at run time (haofeif, #1006 review at ``a4db1db7``, P3-1). The
   check is one ``hasattr`` per exported name on a module the walk has already imported,
   so it costs nothing and covers all 26 lists, not only the 13 ruff can see.

Outside both: a function-scope ``from a import X``, an attribute use ``a.X``, and a
``mock.patch("pkg.a.X")`` target string. Those fail only when a unit test executes
them (ImportError / AttributeError), so a green lint step plus a green import guard is
not proof that no use of a removed name remains anywhere.

Neither check pins a module count. Counts drift with every file added; the assertions
are "the attempted set equals the files on disk", "no module fails", and "every
``__all__`` name resolves".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
SRC_ROOT = REPO_ROOT / "src"

# Modules the enumeration must reach, each chosen because an earlier version of this
# guard, or the review of it, showed a gap there:
#   - utils.agent_profiles: the module whose import broke in the #1006 merge.
#   - services.agui.run_plane, clients.database: live in directories WITHOUT an
#     __init__.py, which pkgutil.walk_packages never enters (P3-1 of the re-review).
SENTINEL_MODULES = (
    "cli_agent_orchestrator.utils.agent_profiles",
    "cli_agent_orchestrator.services.agui.run_plane",
    "cli_agent_orchestrator.clients.database",
)


def modules_under(src_root: Path) -> set[str]:
    """Dotted module name for every .py file under ``src_root``.

    ``pkg/__init__.py`` is the module ``pkg``; ``pkg/mod.py`` is ``pkg.mod``. A directory
    without ``__init__.py`` contributes its files as modules of a namespace-package
    portion, which is how Python imports them today, and is exactly what
    ``pkgutil.walk_packages`` misses.
    """
    names = set()
    for path in src_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = path.relative_to(src_root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.add(".".join(parts))
    return names


# Runs in the subprocess. argv[1] is the src root; the enumeration is the same as
# modules_under() above, repeated here on purpose so the test can compare the two.
#
# The `__all__` pass is what covers ruff's F822 blind spot (see the docstring): for each
# module that imported, every name in its own `__all__` must resolve as an attribute.
# That is precisely what `from <module> import *` does, minus the side effect of binding
# 26 modules' worth of names into this interpreter, and it names the offender.
_IMPORT_ALL = r"""
import importlib, json, pathlib, sys, traceback

src_root = pathlib.Path(sys.argv[1])
attempted, failures, dangling_all = [], [], []
for path in sorted(src_root.rglob("*.py")):
    if "__pycache__" in path.parts:
        continue
    parts = path.relative_to(src_root).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    name = ".".join(parts)
    attempted.append(name)
    try:
        module = importlib.import_module(name)
    except BaseException as exc:  # report every failure shape, SystemExit included
        failures.append(
            {
                "module": name,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=3),
            }
        )
        continue
    exported = getattr(module, "__all__", None)
    if exported is None:
        continue
    missing = [str(n) for n in exported if not hasattr(module, str(n))]
    if missing:
        dangling_all.append({"module": name, "missing": missing})
print(
    json.dumps(
        {"attempted": attempted, "failures": failures, "dangling_all": dangling_all}
    )
)
"""


@pytest.mark.parametrize(
    ("rule", "source"),
    [
        pytest.param(
            "F821",
            "def profile_declares_capability(profile_name: Optional[str]) -> bool:\n"
            "    return profile_name is not None\n",
            id="F821-undefined-name",
        ),
        pytest.param(
            "F822",
            '__all__ = ["Optional"]\n',
            id="F822-undefined-name-in-__all__",
        ),
    ],
)
def test_ruff_gate_reports_an_undefined_name(tmp_path: Path, rule: str, source: str) -> None:
    """The repo's ruff config must report F821 and F822. Static half of the guard:
    F401's autofix removes an import; these report a same-file use of the removed name."""
    broken = tmp_path / "uses_a_name_it_never_imported.py"
    broken.write_text(source, encoding="utf-8")
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
        "ruff passed a module that uses a name it never imported; "
        f"{rule} has left [tool.ruff.lint] select\n" + result.stdout + result.stderr
    )
    assert rule in result.stdout and "Optional" in result.stdout, result.stdout + result.stderr


def test_every_module_under_src_imports_in_a_fresh_interpreter(tmp_path: Path) -> None:
    """Every .py file under src/ imports. Runtime half of the guard: catches the
    NameError from #1006's merge, and the ImportError shapes F821 cannot see."""
    expected = modules_under(SRC_ROOT)
    for sentinel in SENTINEL_MODULES:
        assert sentinel in expected, f"{sentinel} is not under src/; update SENTINEL_MODULES"

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["CAO_HOME_DIR"] = str(tmp_path / ".cao")
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_ALL, str(SRC_ROOT)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    attempted = set(report["attempted"])

    assert attempted == expected, (
        "the subprocess did not attempt exactly the modules on disk\n"
        f"  missing: {sorted(expected - attempted)}\n"
        f"  extra:   {sorted(attempted - expected)}"
    )
    assert not report["failures"], "modules under src/ that do not import:\n" + "\n".join(
        f"  {f['module']}: {f['error']}\n{f['traceback']}" for f in report["failures"]
    )
    assert not report["dangling_all"], (
        "__all__ names that do not resolve, so `from <module> import *` raises "
        "AttributeError (ruff's F822 does not reach __init__.py outside preview):\n"
        + "\n".join(f"  {d['module']}: {', '.join(d['missing'])}" for d in report["dangling_all"])
    )
