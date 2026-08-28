"""Tests for the per-platform wheel matrix — unit `wheel-matrix`, issue #321.

These lock in the fix for a defect verified in a REAL artifact: a wheel tagged
``py3-none-any`` that carried a ``Mach-O 64-bit arm64`` executable. ``py3-none-any`` means
"pure Python, runs anywhere", so pip installed it on Linux and Windows where the binary
cannot execute, and the operator only found out at ``cao tui`` time.

Every test here either (a) exercises the tag-selection logic that produces the platform tag,
or (b) asserts a guard actually FAILS on the bad input it exists to catch. The second kind
matters more: a guard that cannot fail is worse than no guard, because it reports success.

What these tests deliberately do NOT claim: that the Linux wheel works. It cannot be built
or executed on the development machine (macOS arm64), and no test here pretends otherwise —
see `test_wheel_matrix_documents_unverified_platforms`. Windows and Intel macOS are not built
at all; their tags still appear below because the tag-selection logic and the assertion gate
must behave correctly on any input hatchling hands them, not because either is shippable.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml"


def _publish_workflow() -> Dict[str, Any]:
    """The publish workflow, PARSED rather than grepped.

    Substring assertions over the raw YAML are what let two of the v2.5.0 release defects
    through: a missing ``setup-python`` step and an unset ``MACOSX_DEPLOYMENT_TARGET`` are both
    ABSENCES, and ``assert "x" in text`` cannot see an absence nobody thought to name. Parsing
    lets a test assert structure instead — which step precedes which, and what value one
    specific matrix leg carries.

    Module-level so every class that needs the workflow shares one reader; there were two
    copies of this before.
    """
    import yaml

    return yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))


def _wheel_matrix_legs() -> list:
    """The `build-wheels` matrix legs, as dicts."""
    return _publish_workflow()["jobs"]["build-wheels"]["strategy"]["matrix"]["include"]


def _build_wheels_steps() -> list:
    return _publish_workflow()["jobs"]["build-wheels"]["steps"]


def _load_script(name: str) -> Any:
    """Import a module from scripts/ by path.

    scripts/ is not a package (no __init__.py) and is not on sys.path, so a plain import
    would fail. Loading by path keeps the scripts runnable as scripts — which is how CI
    invokes them — rather than forcing them into a package layout just to be testable.
    """
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_cao_script_{name}", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build_tui = _load_script("build_tui")
assert_wheel_matrix = _load_script("assert_wheel_matrix")
smoke_test_wheel = _load_script("smoke_test_wheel")


# The build-hook module imports `hatchling` at module scope, and `hatchling` is a BUILD
# dependency (`[build-system] requires`) — absent from a `uv sync --dev` environment.
#
# So the tag DECISION lives in a module-level `resolve_build_data()` that needs no hatchling,
# and is loaded here by exec'ing the file's source up to that import. The alternative — skip
# these tests when hatchling is missing — would mean the tests for THE ACTUAL FIX never ran
# in CI's test job, and a guard that does not run is not a guard.
#
# The hatchling adapter class itself is 4 lines of delegation and is exercised end-to-end by
# `uv build` in CI, which is where a broken adapter would surface immediately. (#321)
def _load_hook_logic() -> Any:
    """Load hatch_build_tui_tag's pure logic without importing hatchling.

    Executes the module source truncated at the deliberately-late hatchling import. The
    truncation point is ASSERTED, not assumed: if that import ever moves above the pure
    functions, this raises instead of silently testing a partial module.
    """
    source = (SCRIPTS_DIR / "hatch_build_tui_tag.py").read_text(encoding="utf-8")
    marker = "from hatchling.builders.hooks.plugin.interface import BuildHookInterface"
    assert marker in source, "the hatchling import moved; update this loader deliberately"
    head = source.split(marker, 1)[0]
    for required in ("def resolve_build_data", "def staged_binaries", "def _env_flag"):
        assert required in head, (
            f"{required} is no longer defined BEFORE the hatchling import — the tag decision "
            "must stay importable without the build backend so its tests always run"
        )
    module: Dict[str, Any] = {"__name__": "_cao_hook_logic", "__file__": str(SCRIPTS_DIR)}
    exec(compile(head, str(SCRIPTS_DIR / "hatch_build_tui_tag.py"), "exec"), module)
    return SimpleNamespace(**module)


hatch_hook = _load_hook_logic()


def _make_wheel(path: Path, members: dict[str, bytes]) -> Path:
    """Write a minimal .whl (a zip) containing the given members."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _wheel_with_tag(tmp_path: Path, tag: str, binary_size: int = 1024) -> Path:
    """A wheel whose WHEEL metadata declares ``tag`` and which bundles a fake TUI binary."""
    filename = f"cli_agent_orchestrator-2.3.0-{tag}.whl"
    return _make_wheel(
        tmp_path / filename,
        {
            "cli_agent_orchestrator-2.3.0.dist-info/WHEEL": (
                f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: false\nTag: {tag}\n"
            ).encode(),
            "cli_agent_orchestrator/cao-tui": b"\x00" * binary_size,
        },
    )


# ---------------------------------------------------------------------------------------
# THE FIX: the tag the build hook selects
# ---------------------------------------------------------------------------------------


def _stage_binary(tmp_path: Path, name: str = "cao-tui") -> Path:
    """Create the package dir with a staged fake TUI binary, as build_tui.py would."""
    pkg = tmp_path / "src" / "cli_agent_orchestrator"
    pkg.mkdir(parents=True, exist_ok=True)
    binary = pkg / name
    binary.write_bytes(b"\x7fELF fake binary")
    return binary


def _resolve(tmp_path: Path, best_tag: str, target_name: str = "wheel") -> Dict[str, Any]:
    """Run the real decision function and return the resulting build_data.

    ``best_tag`` is the one input a test substitutes — it stands in for hatchling's
    ``get_best_matching_tag()``, the platform-tag SOURCE. Everything else is production code,
    so these tests cannot pass by re-expressing the logic they are checking.
    """
    build_data: Dict[str, Any] = {"infer_tag": False, "pure_python": True}
    hatch_hook.resolve_build_data(str(tmp_path), target_name, best_tag, build_data)
    return build_data


