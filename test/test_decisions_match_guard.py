"""#1199 AC19 and AC20: static guards on who reads decisions.match, and on decider names.

AC19. The local model catalog (``decisions.match`` in settings.json) is read only by the matcher
(``decisions/match.py``), its validator and hash (``decisions/catalog.py``) and the CLI
(``cli/commands/decisions.py``). Everything else gets it, already validated, through those. A
module outside that set that names the key, reads the ``match_catalog`` field, or imports the
catalog or matcher modules is a new reader, and fails here.

The scan walks every module under ``src/``. It resolves ``import x as y``, ``from x import y as
z``, relative imports, ``importlib.import_module`` and ``__import__`` with literal names, and
attribute chains through an imported package alias (``decisions.catalog``). It is static and
one hop deep: a value computed at run time, or a module reached through another module's
attributes, is outside what any AST check can see.

``EXEMPT`` names the modules that use the bare word ``match`` for something else, and exactly
how many times each does. A new ``match`` literal in one of them still fails, and so does an
entry that no longer matches its module.

AC20. No module under ``src/`` names a decider ``jev`` or ``jevk5``: core never branches on
which decider answered (D9). The design scopes this to ``src/``; ``test/`` legitimately builds
fakes under those names to prove that neutrality.
"""

from __future__ import annotations

import ast
from collections import Counter
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PACKAGE = "cli_agent_orchestrator"
CATALOG = PACKAGE + ".decisions.catalog"
MATCHER = PACKAGE + ".decisions.match"
# The readers: the matcher, its validator and hash, and the CLI.
READERS = {
    "cli_agent_orchestrator/decisions/catalog.py",
    "cli_agent_orchestrator/decisions/match.py",
    "cli_agent_orchestrator/cli/commands/decisions.py",
}
# Who may import each module. settings.py builds the validated snapshot and operator.py its
# status summary, both by calling the catalog module; the engine calls the matcher.
IMPORTERS = {
    CATALOG: READERS
    | {
        "cli_agent_orchestrator/decisions/settings.py",
        "cli_agent_orchestrator/decisions/operator.py",
    },
    MATCHER: READERS | {"cli_agent_orchestrator/decisions/engine.py"},
}
KEY_WORDS = ("match", "match_catalog")
# Modules that name the bare key "match" for something other than decisions.match, with the
# exact number of such literals and why. Only the bare key is allowed: "match_catalog",
# "decisions.match" and the .match_catalog field still fail in these modules too.
KIRO_RULE_KEY = "Kiro v3 permission-rule key `match`, not `decisions.match`"
EXEMPT = {
    "cli_agent_orchestrator/providers/kiro_v3_schema.py": (1, KIRO_RULE_KEY),
    "cli_agent_orchestrator/services/kiro_v3_validate.py": (3, KIRO_RULE_KEY),
    "cli_agent_orchestrator/utils/tool_mapping.py": (2, KIRO_RULE_KEY),
}
BARE_KEY = "names 'match'"
DECIDER_NAMES = ("jev", "jevk5")


