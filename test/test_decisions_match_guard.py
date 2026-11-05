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

``"match_catalog"``, ``"decisions.match"`` and the ``.match_catalog`` field fail in any module
outside the readers. The bare key ``"match"`` is a common word (Kiro v3's permission rules use
it, for one), so it fails only where it can be the settings key: anywhere in the decisions
package; anywhere in a module that names the ``decisions`` settings key (``"decisions"`` or
``"_decisions"``); or as the key read from an expression that names ``decisions``
(``x["match"]``, ``x.get("match")``, ``"match" in x``). So an unrelated ``"match"`` elsewhere
never trips the guard, and no module has to be listed.

AC20. No module under ``src/`` names a decider ``jev`` or ``jevk5``: core never branches on
which decider answered (D9). The design scopes this to ``src/``; ``test/`` legitimately builds
fakes under those names to prove that neutrality.
"""

from __future__ import annotations

import ast
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
BARE = "match"
BARE_KEY = "names 'match'"
# Where a bare "match" can only be the settings key: the decisions package.
DECISIONS_PACKAGE = "cli_agent_orchestrator/decisions/"
# The settings key of the decisions block, and settings_service's copy of it.
DECISIONS_KEYS = ("decisions", "_decisions")
# The modules whose bare "match" is something else today, so the structural rule must pass
# them; the guard's own test checks it still sees their literals.
UNRELATED_MATCH = (
    "cli_agent_orchestrator/providers/kiro_v3_schema.py",
    "cli_agent_orchestrator/services/kiro_v3_validate.py",
    "cli_agent_orchestrator/utils/tool_mapping.py",
)
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


def _literal(node: object) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _names_decisions(node: ast.AST) -> bool:
    """Whether an expression names the decisions block: a name, attribute or string key."""
    for part in ast.walk(node):
        if isinstance(part, ast.Name) and part.id in DECISIONS_KEYS:
            return True
        if isinstance(part, ast.Attribute) and part.attr in DECISIONS_KEYS:
            return True
        if _literal(part) in DECISIONS_KEYS:
            return True
    return False


def _keyed_reads(tree: ast.AST) -> set[int]:
    """The bare "match" literals read as a key from an expression that names decisions."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _literal(node.slice) == BARE:
            if _names_decisions(node.value):
                found.add(id(node.slice))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            first = node.args[0] if node.args else None
            if (
                node.func.attr in ("get", "pop", "setdefault")
                and _literal(first) == BARE
                and _names_decisions(node.func.value)
            ):
                found.add(id(first))
        elif isinstance(node, ast.Compare) and _literal(node.left) == BARE:
            if isinstance(node.ops[0], (ast.In, ast.NotIn)) and _names_decisions(
                node.comparators[0]
            ):
                found.add(id(node.left))
    return found


def scan(source: str, relative: str) -> dict[str, list[str]]:
    """Every AC19 and AC20 finding in one module, keyed by rule."""
    tree = ast.parse(source)
    module = _module_name(relative)
    package = module if relative.endswith("__init__.py") else module.rpartition(".")[0]
    docstrings = _docstrings(tree)
    findings: dict[str, list[str]] = {"reads": [], "imports": [], "names": []}
    # Where a bare "match" can be the settings key (see the module docstring).
    bare_anywhere = relative.startswith(DECISIONS_PACKAGE) or any(
        _literal(node) in DECISIONS_KEYS for node in ast.walk(tree) if id(node) not in docstrings
    )
    keyed = _keyed_reads(tree)
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
            if relative not in READERS and (
                text == "match_catalog"
                or "decisions.match" in text
                or (text == BARE and (bare_anywhere or id(node) in keyed))
            ):
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


def test_AC19_only_the_matcher_validator_cli_and_hash_read_decisions_match():
    findings = _scan_src()
    # A bare "match" fails only where it can be the settings key: rename it, or read the
    # catalog through decisions.catalog.
    assert findings["reads"] == [], findings["reads"]
    assert findings["imports"] == [], findings["imports"]
    # Not vacuous: the readers do read it, and the scan sees them.
    for relative in READERS:
        source = (SRC / relative).read_text(encoding="utf-8")
        assert "match" in source
    assert any(
        scan((SRC / relative).read_text(encoding="utf-8"), "elsewhere/x.py")["reads"]
        for relative in READERS
    )


def test_AC19_a_bare_match_fails_only_where_it_can_be_the_settings_key():
    unrelated = (
        "A = {'match': 1}\nB = ('match', 'exclude')\ndef f(rule):\n    return rule['match']\n"
    )
    # An unrelated "match" outside the decisions package passes, with no list to keep.
    assert scan(unrelated, OUTSIDE)["reads"] == []
    # The same source fails in the decisions package, and in a module naming the block's key.
    assert len(scan(unrelated, "cli_agent_orchestrator/decisions/x.py")["reads"]) == 3
    for key in ("'decisions'", "'_decisions'"):
        named = unrelated + f"K = {key}\n"
        assert len(scan(named, OUTSIDE)["reads"]) == 3, key
    # A key read from an expression naming decisions fails, whatever else the module says.
    for source in (
        "def f(decisions):\n    return decisions['match']\n",
        "def f(s):\n    return s.decisions.get('match')\n",
        "def f(s):\n    return s._decisions.pop('match', None)\n",
        "def f(s):\n    return 'match' not in s._decisions\n",
        "def f(load):\n    return load()['decisions'].setdefault('match', {})\n",
    ):
        found = scan(source, OUTSIDE)["reads"]
        assert found == [f"{OUTSIDE}:2: {BARE_KEY}"], (source, found)
    # The same reads from anything else pass.
    for source in (
        "def f(rule):\n    return rule.pop('match', None)\n",
        "def f(rule, decisions):\n    return 'match' in rule or decisions\n",
    ):
        assert scan(source, OUTSIDE)["reads"] == [], source
    # The other words fail anywhere outside the readers, even beside an unrelated "match".
    others = scan(
        unrelated + "D = 'match_catalog'\nE = 'decisions.match'\ndef g(s):\n"
        "    return s.match_catalog\n",
        OUTSIDE,
    )["reads"]
    assert sorted(finding.split(": ", 1)[1] for finding in others) == sorted(
        ["names 'match_catalog'", "names 'decisions.match'", "reads .match_catalog"]
    ), others
    # Docstrings never count, for either word.
    assert scan('"""Reads decisions["match"]."""\nK = "decisions"\n', OUTSIDE)["reads"] == []
    assert scan('"""decisions"""\n' + unrelated, OUTSIDE)["reads"] == []


def test_AC19_the_guard_sees_the_unrelated_match_literals_it_passes():
    # Not vacuous: each module whose bare "match" is something else has such literals, and
    # the scan reports them as soon as the context could make them the settings key.
    for relative in UNRELATED_MATCH:
        source = (SRC / relative).read_text(encoding="utf-8")
        assert relative not in READERS, relative
        assert scan(source, relative)["reads"] == [], relative
        assert scan(source, DECISIONS_PACKAGE + "moved.py")["reads"], relative
        assert scan(source + "K = 'decisions'\n", relative)["reads"], relative


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