class TestPlatformTagSelection:
    """The hook must produce ``py3-none-<platform>`` — not ``any``, and not ``cpXX-cpXX``."""

    def test_tags_wheel_for_the_platform_when_binary_is_staged(self, tmp_path, monkeypatch):
        """The defect's direct inverse: a staged binary yields a platform-specific tag."""
        _stage_binary(tmp_path)
        monkeypatch.setenv(hatch_hook.FORCE_ENV, "1")

        build_data = _resolve(tmp_path, "cp312-cp312-macosx_26_0_arm64")

        assert build_data["tag"] == "py3-none-macosx_26_0_arm64"
        assert build_data["pure_python"] is False, "Root-Is-Purelib must become false"

    def test_windows_exe_is_detected_by_the_same_path(self, tmp_path, monkeypatch):
        """The artifacts glob ends in `*` to cover `.exe`; the hook must agree."""
        _stage_binary(tmp_path, name="cao-tui.exe")
        monkeypatch.setenv(hatch_hook.FORCE_ENV, "1")

        build_data = _resolve(tmp_path, "cp310-cp310-win_amd64")
        assert build_data["tag"] == "py3-none-win_amd64"

    @pytest.mark.parametrize(
        "best_tag,expected_platform",
        [
            ("cp312-cp312-macosx_26_0_arm64", "macosx_26_0_arm64"),
            ("cp310-cp310-macosx_26_0_x86_64", "macosx_26_0_x86_64"),
            ("cp310-cp310-manylinux_2_28_x86_64", "manylinux_2_28_x86_64"),
            ("cp310-cp310-linux_x86_64", "linux_x86_64"),
            ("cp310-cp310-win_amd64", "win_amd64"),
        ],
    )
    def test_platform_component_is_preserved_for_any_platform_hatchling_infers(
        self, tmp_path, monkeypatch, best_tag, expected_platform
    ):
        """The platform half comes from hatchling and must pass through untouched.

        Deliberately broader than the shipped matrix: two of these platforms are not built,
        but the hook must not special-case a platform list it cannot see, and a tag it
        mangled would be worse than one it refused.
        """
        _stage_binary(tmp_path)
        monkeypatch.setenv(hatch_hook.FORCE_ENV, "1")

        build_data = _resolve(tmp_path, best_tag)
        assert build_data["tag"] == f"py3-none-{expected_platform}"

    def test_tag_is_abi_independent_so_the_wheel_installs_on_every_supported_python(
        self, tmp_path, monkeypatch
    ):
        """``cpXX-cpXX`` would pin the wheel to ONE CPython ABI.

        This is the trap that ``infer_tag = True`` alone walks into: hatchling's
        ``get_best_matching_tag()`` returns the RUNNING interpreter's full tag. Under
        cibuildwheel the build runs on cp310, so the wheel would be tagged
        ``cp310-cp310-<plat>`` and pip would REFUSE it on 3.11-3.14 — while this project
        declares ``requires-python = ">=3.10"`` and ships no CPython extension module. That
        would be a wider outage than the defect it replaced.
        """
        _stage_binary(tmp_path)
        monkeypatch.setenv(hatch_hook.FORCE_ENV, "1")

        build_data = _resolve(tmp_path, "cp310-cp310-manylinux_2_28_x86_64")

        interpreter, abi, _ = build_data["tag"].split("-", 2)
        assert (interpreter, abi) == ("py3", "none"), (
            f"tag {build_data['tag']!r} pins an ABI; a wheel with no C extension must stay "
            "py3-none so it installs on every supported Python"
        )

    def test_is_inert_when_no_binary_is_staged(self, tmp_path, monkeypatch):
        """A contributor with no Rust toolchain must still get an installable pure wheel."""
        (tmp_path / "src" / "cli_agent_orchestrator").mkdir(parents=True)
        monkeypatch.delenv(hatch_hook.FORCE_ENV, raising=False)
        monkeypatch.delenv(hatch_hook.CIBUILDWHEEL_ENV, raising=False)

        build_data = _resolve(tmp_path, "cp312-cp312-macosx_26_0_arm64")

        assert "tag" not in build_data, "no binary staged: hatchling's default tag must stand"
        assert build_data["pure_python"] is True

    def test_refuses_a_pure_tag_when_a_native_binary_is_staged(self, tmp_path, monkeypatch):
        """The contradiction must be an error, not a silent choice — it IS the defect."""
        _stage_binary(tmp_path)
        monkeypatch.setenv(hatch_hook.FORCE_ENV, "0")

        with pytest.raises(ValueError, match="native TUI binary is staged"):
            _resolve(tmp_path, "cp312-cp312-macosx_26_0_arm64")

    def test_refuses_a_platform_wheel_with_no_binary_in_it(self, tmp_path, monkeypatch):
        """Under cibuildwheel a missing binary means before-build did not stage."""
        (tmp_path / "src" / "cli_agent_orchestrator").mkdir(parents=True)
        monkeypatch.setenv(hatch_hook.CIBUILDWHEEL_ENV, "1")
        monkeypatch.delenv(hatch_hook.FORCE_ENV, raising=False)

        with pytest.raises(ValueError, match=r"no cao-tui\* binary is staged"):
            _resolve(tmp_path, "cp312-cp312-macosx_26_0_arm64")

    def test_refuses_when_hatchling_resolves_an_any_platform(self, tmp_path, monkeypatch):
        """Never fall through to 'any' — that is the exact defect being fixed."""
        _stage_binary(tmp_path)
        monkeypatch.setenv(hatch_hook.FORCE_ENV, "1")

        with pytest.raises(ValueError, match="platform-independent wheel"):
            _resolve(tmp_path, "py3-none-any")

    def test_sdist_target_is_untouched(self, tmp_path, monkeypatch):
        """An sdist ships source; tagging it for a platform would be meaningless."""
        _stage_binary(tmp_path)
        monkeypatch.setenv(hatch_hook.FORCE_ENV, "1")

        build_data: Dict[str, Any] = {}
        hatch_hook.resolve_build_data(str(tmp_path), "sdist", "", build_data)
        assert build_data == {}

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_force_values_request_a_platform_wheel(self, tmp_path, monkeypatch, value):
        _stage_binary(tmp_path)
        monkeypatch.setenv(hatch_hook.FORCE_ENV, value)
        assert _resolve(tmp_path, "cp312-cp312-macosx_26_0_arm64")["pure_python"] is False

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_force_value_is_treated_as_unset_not_as_false(self, tmp_path, monkeypatch, value):
        """Blank must mean "decide from the tree", not "the caller demanded a pure wheel".

        Reading blank as an explicit False would make a staged binary raise the contradiction
        error on any CI runner that defines the variable as empty — a self-inflicted outage.
        """
        _stage_binary(tmp_path)
        monkeypatch.setenv(hatch_hook.FORCE_ENV, value)
        monkeypatch.delenv(hatch_hook.CIBUILDWHEEL_ENV, raising=False)

        build_data = _resolve(tmp_path, "cp312-cp312-macosx_26_0_arm64")
        assert build_data["tag"] == "py3-none-macosx_26_0_arm64"


# ---------------------------------------------------------------------------------------
# Build-time auto-staging: what makes `cao tui` work from a SOURCE install (#560)
#
# The defect these lock in was measured on a clean clone of main: `uv build --wheel` produced
# `py3-none-any` with no `cao-tui` member, because the binary was only ever staged by
# cibuildwheel's `before-build` at RELEASE time. An operator's `uv tool install <git-url>`
# therefore installed a `cao tui` that could not run.
#
# The graceful-degradation tests matter as much as the happy path: a missing Rust toolchain
# must never fail an install, or this fix trades one broken install for a worse one.
# ---------------------------------------------------------------------------------------


