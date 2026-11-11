"""Structural regressions for workflow authoring CodeQL path guards."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import workflow_spec_service as svc


def _calls(function: object) -> list[ast.Call]:
    tree = ast.parse(inspect.getsource(function))
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _call_name(call: ast.Call) -> str | None:
    return call.func.id if isinstance(call.func, ast.Name) else None


# Every function that publishes a spec file. The YAML pair joined the Python pair for the
# workflow builder's save path, and is held to the same structural rules.
_AUTHORING_WRITERS = (
    svc.create_workflow,
    svc.update_workflow,
    svc.create_yaml_workflow,
    svc.update_yaml_workflow,
)


def test_authoring_uses_identity_only_lock_entrypoint() -> None:
    """A contained workflow path must not flow into target-path lock resolution."""
    for function in _AUTHORING_WRITERS:
        call_names = {_call_name(call) for call in _calls(function)}
        assert "strict_identity_lock" in call_names
        assert "strict_target_lock" not in call_names


def test_lock_identity_stats_only_the_trusted_parent() -> None:
    """The workflow name-derived target must not influence the stat path."""
    stat_calls = [
        call
        for call in _calls(svc._workflow_lock_identity)
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "os"
        and call.func.attr == "stat"
    ]
    assert len(stat_calls) == 1
    assert ast.unparse(stat_calls[0].args[0]) == "canonical_parent"

    for function in _AUTHORING_WRITERS:
        identity_calls = [
            call for call in _calls(function) if _call_name(call) == "_workflow_lock_identity"
        ]
        assert len(identity_calls) == 1
        assert ast.unparse(identity_calls[0].args[1]) == "safe_base"


@pytest.mark.parametrize("function", [svc.create_yaml_workflow, svc.update_yaml_workflow])
def test_yaml_writers_lock_on_the_python_writers_identity(function: object) -> None:
    """One lock per workflow name across both tiers, as ``delete_workflow`` already uses.

    A YAML writer locking on ``<name>.yaml`` instead would let a Python create and a YAML
    create of one name run their existence checks at the same time and both publish.
    """
    tree = ast.parse(inspect.getsource(function))
    lock_targets = [
        ast.unparse(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and [ast.unparse(target) for target in node.targets] == ["lock_target"]
    ]
    assert lock_targets == ["_safe_spec_path(os.path.join(safe_base, f'{name}.py'), safe_base)"]
    identity_calls = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and _call_name(call) == "_workflow_lock_identity"
    ]
    assert [ast.unparse(arg) for arg in identity_calls[0].args] == ["lock_target", "safe_base"]


@pytest.mark.parametrize("function", [svc.create_yaml_workflow, svc.update_yaml_workflow])
def test_yaml_existence_checks_use_contained_targets(function: object) -> None:
    """Each YAML existence check takes a value ``_safe_spec_path`` returned, never a raw join."""
    tree = ast.parse(inspect.getsource(function))
    contained = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and _call_name(node.value) == "_safe_spec_path"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    exists_calls = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and _call_name(call) == "_contained_spec_exists"
    ]
    assert len(exists_calls) == 3, "the .yaml, .yml and .py candidates"
    for call in exists_calls:
        first, second = (ast.unparse(arg) for arg in call.args)
        assert first in contained, f"{first} is not a contained target"
        assert second == "safe_base"


def test_create_existence_check_uses_contained_target() -> None:
    """Create admission must not reintroduce the unchecked name-derived path."""
    contained_exists_calls = [
        call for call in _calls(svc.create_workflow) if _call_name(call) == "_contained_spec_exists"
    ]
    assert len(contained_exists_calls) == 1
    assert [ast.unparse(arg) for arg in contained_exists_calls[0].args] == [
        "lock_target",
        "safe_base",
    ]


def test_contained_exists_colocates_guard_with_sink() -> None:
    """CodeQL's positive containment barrier must dominate exists locally."""
    tree = ast.parse(inspect.getsource(svc._contained_spec_exists))
    conditions = [ast.unparse(node.test) for node in ast.walk(tree) if isinstance(node, ast.If)]
    assert "not real_path.startswith(safe_base + os.sep)" in conditions

    exists_calls = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Attribute)
        and isinstance(call.func.value.value, ast.Name)
        and call.func.value.value.id == "os"
        and call.func.value.attr == "path"
        and call.func.attr == "exists"
    ]
    assert len(exists_calls) == 1
    assert ast.unparse(exists_calls[0].args[0]) == "real_path"


def test_update_existence_check_uses_contained_target() -> None:
    """Update admission must not reintroduce the unchecked name-derived path.

    The first check is the ``.py`` target. When it misses, one more check, run for the
    ``.yaml`` and ``.yml`` names, tells a YAML workflow apart from a missing one (a 409
    that says so). It takes a value ``_safe_spec_path`` returned, as the YAML writers do.
    """
    tree = ast.parse(inspect.getsource(svc.update_workflow))
    contained = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and _call_name(node.value) == "_safe_spec_path"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    contained_exists_calls = sorted(
        (
            call
            for call in ast.walk(tree)
            if isinstance(call, ast.Call) and _call_name(call) == "_contained_spec_exists"
        ),
        key=lambda call: (call.lineno, call.col_offset),
    )
    assert len(contained_exists_calls) == 2, "the .py target, then the YAML candidates"
    assert [ast.unparse(arg) for arg in contained_exists_calls[0].args] == [
        "lock_target",
        "safe_base",
    ]
    for call in contained_exists_calls:
        first, second = (ast.unparse(arg) for arg in call.args)
        assert first in contained, f"{first} is not a contained target"
        assert second == "safe_base"


def test_contained_exists_refuses_an_escaping_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    link = tmp_path / "escape.py"
    link.symlink_to(outside)

    with pytest.raises(svc.SpecPathRefusedError, match="escapes its validated directory"):
        svc._contained_spec_exists(link, str(tmp_path))


def test_contained_exists_refuses_the_base_itself(tmp_path: Path) -> None:
    with pytest.raises(svc.SpecPathRefusedError, match="escapes its validated directory"):
        svc._contained_spec_exists(tmp_path, str(tmp_path))


def test_contained_exists_returns_false_for_an_in_base_dangling_symlink(
    tmp_path: Path,
) -> None:
    link = tmp_path / "dangling.py"
    link.symlink_to(tmp_path / "missing-target.py")

    assert svc._contained_spec_exists(link, str(tmp_path)) is False


def test_contained_exists_returns_true_for_an_in_base_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "workflow.py"
    target.write_text("INPUTS = {}\n")

    assert svc._contained_spec_exists(target, str(tmp_path)) is True


def test_contained_exists_returns_false_for_an_in_base_missing_name(tmp_path: Path) -> None:
    assert svc._contained_spec_exists(tmp_path / "missing.py", str(tmp_path)) is False