def _module_name(relative: str) -> str:
    parts = relative[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _protected(name: str) -> str | None:
    for module in (CATALOG, MATCHER):
        if name == module or name.startswith(module + "."):
            return module
    return None


def _literal(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def scan(source: str, relative: str) -> dict[str, list[str]]:
    """Every AC19 and AC20 finding in one module, keyed by rule."""
    tree = ast.parse(source)
    module = _module_name(relative)
    package = module if relative.endswith("__init__.py") else module.rpartition(".")[0]
    docstrings = _docstrings(tree)
    findings: dict[str, list[str]] = {"reads": [], "imports": [], "names": []}
    aliases: dict[str, str] = {}

    def imported(target: str, line: int) -> None:
        protected = _protected(target)
        if protected is not None and relative not in IMPORTERS[protected]:
            findings["imports"].append(f"{relative}:{line}: imports {target}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported(alias.name, node.lineno)
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    aliases[head] = head
        elif isinstance(node, ast.ImportFrom):
            base = (
                resolve_name("." * node.level + (node.module or ""), package)
                if node.level
                else node.module or ""
            )
            for alias in node.names:
                full = f"{base}.{alias.name}" if base else alias.name
                imported(base, node.lineno)
                imported(full, node.lineno)
                aliases[alias.asname or alias.name] = full
        elif isinstance(node, ast.Call):
            func = node.func
            kind = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if kind in ("import_module", "__import__"):
                name = _literal(node.args[0] if node.args else None)
                if name is not None:
                    if name.startswith("."):
                        anchor = _literal(node.args[1]) if len(node.args) > 1 else None
                        name = resolve_name(name, anchor or package)
                    imported(name, node.lineno)
                    fromlist = node.args[3] if len(node.args) > 3 else None
                    if isinstance(fromlist, (ast.List, ast.Tuple)):
                        for item in map(_literal, fromlist.elts):
                            if item:
                                imported(f"{name}.{item}", node.lineno)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr == "match_catalog" and relative not in READERS:
                findings["reads"].append(f"{relative}:{node.lineno}: reads .match_catalog")
            chain = []
            value: ast.expr = node
            while isinstance(value, ast.Attribute):
                chain.append(value.attr)
                value = value.value
            if isinstance(value, ast.Name) and value.id in aliases:
                dotted = ".".join([aliases[value.id], *reversed(chain)])
                imported(dotted, node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            text = node.value
            if relative not in READERS and (text in KEY_WORDS or "decisions.match" in text):
                findings["reads"].append(f"{relative}:{node.lineno}: names {text!r}")
            if text.strip().lower() in DECIDER_NAMES:
                findings["names"].append(f"{relative}:{node.lineno}: names the decider {text!r}")
    return findings


def _scan_src() -> dict[str, list[str]]:
    total: dict[str, list[str]] = {"reads": [], "imports": [], "names": []}
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 100, "the src/ walk found too few modules; the guard would be vacuous"
    for path in files:
        relative = path.relative_to(SRC).as_posix()
        for rule, found in scan(path.read_text(encoding="utf-8"), relative).items():
            total[rule].extend(found)
    return total


def _bare_key_counts(reads: list[str]) -> Counter[str]:
    """How many bare ``match`` literals each module names, from the ``reads`` findings."""
    return Counter(
        finding.split(":", 1)[0] for finding in reads if finding.endswith(": " + BARE_KEY)
    )


def _unexempted(reads: list[str]) -> list[str]:
    """The findings left once each EXEMPT module's allowed bare ``match`` literals are removed.

    Only up to the allowed count is removed, so an extra literal in an exempt module stays.
    """
    allowance = {relative: count for relative, (count, _) in EXEMPT.items()}
    left = []
    for finding in reads:
        relative = finding.split(":", 1)[0]
        if finding.endswith(": " + BARE_KEY) and allowance.get(relative, 0) > 0:
            allowance[relative] -= 1
            continue
        left.append(finding)
    return left


def test_AC19_only_the_matcher_validator_cli_and_hash_read_decisions_match():
    findings = _scan_src()
    assert _unexempted(findings["reads"]) == [], _unexempted(findings["reads"])
    assert findings["imports"] == [], findings["imports"]
    # Not vacuous: the readers do read it, and the scan sees them.
    for relative in READERS:
        source = (SRC / relative).read_text(encoding="utf-8")
        assert "match" in source
    assert any(
        scan((SRC / relative).read_text(encoding="utf-8"), "elsewhere/x.py")["reads"]
        for relative in READERS
    )


def test_AC19_each_exemption_is_exact_and_covers_only_the_bare_key():
    counts = _bare_key_counts(_scan_src()["reads"])
    for relative, (count, reason) in EXEMPT.items():
        assert (SRC / relative).is_file(), relative
        assert reason.strip(), relative
        assert relative not in READERS, relative
        # Exact: one more literal is a new reader, one fewer is a stale entry.
        assert counts[relative] == count, (relative, counts[relative], count)
    # The exemption removes only the allowed bare keys from an exempt module.
    relative = "cli_agent_orchestrator/utils/tool_mapping.py"
    extra = scan(
        "A = {'match': 1}\nB = 'match'\nC = 'match'\nD = 'match_catalog'\n"
        "E = 'decisions.match'\ndef f(s):\n    return s.match_catalog\n",
        relative,
    )["reads"]
    assert len(extra) == 6, extra
    # Three bare keys against an allowance of two leave one; the other words are never exempt.
    left = sorted(finding.split(": ", 1)[1] for finding in _unexempted(extra))
    assert left == sorted(
        [BARE_KEY, "names 'match_catalog'", "names 'decisions.match'", "reads .match_catalog"]
    ), left
    # And never from a module that isn't listed.
    assert _unexempted(scan("A = 'match'\n", OUTSIDE)["reads"]) == [f"{OUTSIDE}:1: names 'match'"]


def test_AC20_no_src_module_names_jev_or_jevk5():
    assert _scan_src()["names"] == []


OUTSIDE = "cli_agent_orchestrator/services/elsewhere.py"


def test_AC19_guard_catches_aliased_and_relative_reads():
    cases = {
        "key": "def f(raw):\n    return raw['decisions']['match']\n",
        "get": "def f(raw):\n    return raw.get('decisions', {}).get('match')\n",
        "dotted": "PATH = 'decisions.match.models'\n",
        "getattr": "def f(s):\n    return getattr(s, 'match_catalog')\n",
        "attribute": "def f(s):\n    return s.match_catalog.models\n",
        "import_as": "import cli_agent_orchestrator.decisions.catalog as c\n",
        "from_as": "from cli_agent_orchestrator.decisions.catalog import load_catalog as lc\n",
        "from_module_as": "from cli_agent_orchestrator.decisions import catalog as cat\n",
        "matcher_as": "from cli_agent_orchestrator.decisions import match as m\n",
        "relative": "from ..decisions import catalog\n",
        "relative_name": "from ..decisions.catalog import load_catalog\n",
        "package_alias": (
            "from cli_agent_orchestrator import decisions as d\n"
            "def f(raw):\n    return d.catalog.load_catalog(raw)\n"
        ),
        "plain_chain": (
            "import cli_agent_orchestrator\n"
            "def f(raw):\n    return cli_agent_orchestrator.decisions.match.match_launch\n"
        ),
        "import_module": (
            "import importlib\n"
            "m = importlib.import_module('cli_agent_orchestrator.decisions.catalog')\n"
        ),
        "relative_import_module": (
            "import importlib\nm = importlib.import_module('..decisions.match', __package__)\n"
        ),
        "dunder_import": (
            "m = __import__('cli_agent_orchestrator.decisions', None, None, ['catalog'])\n"
        ),
    }
    for name, source in cases.items():
        found = scan(source, OUTSIDE)
        assert found["reads"] or found["imports"], name


def test_AC19_guard_allows_docstrings_unrelated_words_and_the_allowed_importers():
    clean = (
        '"""Mentions decisions.match and match in prose only."""\n'
        "def f(text):\n"
        '    """And here: match."""\n'
        "    return text.startswith('matched') or 'match_rule' in text\n"
    )
    assert scan(clean, OUTSIDE) == {"reads": [], "imports": [], "names": []}
    engine = "from cli_agent_orchestrator.decisions.match import MATCH_DECIDER\n"
    assert scan(engine, "cli_agent_orchestrator/decisions/engine.py")["imports"] == []
    assert scan(engine, OUTSIDE)["imports"]
    settings = "from cli_agent_orchestrator.decisions.catalog import load_catalog\n"
    assert scan(settings, "cli_agent_orchestrator/decisions/settings.py")["imports"] == []
    # The engine may call the matcher, never the catalog.
    assert scan(settings, "cli_agent_orchestrator/decisions/engine.py")["imports"]


def test_AC20_guard_catches_decider_names_in_any_position():
    for source in (
        "def f(d):\n    return d.name == 'jev'\n",
        "def f(d):\n    return d.name in ('jevk5',)\n",
        "def f(d):\n    return d.name.startswith('JEV')\n",
        "NAME = 'jevk5'\n",
        "def f(d):\n    return f'{d}' == 'jev'\n",
    ):
        assert scan(source, OUTSIDE)["names"], source
    assert scan('"""Works with jev and jevk5."""\n', OUTSIDE)["names"] == []
    assert scan("x = 'jevons'\n", OUTSIDE)["names"] == []