class TestAutobuildStagesTheBinary:
    @pytest.fixture(autouse=True)
    def _hermetic(self, monkeypatch):
        """Isolate the hook from the host toolchain and the ambient environment.

        Both halves are load-bearing, and both were real defects:

        * ``cargo`` is FAKED PRESENT by default. Without this, every happy-path case here
          silently inverted on a machine with no Rust toolchain — ``autobuild_binary`` returned
          None at its ``shutil.which`` guard and the test failed for a reason that has nothing
          to do with the hook. Measured: two failures in a cargo-less environment. What these
          tests own is the hook's contract with ``build_tui.py`` (is it invoked, is its exit
          code honoured, is its output adopted or discarded) — never whether the host can
          compile Rust. A case that needs the ABSENT-cargo path overrides this explicitly.
        * All THREE controlling env vars are cleared. ``CAO_TUI_AUTOBUILD`` gates the build,
          and ``CAO_TUI_PLATFORM_WHEEL`` / ``CIBUILDWHEEL`` change ``resolve_build_data``'s
          decision — a runner exporting any of them (cibuildwheel exports ``CIBUILDWHEEL=1``
          for every build it drives) would rewrite these tests' meaning. A test whose verdict
          depends on the shell that launched pytest is not a guard.

        The stand-in ``build_tui.py`` is a plain Python script, so a faked ``which`` never
        causes a real cargo invocation.
        """
        monkeypatch.setattr(hatch_hook.shutil, "which", lambda name: f"/usr/bin/{name}")
        for var in (hatch_hook.AUTOBUILD_ENV, hatch_hook.FORCE_ENV, hatch_hook.CIBUILDWHEEL_ENV):
            monkeypatch.delenv(var, raising=False)

    @staticmethod
    def _fake_build_script(root: Path, body: str) -> Path:
        """Write a stand-in scripts/build_tui.py at ``root``.

        A stand-in, not the real script: invoking real cargo here would make this test need a
        Rust toolchain and a minute of wall-clock, so what is under test is the HOOK's
        contract with the script — is it invoked, is its exit code honoured, is its staged
        output picked up — rather than cargo itself. The real script's own build is covered by
        `test_cargo_build_passes_locked` and proven end-to-end by CI's `uv build`.
        """
        scripts = root / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        script = scripts / "build_tui.py"
        script.write_text(body, encoding="utf-8")
        return script

    def test_autobuild_runs_the_build_script_and_returns_the_staged_binary(self, tmp_path):
        """The happy path: no binary staged, cargo present, so the hook builds one."""
        # A script that stages the binary exactly where the artifacts glob looks.
        self._fake_build_script(
            tmp_path,
            "import pathlib, sys\n"
            "pkg = pathlib.Path(sys.argv[0]).resolve().parents[1] / 'src' / "
            "'cli_agent_orchestrator'\n"
            "pkg.mkdir(parents=True, exist_ok=True)\n"
            "(pkg / 'cao-tui').write_bytes(b'\\x7fELF staged by the hook')\n",
        )

        staged = hatch_hook.autobuild_binary(str(tmp_path))

        assert staged is not None, "the hook did not stage a binary the build script produced"
        assert staged.name == "cao-tui"
        assert staged.read_bytes() == b"\x7fELF staged by the hook"

    def test_resolve_build_data_autobuilds_and_then_tags_for_the_platform(self, tmp_path):
        """THE FIX, end to end through the real decision function.

        This is the test that would have caught the reported defect: with no binary staged,
        `resolve_build_data` must now produce a binary AND a platform tag. Before #560 it
        returned early and left `tag` unset, which is how a `py3-none-any` wheel carrying no
        TUI reached an operator.
        """
        self._fake_build_script(
            tmp_path,
            "import pathlib, sys\n"
            "pkg = pathlib.Path(sys.argv[0]).resolve().parents[1] / 'src' / "
            "'cli_agent_orchestrator'\n"
            "pkg.mkdir(parents=True, exist_ok=True)\n"
            "(pkg / 'cao-tui').write_bytes(b'\\x7fELF')\n",
        )

        build_data = _resolve(tmp_path, "cp312-cp312-macosx_26_0_arm64")

        assert build_data["tag"] == "py3-none-macosx_26_0_arm64", (
            "a wheel that auto-built a native binary must be tagged for its platform, not "
            "left as py3-none-any"
        )
        assert build_data["pure_python"] is False
        assert (tmp_path / "src" / "cli_agent_orchestrator" / "cao-tui").is_file()

    def test_install_still_succeeds_when_cargo_is_absent(self, tmp_path, monkeypatch):
        """The non-negotiable one: no Rust toolchain must NOT fail the build.

        `cao` is a Python tool whose TUI is one subcommand. Raising here would break every
        contributor and CI job that builds without Rust — a far wider outage than the missing
        TUI. `cao tui` already prints an actionable message when the binary is absent.
        """
        self._fake_build_script(tmp_path, "raise SystemExit('cargo should never be reached')\n")
        monkeypatch.setattr(hatch_hook.shutil, "which", lambda _name: None)

        assert hatch_hook.autobuild_binary(str(tmp_path)) is None

        # And the wheel is a normal pure wheel: no tag forced, no exception.
        build_data = _resolve(tmp_path, "cp312-cp312-macosx_26_0_arm64")
        assert "tag" not in build_data
        assert build_data["pure_python"] is True

    def test_a_failed_build_is_not_staged_even_if_it_left_a_binary_behind(self, tmp_path):
        """A NON-ZERO exit must be honoured even when a binary is sitting in the staging dir.

        The stand-in stages a binary and THEN fails, because the real ``build_tui.py build``
        does exactly that: it copies the binary into place first and only afterwards asserts
        the artifacts glob and NFR-2's 10 MB size ceiling. So "exit non-zero with a staged
        file present" is a reachable state, not a hypothetical — an over-ceiling or otherwise
        rejected binary.

        Written this way deliberately: the obvious version of this test (a script that merely
        exits 1) passes even if the exit-code check is deleted entirely, because there is no
        binary to find either way. Mutation-verified — replacing the ``returncode`` check with
        ``if False`` leaves that version GREEN and this one RED.

        Returning None is necessary but NOT sufficient, which is the second assertion: the
        wheel's ``artifacts`` glob collects that directory during hatchling's own file walk and
        never consults this hook, so a leftover left on disk would be packaged into the wheel
        no matter what this function returned. The rejected file must be GONE.
        """
        self._fake_build_script(
            tmp_path,
            "import pathlib, sys\n"
            "pkg = pathlib.Path(sys.argv[0]).resolve().parents[1] / 'src' / "
            "'cli_agent_orchestrator'\n"
            "pkg.mkdir(parents=True, exist_ok=True)\n"
            "(pkg / 'cao-tui').write_bytes(b'REJECTED BY THE BUILD SCRIPT')\n"
            "sys.exit(101)\n",
        )

        assert (
            hatch_hook.autobuild_binary(str(tmp_path)) is None
        ), "a build that exited non-zero must not have its leftover binary adopted"
        assert not (tmp_path / "src" / "cli_agent_orchestrator" / "cao-tui").exists(), (
            "the rejected binary is still on disk — hatchling's artifacts glob would package "
            "it into the wheel regardless of what this hook returned"
        )

    def test_a_rejected_leftover_does_not_tag_the_wheel_for_a_platform(self, tmp_path):
        """The same defect one level up, where it actually reaches the wheel.

        ``resolve_build_data`` RESCANS the staging directory after autobuilding, so before the
        discard existed a build that staged ``cao-tui`` and then exited 101 produced
        ``py3-none-<platform>`` with ``pure_python`` false — a platform wheel built around a
        payload the build had refused. Asserting the pure-wheel outcome (not just that the file
        is gone) is what ties the cleanup to the tag decision it exists to protect.
        """
        self._fake_build_script(
            tmp_path,
            "import pathlib, sys\n"
            "pkg = pathlib.Path(sys.argv[0]).resolve().parents[1] / 'src' / "
            "'cli_agent_orchestrator'\n"
            "pkg.mkdir(parents=True, exist_ok=True)\n"
            "(pkg / 'cao-tui').write_bytes(b'OVER THE SIZE CEILING')\n"
            "sys.exit(101)\n",
        )

        build_data = _resolve(tmp_path, "cp312-cp312-macosx_26_0_arm64")

        assert "tag" not in build_data, "a rejected build must leave hatchling's default tag"
        assert build_data["pure_python"] is True
        assert not (tmp_path / "src" / "cli_agent_orchestrator" / "cao-tui").exists()

    def test_a_preexisting_binary_survives_a_failed_build(self, tmp_path):
        """The discard must unwind only what THIS invocation staged.

        The guard rails the cleanup: cibuildwheel stages via ``before-build``, and a developer
        may stage explicitly. A blanket "delete every binary on any failure" would destroy that
        input — and under cibuildwheel it would convert a release build into
        ``resolve_build_data``'s hard "no binary is staged" error. So the pre-existing file is
        passed through untouched even though the build it triggered failed.
        """
        _stage_binary(tmp_path)
        original = (tmp_path / "src" / "cli_agent_orchestrator" / "cao-tui").read_bytes()
        self._fake_build_script(
            tmp_path,
            "import pathlib, sys\n"
            "pkg = pathlib.Path(sys.argv[0]).resolve().parents[1] / 'src' / "
            "'cli_agent_orchestrator'\n"
            "(pkg / 'cao-tui.exe').write_bytes(b'REJECTED')\n"
            "sys.exit(101)\n",
        )

        # Called directly: resolve_build_data would skip the build entirely (a binary is
        # already staged), which is a different guard — tested separately above.
        assert hatch_hook.autobuild_binary(str(tmp_path)) is None

        staged_dir = tmp_path / "src" / "cli_agent_orchestrator"
        assert (
            staged_dir.joinpath("cao-tui").read_bytes() == original
        ), "the cleanup deleted a binary it did not stage"
        assert not staged_dir.joinpath("cao-tui.exe").exists(), "the rejected leftover survived"

    def test_a_hung_build_is_bounded_and_degrades(self, tmp_path, monkeypatch):
        """A wedged cargo must not hang `pip install` forever.

        ``subprocess.run`` without a timeout waits indefinitely, so a stalled registry fetch or
        a wedged linker would hang the install with no output and no way out but Ctrl-C. The
        timeout is raised here rather than waited on: what is under test is that the hook PASSES
        a timeout and treats expiry as one more graceful-degradation path — including
        discarding anything the killed build had already staged.
        """
        self._fake_build_script(tmp_path, "pass\n")
        pkg = tmp_path / "src" / "cli_agent_orchestrator"
        pkg.mkdir(parents=True, exist_ok=True)

        def _hang(*args, **kwargs):
            assert kwargs.get("timeout") == hatch_hook.BUILD_TIMEOUT_SECONDS, (
                "the hook invoked the build script with no timeout — a hung cargo would hang "
                "the install indefinitely"
            )
            # A build killed mid-flight can leave a partial copy behind.
            (pkg / "cao-tui").write_bytes(b"PARTIAL")
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

        monkeypatch.setattr(hatch_hook.subprocess, "run", _hang)

        assert hatch_hook.autobuild_binary(str(tmp_path)) is None
        assert not (pkg / "cao-tui").exists(), "a timed-out build's partial output was kept"

    def test_install_still_succeeds_when_the_cargo_build_fails(self, tmp_path):
        """A compile error in the crate must not make `pip install` impossible either.

        Distinct from the no-cargo case and tested separately: this path runs the script and
        honours a NON-ZERO exit, where the other never runs it at all.
        """
        self._fake_build_script(tmp_path, "import sys\nsys.exit(101)\n")

        assert hatch_hook.autobuild_binary(str(tmp_path)) is None

        build_data = _resolve(tmp_path, "cp312-cp312-macosx_26_0_arm64")
        assert "tag" not in build_data
        assert build_data["pure_python"] is True

    def test_a_script_that_reports_success_but_stages_nothing_is_not_trusted(self, tmp_path):
        """Exit 0 is not proof of a staged binary — the D1 failure mode, at this boundary."""
        self._fake_build_script(tmp_path, "pass\n")

        assert hatch_hook.autobuild_binary(str(tmp_path)) is None

    def test_autobuild_is_skipped_when_a_binary_is_already_staged(self, tmp_path):
        """cibuildwheel stages via `before-build`; the hook must not rebuild over it.

        The stand-in script would CORRUPT the staged binary if it ran, so the assertion on
        the bytes is what proves the skip — not merely that a tag came out right.
        """
        _stage_binary(tmp_path)
        original = (tmp_path / "src" / "cli_agent_orchestrator" / "cao-tui").read_bytes()
        self._fake_build_script(
            tmp_path,
            "import pathlib, sys\n"
            "pkg = pathlib.Path(sys.argv[0]).resolve().parents[1] / 'src' / "
            "'cli_agent_orchestrator'\n"
            "(pkg / 'cao-tui').write_bytes(b'REBUILT OVER THE STAGED BINARY')\n",
        )

        _resolve(tmp_path, "cp312-cp312-macosx_26_0_arm64")

        assert (
            tmp_path / "src" / "cli_agent_orchestrator" / "cao-tui"
        ).read_bytes() == original, "the hook rebuilt over an already-staged binary"

    def test_autobuild_env_opt_out_prevents_the_build(self, tmp_path, monkeypatch):
        """`CAO_TUI_AUTOBUILD=0` restores the pre-#560 explicit-staging-only behaviour."""
        self._fake_build_script(
            tmp_path,
            "import pathlib, sys\n"
            "pkg = pathlib.Path(sys.argv[0]).resolve().parents[1] / 'src' / "
            "'cli_agent_orchestrator'\n"
            "pkg.mkdir(parents=True, exist_ok=True)\n"
            "(pkg / 'cao-tui').write_bytes(b'\\x7fELF')\n",
        )
        monkeypatch.setenv(hatch_hook.AUTOBUILD_ENV, "0")

        assert hatch_hook.autobuild_binary(str(tmp_path)) is None
        assert not (tmp_path / "src" / "cli_agent_orchestrator" / "cao-tui").exists()

    def test_an_explicit_pure_wheel_request_does_not_trigger_a_cargo_build(
        self, tmp_path, monkeypatch
    ):
        """`CAO_TUI_PLATFORM_WHEEL=0` asks for a pure wheel, so building would be wasted work.

        Worse than wasted: the staged binary would then hit the contradiction guard and raise,
        turning an explicit, legitimate request into a failed build.
        """
        self._fake_build_script(
            tmp_path,
            "import pathlib, sys\n"
            "pkg = pathlib.Path(sys.argv[0]).resolve().parents[1] / 'src' / "
            "'cli_agent_orchestrator'\n"
            "pkg.mkdir(parents=True, exist_ok=True)\n"
            "(pkg / 'cao-tui').write_bytes(b'\\x7fELF')\n",
        )
        monkeypatch.setenv(hatch_hook.FORCE_ENV, "0")
        monkeypatch.delenv(hatch_hook.CIBUILDWHEEL_ENV, raising=False)

        build_data = _resolve(tmp_path, "cp312-cp312-macosx_26_0_arm64")

        assert build_data["pure_python"] is True
        assert not (tmp_path / "src" / "cli_agent_orchestrator" / "cao-tui").exists()

    def test_the_sdist_target_never_autobuilds(self, tmp_path):
        """An sdist carries source. Compiling a binary for it would be pure wasted time."""
        self._fake_build_script(
            tmp_path,
            "import pathlib, sys\n"
            "pkg = pathlib.Path(sys.argv[0]).resolve().parents[1] / 'src' / "
            "'cli_agent_orchestrator'\n"
            "pkg.mkdir(parents=True, exist_ok=True)\n"
            "(pkg / 'cao-tui').write_bytes(b'\\x7fELF')\n",
        )

        _resolve(tmp_path, "cp312-cp312-macosx_26_0_arm64", target_name="sdist")

        assert not (tmp_path / "src" / "cli_agent_orchestrator" / "cao-tui").exists()


# ---------------------------------------------------------------------------------------
# The publish gate: the full platform set, and no 'any' wheel
# ---------------------------------------------------------------------------------------


class TestAssertWheelMatrix:
    def test_full_platform_set_passes_without_windows_or_intel_macos_wheels(self, tmp_path):
        """The two platforms the matrix builds are the whole requirement.

        Doubles as the regression test for the v2.5.0 publish gate: while `Windows AMD64` and
        `macOS x86_64` were still in ``REQUIRED_PLATFORM_PATTERNS``, this exact directory —
        every wheel the matrix can actually produce — would have been rejected as incomplete,
        blocking the publish on artifacts nothing builds.
        """
        for tag in (
            "py3-none-macosx_26_0_arm64",
            "py3-none-linux_x86_64",
        ):
            _wheel_with_tag(tmp_path, tag)
        assert assert_wheel_matrix.main(["--dist", str(tmp_path)]) == 0

    def test_missing_one_platform_fails(self, tmp_path):
        """A partial matrix must not publish. `fail-fast: false` makes this easy to miss."""
        _wheel_with_tag(tmp_path, "py3-none-macosx_26_0_arm64")
        assert assert_wheel_matrix.main(["--dist", str(tmp_path)]) == 1

    def test_the_gate_still_fails_on_a_platform_it_is_asked_to_require(self, tmp_path):
        """The shrunken required set must not be mistaken for a gate that cannot fail.

        Two platforms were removed from ``REQUIRED_PLATFORM_PATTERNS`` for good reasons; this
        pins that the remaining mechanism is still load-bearing by asking it, via ``--expect``,
        for a platform the directory does not have.
        """
        _wheel_with_tag(tmp_path, "py3-none-macosx_26_0_arm64")
        assert (
            assert_wheel_matrix.main(["--dist", str(tmp_path), "--expect", "macosx_26_0_x86_64"])
            == 1
        )

    def test_an_any_wheel_in_the_set_fails(self, tmp_path):
        """An 'any' wheel OUTRANKS the platform wheels for any unmatched host."""
        for tag in (
            "py3-none-macosx_26_0_arm64",
            "py3-none-linux_x86_64",
            "py3-none-any",
        ):
            _wheel_with_tag(tmp_path, tag)
        assert assert_wheel_matrix.main(["--dist", str(tmp_path)]) == 1

    def test_empty_dist_fails_rather_than_passing_quietly(self, tmp_path):
        """A missing artifact is a FAILED check — the vacuous-guard failure mode."""
        assert assert_wheel_matrix.main(["--dist", str(tmp_path)]) == 1

    def test_missing_directory_fails(self, tmp_path):
        assert assert_wheel_matrix.main(["--dist", str(tmp_path / "nope")]) == 1

    def test_manylinux_retagged_wheel_satisfies_the_linux_requirement(self, tmp_path):
        """``manylinux_2_28_x86_64`` is the tag that ACTUALLY ships, and must be accepted.

        Measured, not assumed: running `auditwheel repair` inside
        ``quay.io/pypa/manylinux_2_28_x86_64`` against a wheel carrying a real ELF binary
        rewrote ``py3-none-linux_x86_64`` to ``py3-none-manylinux_2_28_x86_64`` and exited 0.
        PyPI rejects the un-retagged ``linux_x86_64`` form, so requiring that literal would
        have failed this gate on every correctly-built Linux wheel.

        This test exists because an earlier draft asserted the OPPOSITE — that a manylinux
        wheel should be rejected — encoding a wrong belief about auditwheel as a passing test.
        Running the real tool is what corrected it.
        """
        for tag in (
            "py3-none-macosx_26_0_arm64",
            "py3-none-manylinux_2_28_x86_64",
        ):
            _wheel_with_tag(tmp_path, tag)
        assert assert_wheel_matrix.main(["--dist", str(tmp_path)]) == 0

    def test_musllinux_does_not_satisfy_the_linux_requirement(self, tmp_path):
        """The Linux pattern must not be so loose that a skipped platform satisfies it.

        `musllinux` is explicitly skipped (no musl Rust target, no Alpine CI job), so a
        musllinux wheel appearing in place of the glibc one is a real misconfiguration. Pinned
        because the pattern's leading wildcard — needed for `manylinux` — could otherwise
        match this too and make the Linux check unfalsifiable.
        """
        for tag in (
            "py3-none-macosx_26_0_arm64",
            "py3-none-musllinux_1_2_x86_64",
        ):
            _wheel_with_tag(tmp_path, tag)
        assert assert_wheel_matrix.main(["--dist", str(tmp_path)]) == 1

    def test_wrong_arch_linux_wheel_does_not_satisfy_x86_64(self, tmp_path):
        """The suffix pins the ARCH; an aarch64 wheel must not stand in for x86_64."""
        for tag in (
            "py3-none-macosx_26_0_arm64",
            "py3-none-manylinux_2_28_aarch64",
        ):
            _wheel_with_tag(tmp_path, tag)
        assert assert_wheel_matrix.main(["--dist", str(tmp_path)]) == 1


# ---------------------------------------------------------------------------------------
# NFR-2's 10 MB ceiling
# ---------------------------------------------------------------------------------------


class TestBinarySizeCeiling:
    def test_ceiling_is_ten_binary_megabytes(self):
        """Pinned at 3.2 Q5; read strictly as 10 * 1024 * 1024, the stricter reading.

        Hard-coded literal rather than derived from the constant under test: sourcing the
        expected value from the value being checked is a guard that cannot fail.
        """
        assert build_tui.DEFAULT_MAX_BINARY_BYTES == 10485760
        assert smoke_test_wheel.DEFAULT_MAX_BINARY_BYTES == 10485760

    def test_oversized_binary_in_wheel_is_rejected(self, tmp_path):
        wheel = _wheel_with_tag(tmp_path, "py3-none-macosx_26_0_arm64", binary_size=2048)
        with pytest.raises(build_tui.BuildError, match="over NFR-2's ceiling"):
            build_tui._assert_binary_size(wheel, max_bytes=1024)

    def test_binary_at_exactly_the_ceiling_is_allowed(self, tmp_path):
        """Boundary: the ceiling is inclusive, so ``==`` must pass while ``+1`` fails."""
        wheel = _wheel_with_tag(tmp_path, "py3-none-macosx_26_0_arm64", binary_size=1024)
        build_tui._assert_binary_size(wheel, max_bytes=1024)
        with pytest.raises(build_tui.BuildError):
            build_tui._assert_binary_size(wheel, max_bytes=1023)

    def test_wheel_with_no_binary_fails_rather_than_passing_quietly(self, tmp_path):
        wheel = _make_wheel(
            tmp_path / "cli_agent_orchestrator-2.3.0-py3-none-macosx_26_0_arm64.whl",
            {"cli_agent_orchestrator/__init__.py": b""},
        )
        with pytest.raises(build_tui.BuildError, match="no cao-tui\\* binary found"):
            build_tui._assert_binary_size(wheel, max_bytes=10485760)


# ---------------------------------------------------------------------------------------
# The per-platform smoke test (SR-1)
# ---------------------------------------------------------------------------------------


class TestSmokeTestAssertions:
    def test_any_tag_is_rejected(self, tmp_path):
        wheel = _wheel_with_tag(tmp_path, "py3-none-any")
        with pytest.raises(smoke_test_wheel.SmokeTestError, match="platform 'any'"):
            smoke_test_wheel.assert_platform_tag(wheel)

    def test_platform_tag_is_accepted(self, tmp_path):
        wheel = _wheel_with_tag(tmp_path, "py3-none-macosx_26_0_arm64")
        smoke_test_wheel.assert_platform_tag(wheel)

    def test_wheel_without_wheel_metadata_fails(self, tmp_path):
        wheel = _make_wheel(tmp_path / "broken-1.0-py3-none-any.whl", {"a.py": b""})
        with pytest.raises(smoke_test_wheel.SmokeTestError, match="no .dist-info/WHEEL"):
            smoke_test_wheel.assert_platform_tag(wheel)

    def test_missing_wheel_is_a_failure_not_a_pass(self, tmp_path):
        assert smoke_test_wheel.main(["--wheel", str(tmp_path / "absent.whl")]) == 1


# ---------------------------------------------------------------------------------------
# Cross-compilation: the arch cargo builds must match the arch the wheel is tagged for
# ---------------------------------------------------------------------------------------


class TestMacosCrossCompileTarget:
    """ARCHFLAGS drives BOTH the wheel tag and the cargo target; they must not diverge.

    A mismatch here is invisible until exec time: an arm64 binary inside an x86_64-tagged
    wheel installs cleanly on an Intel Mac and then fails to run.
    """

    @pytest.mark.parametrize(
        "archflags,expected",
        [
            ("-arch arm64", "aarch64-apple-darwin"),
            ("-arch x86_64", "x86_64-apple-darwin"),
        ],
    )
    def test_archflags_maps_to_the_matching_rust_target(self, archflags, expected, monkeypatch):
        monkeypatch.setenv("ARCHFLAGS", archflags)
        arch = build_tui._requested_macos_arch()
        assert build_tui._ARCHFLAGS_TO_RUST_TARGET[arch] == expected

    def test_no_archflags_means_a_native_build(self, monkeypatch):
        monkeypatch.delenv("ARCHFLAGS", raising=False)
        assert build_tui._requested_macos_arch() is None

    def test_blank_archflags_means_a_native_build(self, monkeypatch):
        monkeypatch.setenv("ARCHFLAGS", "   ")
        assert build_tui._requested_macos_arch() is None

    def test_universal2_request_is_refused_rather_than_silently_single_arch(self, monkeypatch):
        """Two archs need a `lipo` of two cargo targets, which this script does not do.

        Returning one arch here would put a single-arch binary inside a universal2-tagged
        wheel — broken on half the Macs that accept it.
        """
        monkeypatch.setenv("ARCHFLAGS", "-arch arm64 -arch x86_64")
        with pytest.raises(build_tui.BuildError, match="cannot produce a universal2 binary"):
            build_tui._requested_macos_arch()


class TestReleaseToolchainInstallsEveryCrossTarget:
    """Every cross-compiled leg must INSTALL the Rust target it asks cargo to build for.

    The defect this locks in was real and release-only: the toolchain step installed
    ``stable`` with no ``targets:`` while ``build_tui.py`` passed
    ``--target x86_64-apple-darwin`` under cibuildwheel's ``ARCHFLAGS=-arch x86_64``. The
    runner images ship host-target-only rustup, so that leg failed on "target may not be
    installed" — and ``publish-testpypi`` ``needs: build-wheels``, so the first release after
    merge would have died. No PR check could catch it: this workflow runs only on release and
    ``workflow_dispatch``. (Reported by review on PR #547.)

    Asserted against the PARSED yaml and against ``build_tui.py``'s own arch map, not against
    a string in the file. A grep for ``targets:`` would keep passing if a leg were added later
    without one, which is the same silence the original defect had.

    NO LEG CROSS-COMPILES TODAY — the one that did (macOS x86_64) was dropped in v2.5.0 for a
    dependency reason, not because cross-compiling stopped working. So the arch-map agreement
    tests below are currently vacuous by construction, and the load-bearing assertion is
    ``test_the_toolchain_step_installs_the_matrix_target``: it fails if the plumbing that a
    restored leg would need is deleted as dead config.
    """

    WORKFLOW = PUBLISH_WORKFLOW

    @staticmethod
    def _wheel_matrix() -> list:
        return _wheel_matrix_legs()

    @staticmethod
    def _toolchain_step() -> dict:
        steps = _build_wheels_steps()
        matches = [s for s in steps if "dtolnay/rust-toolchain" in str(s.get("uses", ""))]
        assert len(matches) == 1, (
            f"expected exactly one rust-toolchain step in build-wheels, found {len(matches)}; "
            "this test reads that one step's `targets:` input"
        )
        return matches[0]

    def test_the_toolchain_step_installs_the_matrix_target(self):
        """The step must take its target FROM the matrix, not hard-code one leg's triple."""
        step = self._toolchain_step()
        targets = step.get("with", {}).get("targets")
        assert targets is not None, (
            "the rust-toolchain step declares no `targets:` input, so a cross-compiled leg "
            "builds for a target whose std was never installed — release-only breakage"
        )
        assert targets.strip() == "${{ matrix.rust-target }}", (
            "`targets:` must be driven by the matrix so each leg installs its OWN target; "
            f"got {targets!r}"
        )

    def test_every_leg_declares_a_rust_target_key(self):
        """A leg with no key at all silently expands to an empty target — the original bug.

        An absent key and an intentionally-empty one look identical to the action, so the key
        is required on every leg to force the choice to be made once per platform.
        """
        missing = [leg["label"] for leg in self._wheel_matrix() if "rust-target" not in leg]
        assert not missing, f"these wheel legs declare no `rust-target`: {missing}"

    def test_macos_cross_legs_name_the_triple_build_tui_will_pass_to_cargo(self):
        """The matrix triple and `build_tui.py`'s ARCHFLAGS map must be the same string.

        Two independent copies of one triple; a typo in either produces a leg that installs
        one target and builds for another. Derived from the script's map rather than repeated
        as a literal, so the two cannot drift apart without failing here.
        """
        host_arch_for_macos_runner = "arm64"  # `macos-latest` has been arm64 since macOS 14
        for leg in self._wheel_matrix():
            if not str(leg["os"]).startswith("macos"):
                continue
            expected = (
                ""
                if leg["archs"] == host_arch_for_macos_runner
                else build_tui._ARCHFLAGS_TO_RUST_TARGET[leg["archs"]]
            )
            assert leg["rust-target"] == expected, (
                f"leg {leg['label']!r} builds for arch {leg['archs']!r}, which "
                f"build_tui.py maps to {expected!r}, but the matrix installs "
                f"{leg['rust-target']!r}"
            )

    def test_a_non_host_arch_leg_cannot_declare_an_empty_target(self):
        """The guard's own negative control: it must REJECT the pre-fix configuration.

        Without this, `test_macos_cross_legs_name_the_triple...` could be satisfied by a map
        that returns `""` for everything. Here the x86_64 leg is forced back to the empty
        value the workflow shipped with, and the same comparison must fail.
        """
        pre_fix_leg = {
            "os": "macos-latest",
            "label": "macOS x86_64",
            "archs": "x86_64",
            "rust-target": "",
        }
        expected = build_tui._ARCHFLAGS_TO_RUST_TARGET[pre_fix_leg["archs"]]
        assert expected, "the arch map must yield a non-empty triple for a cross-built arch"
        assert pre_fix_leg["rust-target"] != expected, (
            "the pre-fix configuration must not compare equal to the fixed one, or the "
            "assertion above cannot fail on the defect it exists to catch"
        )


# ---------------------------------------------------------------------------------------
# Configuration invariants
# ---------------------------------------------------------------------------------------


class TestConfigurationInvariants:
    @staticmethod
    def _pyproject() -> dict:
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10
            import tomli as tomllib  # type: ignore[no-redef]
        return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_binary_stem_agrees_across_every_place_it_is_declared(self):
        """Five copies of one name. A drift makes the artifacts glob match nothing —
        and hatchling treats that as a SILENT no-op (defect D1's failure mode)."""
        cargo = (REPO_ROOT / "tui" / "Cargo.toml").read_text(encoding="utf-8")
        assert 'name = "cao-tui"' in cargo

        assert build_tui.BINARY_STEM == "cao-tui"
        assert smoke_test_wheel.BINARY_STEM == "cao-tui"

        # The hook's copy is read as TEXT rather than imported, so this invariant holds in
        # environments without hatchling — the drift it guards against is the whole point.
        hook_source = (SCRIPTS_DIR / "hatch_build_tui_tag.py").read_text(encoding="utf-8")
        assert 'BINARY_STEM = "cao-tui"' in hook_source

        # cli/commands/tui.py locates the binary at runtime; a drift here means `cao tui`
        # cannot find a binary the wheel does in fact contain.
        cli_source = (
            REPO_ROOT / "src" / "cli_agent_orchestrator" / "cli" / "commands" / "tui.py"
        ).read_text(encoding="utf-8")
        assert '_BINARY_STEM = "cao-tui"' in cli_source

        globs = self._pyproject()["tool"]["hatch"]["build"]["artifacts"]
        assert "src/cli_agent_orchestrator/cao-tui*" in globs

    def test_build_hook_is_registered_for_the_wheel_target(self):
        """Without the registration the tag stays py3-none-any and the defect returns."""
        hooks = self._pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["hooks"]
        assert hooks["custom"]["path"] == "scripts/hatch_build_tui_tag.py"

    def test_cibuildwheel_before_build_stages_the_binary(self):
        """Ordering is load-bearing: the binary must exist BEFORE hatchling globs for it."""
        cibw = self._pyproject()["tool"]["cibuildwheel"]
        assert "scripts/build_tui.py build" in cibw["before-build"]

    def test_cibuildwheel_test_command_executes_the_binary(self):
        """SR-1: `cao --help` passes with NO binary in the wheel. Only an exec proves it."""
        cibw = self._pyproject()["tool"]["cibuildwheel"]
        assert "smoke_test_wheel.py" in cibw["test-command"]
        assert "--max-binary-bytes" in cibw["test-command"]

    def test_cibuildwheel_builds_one_abi_independent_wheel_per_platform(self):
        cibw = self._pyproject()["tool"]["cibuildwheel"]
        assert cibw["build"] == "cp310-*", (
            "one build per platform; cibuildwheel reuses the py3-none wheel for the other "
            "interpreters via find_compatible_wheel()"
        )
        assert cibw["archs"] == ["auto64"]

    def test_cargo_build_passes_locked(self):
        """SR-5: a release must not silently resolve different dependency versions."""
        source = (SCRIPTS_DIR / "build_tui.py").read_text(encoding="utf-8")
        assert '["cargo", "build", "--release", "--locked"]' in source

    def test_trivy_can_fail_the_build(self):
        """SR-4: without exit-code, "Security Scan passed" attests completion, not cleanliness."""
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "exit-code: '1'" in ci

    def test_every_action_added_by_this_unit_is_sha_pinned(self):
        """SR-2: a `uses:` on a mutable tag or branch is unreviewable supply chain.

        Asserted over the whole publish workflow because that is the file this unit owns and
        the one holding the PyPI credential path. A 40-hex ref is required for every
        third-party action; the local `./`-style and reusable-workflow forms are absent here.
        """
        import re

        text = (REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml").read_text(
            encoding="utf-8"
        )
        unpinned = [
            ref
            for ref in re.findall(r"^\s*-?\s*uses:\s*(\S+)", text, re.MULTILINE)
            if not re.search(r"@[0-9a-f]{40}$", ref)
        ]
        assert not unpinned, f"these `uses:` are not SHA-pinned: {unpinned}"

    def test_pypi_publish_action_is_sha_pinned_specifically(self):
        """Called out separately: this action holds the OIDC credential path to PyPI.

        `release/v1` is a mutable BRANCH — the highest-consequence moving reference in the
        repo. Named in its own test so a regression here cannot hide inside the aggregate.
        """
        text = (REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml").read_text(
            encoding="utf-8"
        )
        assert "pypa/gh-action-pypi-publish@release/v1" not in text
        assert "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33" in text

    def test_wheel_jobs_carry_an_explicit_timeout(self):
        """No job in this repo had one; a hung pty/container build must not burn 6 hours."""
        text = (REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml").read_text(
            encoding="utf-8"
        )
        assert text.count("timeout-minutes:") >= 5

    def test_matrix_covers_the_two_shippable_platforms(self):
        """Interview Q2 named four. Two are not shippable — see the next two tests."""
        legs = _wheel_matrix_legs()
        assert {leg["label"] for leg in legs} == {
            "macOS arm64",
            "Linux x86_64",
        }
        assert {leg["os"] for leg in legs} == {"macos-latest", "ubuntu-latest"}
        assert {leg["archs"] for leg in legs} == {"arm64", "x86_64"}

    def test_no_windows_leg_is_built_or_smoke_tested(self):
        """Windows cannot import this package, so a wheel for it must not be produced.

        v2.5.0 shipped a matrix that built one. `cao-tui.exe` was fine; the Python package
        died on `ModuleNotFoundError: No module named 'fcntl'` in the smoke test. Pinned in
        FOUR places because a leg restored in any one of them alone would either publish a
        broken wheel or deadlock the publish gate on an artifact nobody builds.

        Asserted over the PARSED runner values, not a grep for "windows-latest": the first
        draft of this test grepped, and failed on its own explanatory comment. A substring
        check over YAML cannot tell configuration from prose ABOUT configuration.
        """
        workflow = _publish_workflow()
        for job in ("build-wheels", "smoke-test"):
            runners = {
                str(leg["os"]) for leg in workflow["jobs"][job]["strategy"]["matrix"]["include"]
            }
            assert not any("windows" in r for r in runners), (
                f"a Windows runner is back in the {job} matrix ({sorted(runners)}); the "
                "package still cannot be imported on Windows (fcntl at module scope, "
                "tmux-only backend)"
            )
        assert (
            "Windows AMD64" not in assert_wheel_matrix.REQUIRED_PLATFORM_PATTERNS
        ), "the publish gate would block on a wheel the matrix no longer builds"
        assert "*-win*" in self._pyproject()["tool"]["cibuildwheel"]["skip"]

    def test_wheel_job_provides_a_host_python_for_the_assert_step(self):
        """The v2.5.0 macOS arm64 failure: `python: command not found`, exit 127.

        cibuildwheel supplies interpreters for the BUILD, so this job originally had no
        setup-python — but the `Assert TUI binary is inside each wheel` step shells out to
        `python` on the HOST, and the macOS images provide only `python3`. That step runs
        AFTER cibuildwheel has already produced and smoke-tested a good wheel, so the whole
        leg failed on a working artifact.
        """
        steps = _build_wheels_steps()
        setup_python = [s for s in steps if "actions/setup-python@" in s.get("uses", "")]
        assert setup_python, (
            "build-wheels invokes `python` on the host but sets up no interpreter; the macOS "
            "runner images have only `python3`"
        )

        # Ordering is the whole point: an interpreter configured after the step that needs it
        # is no interpreter at all.
        def index_of(predicate) -> int:
            return next(i for i, s in enumerate(steps) if predicate(s))

        assert index_of(lambda s: "actions/setup-python@" in s.get("uses", "")) < index_of(
            lambda s: "python scripts/build_tui.py check" in s.get("run", "")
        )

    def test_macos_deployment_target_is_declared_per_leg_and_reaches_cibuildwheel(self):
        """The v2.5.0 macOS x86_64 failure, in delocate:

            DelocationError: Library dependencies do not satisfy target MacOS version 10.9:
              .../cao-tui has a minimum target of 10.12

        delocate compares the bundled binary's minimum-OS against the wheel's platform tag, so
        each leg's MACOSX_DEPLOYMENT_TARGET has to clear its own binary's floor. It is declared
        PER LEG because cibuildwheel's defaults differ by arch (10.9 x86_64, 11.0 arm64) and so
        do the floors — one macOS-wide value is necessarily wrong for one of them.

        Only the arm64 leg remains (the Intel leg is gone; see the test below), so what is
        pinned here is the value AND the plumbing. Without the env line the matrix key would be
        inert config that reads as if it were doing something.
        """
        targets = {leg["label"]: leg["macos-target"] for leg in _wheel_matrix_legs()}
        assert (
            targets["macOS arm64"] == "11.0"
        ), "arm64's binary floor is 11.0; a lower value here is rejected by delocate at repair"
        assert targets["Linux x86_64"] == "", "the variable is meaningless on Linux"
        assert all(
            "macos-target" in leg for leg in _wheel_matrix_legs()
        ), "every leg must declare the key, so an added platform has to make the choice"

        # And the value has to actually reach cibuildwheel.
        steps = _build_wheels_steps()
        cibw = next(s for s in steps if "pypa/cibuildwheel@" in s.get("uses", ""))
        assert cibw["env"]["MACOSX_DEPLOYMENT_TARGET"] == "${{ matrix.macos-target }}"

    def test_no_intel_macos_wheel_is_built_or_required(self):
        """cryptography ships no Intel-macOS wheel at the versions we are allowed to use.

        Measured over every cryptography release on PyPI: 48.0.1 and earlier ship
        `macosx_10_9_universal2`; 49.0.0 onward ship `macosx_11_0_arm64` and nothing else for
        macOS. This project floors at `cryptography>=50.0.0` because 49.0.0 fixes
        CVE-2026-69249 and 50.0.0 fixes CVE-2026-69247, both HIGH — so every permitted version
        is one with no Intel wheel, and an Intel-Mac install must compile it from source.

        v2.5.0's x86_64 leg died on exactly that inside cibuildwheel's test venv: pkg-config
        cannot supply an x86_64 OpenSSL from an arm64 host. Suppressing the leg's test would
        have shipped a wheel that fails the same way on the operator's machine, so the leg is
        gone instead.

        Pinned in three places, because restoring it in any one alone either publishes a wheel
        that cannot install or deadlocks the publish gate on an artifact nobody builds.
        """
        legs = _wheel_matrix_legs()
        intel = [
            leg["label"]
            for leg in legs
            if str(leg["os"]).startswith("macos") and leg["archs"] == "x86_64"
        ]
        assert not intel, (
            f"an Intel-macOS leg is back in build-wheels ({intel}); cryptography>=50 has no "
            "Intel-macOS wheel, so the leg's test venv cannot resolve it and neither can an "
            "operator's Intel Mac"
        )
        assert (
            "macOS x86_64" not in assert_wheel_matrix.REQUIRED_PLATFORM_PATTERNS
        ), "the publish gate would block forever on a wheel the matrix no longer builds"
        assert "*-macosx_x86_64" in self._pyproject()["tool"]["cibuildwheel"]["skip"], (
            "a local cibuildwheel run on an Intel Mac would otherwise emit the wheel that "
            "CI deliberately does not"
        )

    def test_wheelhouse_is_gitignored(self):
        """cibuildwheel's output dir holds multi-MB binaries; it must not enter history."""
        assert "wheelhouse/" in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")


class TestWindowsIsRefusedAtImport:
    """Not publishing a Windows wheel is NOT the same as refusing to install on Windows.

    No installer enforces an ``Operating System`` classifier, and metadata cannot express an
    OS constraint. Without a wheel, pip falls back to the sdist, which builds and installs
    fine — the Rust hook is inert without cargo. So the guard in
    ``cli_agent_orchestrator/__init__.py`` is the only thing standing between a Windows
    operator and ``ModuleNotFoundError: No module named 'fcntl'`` from whichever submodule
    happened to be imported first.

    The module source is exec'd under a patched ``sys.platform`` because the real package is
    already imported by the time these tests run, so a plain ``import`` cannot re-trigger it.
    """

    SOURCE = REPO_ROOT / "src" / "cli_agent_orchestrator" / "__init__.py"

    def _exec(self) -> None:
        exec(  # noqa: S102 - executing our own source under a patched platform is the test
            compile(self.SOURCE.read_text(encoding="utf-8"), str(self.SOURCE), "exec"),
            {"__name__": "cli_agent_orchestrator"},
        )

    def test_import_on_win32_raises_and_says_why(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        with pytest.raises(ImportError) as excinfo:
            self._exec()
        message = str(excinfo.value)
        # The diagnosis, the cause, and the way forward — a bare "unsupported" would leave the
        # operator no better off than the ModuleNotFoundError it replaces.
        assert "does not support Windows" in message
        assert "fcntl" in message and "tmux" in message
        assert "WSL2" in message

    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_the_guard_is_inert_on_supported_platforms(self, monkeypatch, platform):
        """The negative control. A guard that fires everywhere would break every install."""
        monkeypatch.setattr(sys, "platform", platform)
        self._exec()


def test_wheel_matrix_documents_unverified_platforms():
    """The honesty requirement, asserted rather than left to a report.

    The Linux wheel CANNOT be built or executed on the development machine (macOS arm64), and
    the operator declined Docker/QEMU emulation. The configuration must therefore state what
    remains CI's to prove — a config that reads as though everything were verified is exactly
    the "passed CI but partially worked" failure this intent exists to eliminate, made worse
    because wheels reach operators.

    Since v2.5.0 the remaining admission is no longer about a PLATFORM: both unverifiable
    platforms were dropped rather than shipped unverified, so every wheel published is built
    and executed by CI. What is left is the smoke-test-on-dispatch coverage gap.

    This test fails if those admissions are removed from the config, so the caveat cannot be
    quietly dropped by a later edit.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "CONSEQUENCE, stated rather than glossed" in text, (
        "the pyproject cibuildwheel section must keep naming the known gaps (the "
        "linux_x86_64/PyPI rejection, why the Intel-macOS leg was dropped rather than "
        "shipped untested, and the smoke-test-on-dispatch hole)"
    )
    assert "UNVERIFIED" in text
