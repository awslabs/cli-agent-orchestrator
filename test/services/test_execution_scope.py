"""Finite SCOPE parsing and private binding to actual Git working copies."""

from __future__ import annotations

import json
import os
import subprocess
import traceback
from dataclasses import fields
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, cast

import pytest

from cli_agent_orchestrator.services import execution_scope, private_plan_snapshot


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "scope@example.com")
    _git(path, "config", "user.name", "Scope Test")
    (path / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "initial")
    return path


def _repository_with_command_scoped_identity(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    (path / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(
        path,
        "-c",
        "user.email=scope@example.com",
        "-c",
        "user.name=Scope Test",
        "commit",
        "-qm",
        "initial",
    )
    return path


def _scope(
    *,
    targets: str = ('"main": {"agents": ["developer", "reviewer"], "memory": "exact-snapshot"}'),
) -> str:
    return f'SCOPE = {{"version": 1, "targets": {{{targets}}}}}\n'


def _freeze(source: str, mappings: object) -> execution_scope.ExecutionScopeFreeze:
    declaration = execution_scope.parse_scope_declaration(source)
    return execution_scope.freeze_execution_scope(declaration, mappings)


def _actual_binding(
    frozen: execution_scope.ExecutionScopeFreeze, index: int = 0
) -> execution_scope.TargetBinding:
    binding = frozen.bindings[index]
    assert isinstance(binding, execution_scope.TargetBinding)
    return binding


def test_scope_parser_returns_immutable_stably_serialized_declaration() -> None:
    first = execution_scope.parse_scope_declaration(
        _scope(
            targets=(
                '"z": {"memory": "off", "agents": ["reviewer"]},'
                '"a": {"agents": ["developer"], "memory": "exact-snapshot"}'
            )
        )
    )
    second = execution_scope.parse_scope_declaration(
        _scope(
            targets=(
                '"a": {"memory": "exact-snapshot", "agents": ["developer"]},'
                '"z": {"agents": ["reviewer"], "memory": "off"}'
            )
        )
    )

    assert first == second
    assert first.material_bytes == second.material_bytes
    assert json.loads(first.material_bytes) == {
        "targets": {
            "a": {"agents": ["developer"], "memory": "exact-snapshot"},
            "z": {"agents": ["reviewer"], "memory": "off"},
        },
        "version": 1,
    }
    with pytest.raises(AttributeError):
        first.targets = ()  # type: ignore[misc]
    assert first.digest == sha256(first.material_bytes).hexdigest()


def test_scope_parser_ignores_nested_local_scope_names_and_allows_reads() -> None:
    declaration = execution_scope.parse_scope_declaration(
        _scope()
        + "COPY = SCOPE\n"
        + "def helper():\n"
        + "    SCOPE = {'not': 'the module declaration'}\n"
        + "    return SCOPE\n"
    )

    assert tuple(profile.value for profile in declaration.targets[0].allowed_agent_profiles) == (
        "developer",
        "reviewer",
    )


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("x = 1\n", "scope_missing"),
        ("SCOPE = {}\n", "scope_missing_key"),
        (
            'SCOPE = {"version": 2, "targets": {"main": {"agents": ["d"], "memory": "off"}}}\n',
            "scope_version_unsupported",
        ),
        (
            'SCOPE = {"version": 1, "version": 1, "targets": {}}\n',
            "scope_duplicate_key",
        ),
        (
            _scope() + _scope(),
            "scope_duplicate_assignment",
        ),
        (
            'SCOPE = {"version": 1, "targets": {"main": {"agents": ["d"], "memory": "live"}}}\n',
            "scope_memory_mode_invalid",
        ),
        (
            'SCOPE = {"version": 1, "targets": {"main": {"agents": ["d"], "memory": "off", "x": 1}}}\n',
            "scope_unknown_key",
        ),
        (
            'SCOPE = {"version": 1, "targets": {"main": {"agents": ["d", "d"], "memory": "off"}}}\n',
            "scope_duplicate_agent",
        ),
        (
            'SCOPE = {"version": 1, "targets": {"unsafe/key": {"agents": ["d"], "memory": "off"}}}\n',
            "scope_target_key_invalid",
        ),
    ],
)
def test_scope_parser_refuses_invalid_or_ambiguous_declarations(source: str, code: str) -> None:
    with pytest.raises(execution_scope.ScopeDeclarationError) as caught:
        execution_scope.parse_scope_declaration(source)

    assert caught.value.status_code == 422
    assert caught.value.retryable is False
    assert caught.value.code == code


def test_scope_parser_never_executes_dynamic_declaration_or_module_body(tmp_path: Path) -> None:
    sentinel = tmp_path / "would-have-been-written"
    source = (
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed')\n"
        "def build():\n"
        f"    Path({str(sentinel)!r}).write_text('executed')\n"
        "    return {}\n"
        "SCOPE = build()\n"
    )

    with pytest.raises(execution_scope.ScopeDeclarationError) as caught:
        execution_scope.parse_scope_declaration(source)

    assert caught.value.code == "scope_dynamic_expression"
    assert not sentinel.exists()
    assert str(sentinel) not in str(caught.value)


def test_scope_parser_refuses_parent_cycles_without_echoing_source() -> None:
    source = _scope(
        targets=(
            '"a": {"agents": ["d"], "memory": "off", "worktree": "ephemeral-child-of:b"},'
            '"b": {"agents": ["d"], "memory": "off", "worktree": "ephemeral-child-of:a"}'
        )
    )

    with pytest.raises(execution_scope.ScopeDeclarationError) as caught:
        execution_scope.parse_scope_declaration(source)

    assert caught.value.code == "scope_parent_cycle"
    assert caught.value.target_key in {"a", "b"}


def test_resolver_requires_one_explicit_mapping_per_declared_target(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    declaration = execution_scope.parse_scope_declaration(
        _scope(
            targets=(
                '"main": {"agents": ["d"], "memory": "off"},'
                '"other": {"agents": ["d"], "memory": "off"}'
            )
        )
    )

    with pytest.raises(execution_scope.TargetResolutionError) as missing:
        execution_scope.freeze_execution_scope(declaration, {"main": {"path": str(repo)}})
    with pytest.raises(execution_scope.TargetResolutionError) as unknown:
        execution_scope.freeze_execution_scope(
            declaration,
            {
                "main": {"path": str(repo)},
                "other": {"path": str(repo)},
                "extra": {"path": str(repo)},
            },
        )

    assert missing.value.code == "target_mapping_missing"
    assert missing.value.target_key == "other"
    assert unknown.value.code == "target_mapping_unknown"
    assert unknown.value.target_key == "extra"


def test_resolver_returns_typed_mapping_options(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    declaration = execution_scope.parse_scope_declaration(
        _scope(
            targets=(
                '"main": {"agents": ["d"], "memory": "off"},'
                '"sandbox": {"agents": ["d"], "memory": "off",'
                ' "worktree": "ephemeral-child-of:main"}'
            )
        )
    )

    options = dict(
        execution_scope.resolve_target_mappings(
            declaration,
            {
                "main": {"path": str(repo)},
                "sandbox": {"parent": "main", "baseline": "HEAD"},
            },
        )
    )

    assert options == {
        "main": execution_scope.GitWorkingCopyOption(str(repo)),
        "sandbox": execution_scope.EphemeralChildOption("main", "HEAD"),
    }


def test_non_git_server_cwd_with_valid_explicit_git_target_is_supported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repository(tmp_path / "repo")
    server = tmp_path / "server"
    server.mkdir()
    monkeypatch.chdir(server)

    frozen = _freeze(_scope(), {"main": {"path": str(repo)}})

    assert _actual_binding(frozen).commit == _git(repo, "rev-parse", "HEAD")
    assert _actual_binding(frozen).worktree_state == (("status", "clean"),)


def test_non_git_target_refuses_with_secret_safe_nonretryable_error(tmp_path: Path) -> None:
    private_target = tmp_path / "customer-secret-target"
    private_target.mkdir()

    with pytest.raises(execution_scope.TargetResolutionError) as caught:
        _freeze(_scope(), {"main": {"path": str(private_target)}})

    assert caught.value.status_code == 422
    assert caught.value.retryable is False
    assert caught.value.code == "target_not_git"
    assert caught.value.target_key == "main"
    assert str(private_target) not in str(caught.value)


def test_actual_mapping_clone_and_baseline_changes_change_target_material(tmp_path: Path) -> None:
    origin = _repository(tmp_path / "origin")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))

    from_origin = _freeze(_scope(), {"main": {"path": str(origin)}})
    from_clone = _freeze(_scope(), {"main": {"path": str(clone)}})
    assert from_origin.targets_digest != from_clone.targets_digest

    (origin / "tracked.txt").write_text("two\n", encoding="utf-8")
    dirty = _freeze(_scope(), {"main": {"path": str(origin)}})
    (origin / "untracked.txt").write_text("new\n", encoding="utf-8")
    dirtier = _freeze(_scope(), {"main": {"path": str(origin)}})
    assert (
        len(
            {
                from_origin.targets_digest,
                dirty.targets_digest,
                dirtier.targets_digest,
            }
        )
        == 3
    )
    assert (
        _actual_binding(from_origin).instance_key
        == _actual_binding(dirty).instance_key
        == _actual_binding(dirtier).instance_key
    )


def test_new_commit_changes_digest_but_not_actual_instance_key(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    before = _freeze(_scope(), {"main": {"path": str(repo)}})

    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "second")
    after = _freeze(_scope(), {"main": {"path": str(repo)}})

    assert before.targets_digest != after.targets_digest
    assert _actual_binding(before).instance_key == _actual_binding(after).instance_key


def test_actual_instance_helper_matches_real_freeze_without_changing_private_material(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")

    identity = execution_scope.derive_actual_instance_identity(str(repo), target_key="main")
    binding = _actual_binding(_freeze(_scope(), {"main": {"path": str(repo)}}))
    private = json.loads(binding.private_material_bytes)

    assert tuple(field.name for field in fields(execution_scope.ActualInstanceIdentity)) == (
        "repo_realpath",
        "workcopy_realpath",
        "workcopy_rel",
        "filesystem_identity",
        "instance_key",
        "git_common_dir_realpath",
    )
    assert identity == execution_scope.ActualInstanceIdentity(
        repo_realpath=binding.repo_realpath,
        workcopy_realpath=binding.workcopy_realpath,
        workcopy_rel=binding.workcopy_rel,
        filesystem_identity=binding.filesystem_identity,
        instance_key=binding.instance_key,
        git_common_dir_realpath=str(repo / ".git"),
    )
    assert set(private) == {
        "commit",
        "dirty_digest",
        "filesystem_identity",
        "instance_key",
        "key",
        "kind",
        "repo_realpath",
        "requested_realpath",
        "requested_subdirectory",
        "requested_working_directory",
        "schema",
        "workcopy_realpath",
        "workcopy_rel",
        "workcopy_role",
        "worktree_state",
    }
    assert "git_common_dir_realpath" not in private


def test_linked_worktrees_are_distinct_instances(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    sibling = tmp_path / "sibling"
    _git(repo, "worktree", "add", "-qb", "sibling", str(sibling))
    try:
        primary = _freeze(_scope(), {"main": {"path": str(repo)}})
        linked = _freeze(_scope(), {"main": {"path": str(sibling)}})

        assert primary.targets_digest != linked.targets_digest
        assert _actual_binding(primary).instance_key != _actual_binding(linked).instance_key
        assert _actual_binding(primary).workcopy_role == "primary"
        assert _actual_binding(linked).workcopy_role == "declared-linked"
    finally:
        _git(repo, "worktree", "remove", "--force", str(sibling))


def test_two_linked_worktree_helpers_match_freeze_and_remain_distinct(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")
    siblings = (tmp_path / "sibling-a", tmp_path / "sibling-b")
    _git(repo, "worktree", "add", "-qb", "sibling-a", str(siblings[0]))
    _git(repo, "worktree", "add", "-qb", "sibling-b", str(siblings[1]))
    try:
        helper_identities = [
            execution_scope.derive_actual_instance_identity(str(sibling), target_key="main")
            for sibling in siblings
        ]
        bindings = [
            _actual_binding(_freeze(_scope(), {"main": {"path": str(sibling)}}))
            for sibling in siblings
        ]

        assert [identity.instance_key for identity in helper_identities] == [
            binding.instance_key for binding in bindings
        ]
        assert len({identity.instance_key for identity in helper_identities}) == 2
        assert all(
            identity.git_common_dir_realpath == str(repo / ".git") for identity in helper_identities
        )
    finally:
        for sibling in reversed(siblings):
            _git(repo, "worktree", "remove", "--force", str(sibling))


def test_separate_git_directory_remains_a_supported_actual_identity(
    tmp_path: Path,
) -> None:
    workcopy = tmp_path / "workcopy"
    git_directory = tmp_path / "metadata.git"
    _git(
        tmp_path,
        "init",
        "-q",
        f"--separate-git-dir={git_directory}",
        str(workcopy),
    )
    (workcopy / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(workcopy, "add", "tracked.txt")
    _git(
        workcopy,
        "-c",
        "user.email=scope@example.com",
        "-c",
        "user.name=Scope Test",
        "commit",
        "-qm",
        "initial",
    )

    identity = execution_scope.derive_actual_instance_identity(str(workcopy), target_key="main")
    binding = _actual_binding(_freeze(_scope(), {"main": {"path": str(workcopy)}}))

    assert identity.instance_key == binding.instance_key
    assert identity.git_common_dir_realpath == str(git_directory)
    assert binding.workcopy_role == "declared-linked"


def test_sibling_submodules_are_refused_by_freeze_and_actual_identity_helper(
    tmp_path: Path,
) -> None:
    module_origin = _repository_with_command_scoped_identity(tmp_path / "module-origin")
    host = _repository_with_command_scoped_identity(tmp_path / "host")
    submodules = (host / "sub-a", host / "sub-b")
    for submodule in submodules:
        _git(
            host,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(module_origin),
            submodule.name,
        )
    _git(host, "add", ".gitmodules", "sub-a", "sub-b")
    _git(
        host,
        "-c",
        "user.email=scope@example.com",
        "-c",
        "user.name=Scope Test",
        "commit",
        "-qm",
        "add sibling submodules",
    )

    for submodule in submodules:
        submodule_commit = _git(submodule, "rev-parse", "HEAD")

        def freeze_submodule(path: Path = submodule) -> object:
            return _freeze(_scope(), {"main": {"path": str(path)}})

        def derive_submodule(path: Path = submodule) -> object:
            return execution_scope.derive_actual_instance_identity(str(path), target_key="main")

        invocations: tuple[Callable[[], object], ...] = (
            freeze_submodule,
            derive_submodule,
        )
        for invoke in invocations:
            caught_error: execution_scope.TargetResolutionError | None = None
            try:
                invoke()
            except execution_scope.TargetResolutionError as error:
                caught_error = error
                rendered = traceback.format_exc()
            else:
                pytest.fail("submodule was accepted as an ordinary Git working copy")

            assert caught_error is not None
            assert caught_error.code == "target_unsupported_submodule"
            assert str(host) not in rendered
            assert str(submodule) not in rendered
            assert submodule_commit not in rendered


def test_linked_submodule_worktrees_are_refused_before_identity_or_child_planning(
    tmp_path: Path,
) -> None:
    module_origin = _repository_with_command_scoped_identity(tmp_path / "module-origin")
    host = _repository_with_command_scoped_identity(tmp_path / "host")
    submodules = (host / "sub-a", host / "sub-b")
    linked_worktrees = (tmp_path / "linked-a", tmp_path / "linked-b")
    for submodule in submodules:
        _git(
            host,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(module_origin),
            submodule.name,
        )
    _git(host, "add", ".gitmodules", "sub-a", "sub-b")
    _git(
        host,
        "-c",
        "user.email=scope@example.com",
        "-c",
        "user.name=Scope Test",
        "commit",
        "-qm",
        "add sibling submodules",
    )
    for submodule, linked in zip(submodules, linked_worktrees):
        _git(
            submodule,
            "worktree",
            "add",
            "-qb",
            f"linked-{submodule.name}",
            str(linked),
        )

    child_source = _scope(
        targets=(
            '"main": {"agents": ["d"], "memory": "off"},'
            '"child": {"agents": ["d"], "memory": "off",'
            ' "worktree": "ephemeral-child-of:main"}'
        )
    )
    try:
        for submodule, linked in zip(submodules, linked_worktrees):
            linked_commit = _git(linked, "rev-parse", "HEAD")
            assert _git(linked, "rev-parse", "--show-superproject-working-tree") == ""
            assert "/.git/modules/" in str(
                Path(_git(linked, "rev-parse", "--git-common-dir")).resolve()
            )

            def derive_linked(path: Path = linked) -> object:
                return execution_scope.derive_actual_instance_identity(str(path), target_key="main")

            def freeze_linked(path: Path = linked) -> object:
                return _freeze(_scope(), {"main": {"path": str(path)}})

            def plan_child(path: Path = linked) -> object:
                return _freeze(
                    child_source,
                    {
                        "main": {"path": str(path)},
                        "child": {"parent": "main", "baseline": "HEAD"},
                    },
                )

            for invoke in (derive_linked, freeze_linked, plan_child):
                caught_error: execution_scope.TargetResolutionError | None = None
                try:
                    invoke()
                except execution_scope.TargetResolutionError as error:
                    caught_error = error
                    rendered = traceback.format_exc()
                else:
                    pytest.fail("linked submodule worktree was accepted")

                assert caught_error is not None
                assert caught_error.code == "target_unsupported_submodule"
                assert str(host) not in rendered
                assert str(submodule) not in rendered
                assert str(linked) not in rendered
                assert linked_commit not in rendered
    finally:
        for submodule, linked in reversed(tuple(zip(submodules, linked_worktrees))):
            _git(submodule, "worktree", "remove", "--force", str(linked))


def test_lookalike_non_submodule_common_directory_remains_supported(
    tmp_path: Path,
) -> None:
    workcopy = tmp_path / "workcopy"
    git_directory = tmp_path / "not.git" / "modules" / "repo.git"
    git_directory.parent.mkdir(parents=True)
    _git(
        tmp_path,
        "init",
        "-q",
        f"--separate-git-dir={git_directory}",
        str(workcopy),
    )
    (workcopy / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(workcopy, "add", "tracked.txt")
    _git(
        workcopy,
        "-c",
        "user.email=scope@example.com",
        "-c",
        "user.name=Scope Test",
        "commit",
        "-qm",
        "initial",
    )

    identity = execution_scope.derive_actual_instance_identity(str(workcopy), target_key="main")
    binding = _actual_binding(_freeze(_scope(), {"main": {"path": str(workcopy)}}))

    assert identity.instance_key == binding.instance_key
    assert identity.git_common_dir_realpath == str(git_directory)
    assert binding.workcopy_role == "declared-linked"


def test_symlink_and_filesystem_case_aliases_share_one_instance_key(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "CaseRepo")
    symlink = tmp_path / "repo-link"
    symlink.symlink_to(repo, target_is_directory=True)

    direct = _freeze(_scope(), {"main": {"path": str(repo)}})
    through_symlink = _freeze(_scope(), {"main": {"path": str(symlink)}})
    assert _actual_binding(direct).instance_key == _actual_binding(through_symlink).instance_key
    assert (
        execution_scope.derive_actual_instance_identity(str(repo), target_key="main").instance_key
        == execution_scope.derive_actual_instance_identity(
            str(symlink), target_key="main"
        ).instance_key
    )

    case_alias = Path(str(repo).swapcase())
    if not case_alias.exists():
        pytest.skip("filesystem is case-sensitive")
    through_case_alias = _freeze(_scope(), {"main": {"path": str(case_alias)}})
    assert _actual_binding(direct).instance_key == _actual_binding(through_case_alias).instance_key


def test_unicode_case_and_normalization_samefile_aliases_share_instance_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "Ünicode")
    aliases = (
        tmp_path / "ünicode",
        tmp_path / "U\u0308nicode",
    )
    if not all(alias.exists() and os.path.samefile(repo, alias) for alias in aliases):
        pytest.skip("filesystem does not coalesce Unicode case and normalization aliases")

    actual_run_git = execution_scope._run_git

    def preserve_samefile_spelling(
        cwd: str,
        args: tuple[str, ...],
        *,
        target_key: str,
        nonzero_code: str,
    ) -> str:
        if args == ("rev-parse", "--show-toplevel"):
            return cwd
        return actual_run_git(
            cwd,
            args,
            target_key=target_key,
            nonzero_code=nonzero_code,
        )

    monkeypatch.setattr(execution_scope, "_run_git", preserve_samefile_spelling)
    bindings = [
        _actual_binding(_freeze(_scope(), {"main": {"path": str(path)}}))
        for path in (repo, *aliases)
    ]
    helper_keys = {
        execution_scope.derive_actual_instance_identity(str(path), target_key="main").instance_key
        for path in (repo, *aliases)
    }

    assert len({binding.instance_key for binding in bindings}) == 1
    assert len(helper_keys) == 1
    assert len({binding.target_digest for binding in bindings}) == 3


def test_alias_target_names_do_not_change_instance_key(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    first = _freeze(
        _scope(targets='"alpha": {"agents": ["developer"], "memory": "off"}'),
        {"alpha": str(repo)},
    )
    second = _freeze(
        _scope(targets='"beta": {"agents": ["developer"], "memory": "off"}'),
        {"beta": {"path": str(repo)}},
    )

    assert _actual_binding(first).key == "alpha"
    assert _actual_binding(first).instance_key == _actual_binding(second).instance_key


def test_server_cwd_drift_does_not_change_target_material(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repository(tmp_path / "repo")
    server_a = tmp_path / "server-a"
    server_b = tmp_path / "server-b"
    server_a.mkdir()
    server_b.mkdir()

    monkeypatch.chdir(server_a)
    first = _freeze(_scope(), {"main": {"path": str(repo)}})
    monkeypatch.chdir(server_b)
    second = _freeze(_scope(), {"main": {"path": str(repo)}})

    assert first.private_material_bytes == second.private_material_bytes
    assert first.targets_digest == second.targets_digest


def test_requested_subdirectory_is_privately_bound(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    subdirectory = repo / "nested"
    subdirectory.mkdir()

    root = _freeze(_scope(), {"main": {"path": str(repo)}})
    nested = _freeze(_scope(), {"main": {"path": str(subdirectory)}})

    assert _actual_binding(root).instance_key == _actual_binding(nested).instance_key
    assert root.targets_digest != nested.targets_digest
    assert str(subdirectory).encode() in nested.private_material_bytes
    assert _actual_binding(nested).requested_subdirectory == "nested"

    (repo / "tracked.txt").write_text("changed outside nested\n", encoding="utf-8")
    nested_after_root_dirt = _freeze(_scope(), {"main": {"path": str(subdirectory)}})
    assert nested_after_root_dirt.targets_digest != nested.targets_digest


def test_public_summary_and_errors_never_contain_private_paths(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "sensitive-customer-name")
    frozen = _freeze(_scope(), {"main": {"path": str(repo)}})

    summary = frozen.public_summary()
    rendered = json.dumps(summary, sort_keys=True)

    assert summary["schema"] == "execution-scope-public-v2"
    assert summary["targets"][0]["key"] == "main"
    assert set(summary["targets"][0]) == {
        "instance_key",
        "key",
        "kind",
        "role",
        "target_digest",
    }
    assert summary["targets"][0]["kind"] == "git-working-copy"
    assert str(repo) not in rendered
    assert str(repo) in frozen.private_material_bytes.decode("utf-8")
    assert frozen.targets_digest == sha256(frozen.targets_material_bytes).hexdigest()
    assert (
        frozen.bindings[0].target_digest
        == sha256(frozen.bindings[0].private_material_bytes).hexdigest()
    )
    assert json.loads(frozen.bindings[0].private_material_bytes)["schema"] == (
        "execution-target-private-v1"
    )
    assert json.loads(frozen.targets_material_bytes)["schema"] == ("execution-targets-private-v1")
    assert json.loads(frozen.private_material_bytes)["schema"] == ("execution-scope-private-v1")


def test_non_ascii_private_target_material_round_trips_through_snapshot_decoder(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "项目")

    frozen = _freeze(_scope(), {"main": {"path": str(repo)}})
    decoded = private_plan_snapshot.decode_material("targets", frozen.targets_material_bytes)

    assert decoded["targets"][0]["requested_working_directory"] == str(repo)
    assert b"\\u9879\\u76ee" in frozen.targets_material_bytes
    assert "项目".encode("utf-8") not in frozen.targets_material_bytes


def test_incomplete_baseline_probe_refuses_instead_of_hashing_partial_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repository(tmp_path / "repo")
    monkeypatch.setattr(
        execution_scope,
        "derive_baseline",
        lambda _cwd: {
            "available": False,
            "commit": "a" * 40,
            "worktree_state": {"status": "unavailable"},
        },
    )

    with pytest.raises(execution_scope.TargetResolutionError) as caught:
        _freeze(_scope(), {"main": {"path": str(repo)}})

    assert caught.value.code == "target_baseline_incomplete"
    assert str(repo) not in str(caught.value)


@pytest.mark.parametrize(
    ("run_result", "code"),
    [
        (subprocess.TimeoutExpired(cmd=["git"], timeout=1), "target_git_probe_incomplete"),
        (
            SimpleNamespace(returncode=0, stdout=b"x" * (16 * 1024 + 1)),
            "target_git_probe_truncated",
        ),
    ],
)
def test_timeout_or_oversized_git_probe_refuses_complete_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run_result: object,
    code: str,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(
        execution_scope,
        "derive_baseline",
        lambda _cwd: {
            "available": True,
            "commit": "a" * 40,
            "worktree_state": {"status": "clean"},
        },
    )

    def probe(*_args: object, **_kwargs: object) -> object:
        if isinstance(run_result, BaseException):
            raise run_result
        return run_result

    monkeypatch.setattr(execution_scope.subprocess, "run", probe)

    with pytest.raises(execution_scope.TargetResolutionError) as caught:
        _freeze(_scope(), {"main": {"path": str(target)}})

    assert caught.value.code == code


def test_ephemeral_child_binds_parent_provenance_and_resolved_baseline_only(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")
    source = _scope(
        targets=(
            '"main": {"agents": ["developer"], "memory": "exact-snapshot"},'
            '"sandbox": {"agents": ["developer"], "memory": "off",'
            ' "worktree": "ephemeral-child-of:main"}'
        )
    )
    mappings = {
        "main": {"path": str(repo)},
        "sandbox": {"parent": "main", "baseline": "HEAD"},
    }

    first = _freeze(source, mappings)
    second = _freeze(source, mappings)
    child = next(binding for binding in first.bindings if binding.key == "sandbox")

    assert first.targets_digest == second.targets_digest
    assert isinstance(child, execution_scope.PlannedChildBinding)
    assert child.parent_key == "main"
    assert child.intended_baseline_commit == _git(repo, "rev-parse", "HEAD")
    assert "random-terminal-id" not in first.private_material_bytes.decode("utf-8")

    (repo / "tracked.txt").write_text("second\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "new baseline")
    changed = _freeze(source, mappings)
    assert changed.targets_digest != first.targets_digest


def test_ephemeral_mapping_parent_must_match_the_declared_parent(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    source = _scope(
        targets=(
            '"main": {"agents": ["d"], "memory": "off"},'
            '"other": {"agents": ["d"], "memory": "off"},'
            '"sandbox": {"agents": ["d"], "memory": "off",'
            ' "worktree": "ephemeral-child-of:main"}'
        )
    )

    with pytest.raises(execution_scope.TargetResolutionError) as caught:
        _freeze(
            source,
            {
                "main": {"path": str(repo)},
                "other": {"path": str(repo)},
                "sandbox": {"parent": "other", "baseline": "HEAD"},
            },
        )

    assert caught.value.code == "target_parent_ambiguous"
    assert caught.value.target_key == "sandbox"


def _planned_children_source() -> str:
    return _scope(
        targets=(
            '"main": {"agents": ["developer"], "memory": "exact-snapshot"},'
            '"child-a": {"agents": ["developer"], "memory": "off",'
            ' "worktree": "ephemeral-child-of:main"},'
            '"child-b": {"agents": ["developer"], "memory": "off",'
            ' "worktree": "ephemeral-child-of:main"}'
        )
    )


def test_planned_children_have_disjoint_exact_material_and_no_actual_instance(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")
    frozen = _freeze(
        _planned_children_source(),
        {
            "main": {"path": str(repo)},
            "child-a": {"parent": "main", "baseline": "HEAD"},
            "child-b": {"parent": "main", "baseline": "HEAD"},
        },
    )
    children = [
        binding
        for binding in frozen.bindings
        if isinstance(binding, execution_scope.PlannedChildBinding)
    ]

    assert len(children) == 2
    assert len({child.provenance_key for child in children}) == 2
    assert len({child.target_digest for child in children}) == 2
    forbidden = {
        "commit",
        "dirty_digest",
        "worktree_state",
        "instance_key",
        "filesystem_identity",
        "repo_realpath",
        "workcopy_realpath",
        "workcopy_rel",
        "workcopy_role",
        "requested_working_directory",
        "requested_realpath",
        "requested_subdirectory",
    }
    expected = {
        "intended_baseline_commit",
        "intended_baseline_ref",
        "key",
        "kind",
        "parent_instance_key",
        "parent_key",
        "parent_target_digest",
        "provenance_key",
        "schema",
    }
    for child in children:
        private = json.loads(child.private_material_bytes)
        assert set(private) == expected
        assert not forbidden.intersection(private)
        assert private["kind"] == "git-ephemeral-child"
        assert private["schema"] == "execution-target-ephemeral-child-private-v1"
        assert all(not hasattr(child, field) for field in forbidden)


def test_planned_child_public_summary_is_discriminated_and_path_free(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "private-parent")
    frozen = _freeze(
        _planned_children_source(),
        {
            "main": {"path": str(repo)},
            "child-a": {"parent": "main", "baseline": "HEAD"},
            "child-b": {"parent": "main", "baseline": "HEAD"},
        },
    )

    summary = frozen.public_summary()
    child_rows = [row for row in summary["targets"] if row["kind"] == "git-ephemeral-child"]

    assert summary["schema"] == "execution-scope-public-v2"
    assert len(child_rows) == 2
    assert all(
        set(row)
        == {
            "key",
            "kind",
            "parent_key",
            "provenance_key",
            "role",
            "target_digest",
        }
        for row in child_rows
    )
    assert all("instance_key" not in row for row in child_rows)
    assert all(row["role"] == "ephemeral-child" for row in child_rows)
    assert str(repo) not in json.dumps(summary, sort_keys=True)


def test_planned_child_digest_changes_with_parent_clone_and_parent_head(
    tmp_path: Path,
) -> None:
    first = _repository(tmp_path / "first")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(first), str(clone))
    source = _scope(
        targets=(
            '"main": {"agents": ["d"], "memory": "off"},'
            '"child": {"agents": ["d"], "memory": "off",'
            ' "worktree": "ephemeral-child-of:main"}'
        )
    )

    first_child = _freeze(
        source,
        {
            "main": {"path": str(first)},
            "child": {"parent": "main", "baseline": "HEAD"},
        },
    ).bindings[0]
    clone_child = _freeze(
        source,
        {
            "main": {"path": str(clone)},
            "child": {"parent": "main", "baseline": "HEAD"},
        },
    ).bindings[0]
    (first / "tracked.txt").write_text("advanced\n", encoding="utf-8")
    _git(first, "add", "tracked.txt")
    _git(first, "commit", "-qm", "advance")
    advanced_child = _freeze(
        source,
        {
            "main": {"path": str(first)},
            "child": {"parent": "main", "baseline": "HEAD"},
        },
    ).bindings[0]

    assert isinstance(first_child, execution_scope.PlannedChildBinding)
    assert isinstance(clone_child, execution_scope.PlannedChildBinding)
    assert isinstance(advanced_child, execution_scope.PlannedChildBinding)
    assert (
        len(
            {
                first_child.target_digest,
                clone_child.target_digest,
                advanced_child.target_digest,
            }
        )
        == 3
    )
    assert first_child.provenance_key != clone_child.provenance_key
    assert first_child.provenance_key != advanced_child.provenance_key


def test_two_refs_at_same_commit_produce_distinct_child_target_digests(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")
    _git(repo, "tag", "same-commit")
    source = _scope(
        targets=(
            '"main": {"agents": ["d"], "memory": "off"},'
            '"child": {"agents": ["d"], "memory": "off",'
            ' "worktree": "ephemeral-child-of:main"}'
        )
    )

    from_head = _freeze(
        source,
        {
            "main": {"path": str(repo)},
            "child": {"parent": "main", "baseline": "HEAD"},
        },
    ).bindings[0]
    from_tag = _freeze(
        source,
        {
            "main": {"path": str(repo)},
            "child": {"parent": "main", "baseline": "same-commit"},
        },
    ).bindings[0]

    assert isinstance(from_head, execution_scope.PlannedChildBinding)
    assert isinstance(from_tag, execution_scope.PlannedChildBinding)
    assert from_head.intended_baseline_commit == from_tag.intended_baseline_commit
    assert from_head.provenance_key == from_tag.provenance_key
    assert from_head.target_digest != from_tag.target_digest


def test_planned_child_freeze_is_deterministic_and_creates_no_worktree(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")
    source = _scope(
        targets=(
            '"main": {"agents": ["d"], "memory": "off"},'
            '"child": {"agents": ["d"], "memory": "off",'
            ' "worktree": "ephemeral-child-of:main"}'
        )
    )
    mappings = {
        "main": {"path": str(repo)},
        "child": {"parent": "main", "baseline": "HEAD"},
    }
    before = _git(repo, "worktree", "list", "--porcelain")

    first = _freeze(source, mappings)
    second = _freeze(source, mappings)

    assert first.private_material_bytes == second.private_material_bytes
    assert _git(repo, "worktree", "list", "--porcelain") == before
    assert not (repo / ".worktrees").exists()


def test_planned_child_resolves_when_declaration_sorts_before_parent(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")
    source = _scope(
        targets=(
            '"z-parent": {"agents": ["d"], "memory": "off"},'
            '"a-child": {"agents": ["d"], "memory": "off",'
            ' "worktree": "ephemeral-child-of:z-parent"}'
        )
    )

    frozen = _freeze(
        source,
        {
            "z-parent": {"path": str(repo)},
            "a-child": {"parent": "z-parent", "baseline": "HEAD"},
        },
    )

    assert isinstance(frozen.bindings[0], execution_scope.PlannedChildBinding)
    assert frozen.bindings[0].parent_key == "z-parent"


def test_planned_child_cannot_be_the_actual_parent_of_another_child(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")
    source = _scope(
        targets=(
            '"main": {"agents": ["d"], "memory": "off"},'
            '"child": {"agents": ["d"], "memory": "off",'
            ' "worktree": "ephemeral-child-of:main"},'
            '"grandchild": {"agents": ["d"], "memory": "off",'
            ' "worktree": "ephemeral-child-of:child"}'
        )
    )

    with pytest.raises(execution_scope.TargetResolutionError) as caught:
        _freeze(
            source,
            {
                "main": {"path": str(repo)},
                "child": {"parent": "main", "baseline": "HEAD"},
                "grandchild": {"parent": "child", "baseline": "HEAD"},
            },
        )

    assert caught.value.code == "target_parent_not_actual"


def test_malformed_resolved_child_option_is_explicitly_refused_without_assert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repository(tmp_path / "repo")
    declaration = execution_scope.parse_scope_declaration(
        _scope(
            targets=(
                '"main": {"agents": ["d"], "memory": "off"},'
                '"child": {"agents": ["d"], "memory": "off",'
                ' "worktree": "ephemeral-child-of:main"}'
            )
        )
    )
    monkeypatch.setattr(
        execution_scope,
        "resolve_target_mappings",
        lambda _declaration, _mappings: (
            ("child", object()),
            ("main", execution_scope.GitWorkingCopyOption(str(repo))),
        ),
    )

    with pytest.raises(execution_scope.TargetResolutionError) as caught:
        execution_scope.freeze_execution_scope(declaration, object())

    assert caught.value.code == "target_mapping_kind_invalid"
    assert caught.value.target_key == "child"


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"])
def test_git_redirecting_environment_is_refused_before_target_inspection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str
) -> None:
    intended = _repository(tmp_path / "intended")
    foreign = _repository(tmp_path / "foreign")
    foreign_commit = _git(foreign, "rev-parse", "HEAD")
    value = {
        "GIT_DIR": str(foreign / ".git"),
        "GIT_WORK_TREE": str(foreign),
        "GIT_INDEX_FILE": str(foreign / ".git" / "index"),
    }[variable]
    monkeypatch.setenv(variable, value)

    with pytest.raises(execution_scope.TargetResolutionError) as caught:
        _freeze(_scope(), {"main": {"path": str(intended)}})
    with pytest.raises(execution_scope.TargetResolutionError) as direct_caught:
        execution_scope.derive_actual_instance_identity(str(intended), target_key="main")

    assert caught.value.code == "target_git_environment_unsafe"
    assert direct_caught.value.code == "target_git_environment_unsafe"
    assert foreign_commit not in str(caught.value)
    assert value not in str(caught.value)
    assert value not in str(direct_caught.value)


@pytest.mark.parametrize(
    "variable",
    [
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_COUNT",
    ],
)
def test_git_config_environment_has_distinct_safe_diagnosis_without_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str
) -> None:
    repo = _repository(tmp_path / "repo")
    value = "1" if variable in {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_COUNT"} else "private"
    monkeypatch.setenv(variable, value)

    invocations: tuple[Callable[[], object], ...] = (
        lambda: _freeze(_scope(), {"main": {"path": str(repo)}}),
        lambda: execution_scope.derive_actual_instance_identity(str(repo), target_key="main"),
    )
    for invoke in invocations:
        with pytest.raises(execution_scope.TargetResolutionError) as caught:
            invoke()

        assert caught.value.code == "target_git_config_environment_unsafe"
        assert value not in str(caught.value)
        assert os.environ[variable] == value


def test_run_git_filters_every_unsafe_environment_variable_without_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unsafe = {
        "GIT_DIR": "redirect",
        "GIT_WORK_TREE": "redirect",
        "GIT_INDEX_FILE": "redirect",
        "GIT_CONFIG": "private",
        "GIT_CONFIG_GLOBAL": "private",
        "GIT_CONFIG_SYSTEM": "private",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
    }
    for key, value in unsafe.items():
        monkeypatch.setenv(key, value)
    captured_environment: dict[str, str] = {}

    def capture_environment(*_args: object, **kwargs: object) -> object:
        captured_environment.update(cast(dict[str, str], kwargs["env"]))
        return SimpleNamespace(returncode=0, stdout=b"ok\n")

    monkeypatch.setattr(execution_scope.subprocess, "run", capture_environment)

    assert (
        execution_scope._run_git(
            str(tmp_path),
            ("rev-parse", "--show-toplevel"),
            target_key="main",
            nonzero_code="target_not_git",
        )
        == "ok"
    )
    assert not unsafe.keys() & captured_environment.keys()
    assert all(os.environ[key] == value for key, value in unsafe.items())


def test_git_environment_change_during_freeze_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repository(tmp_path / "repo")
    actual_derive = execution_scope.derive_baseline
    calls = 0

    def mutate_environment(cwd: str) -> object:
        nonlocal calls
        result = actual_derive(cwd)
        calls += 1
        if calls == 1:
            monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
        return result

    monkeypatch.setattr(execution_scope, "derive_baseline", mutate_environment)
    with pytest.raises(execution_scope.TargetResolutionError) as caught:
        _freeze(_scope(), {"main": {"path": str(repo)}})

    assert caught.value.code == "target_git_environment_unsafe"


def test_git_path_output_preserves_trailing_space_checkout_identity(tmp_path: Path) -> None:
    ordinary = _repository(tmp_path / "repo")
    trailing = _repository(tmp_path / "repo ")
    (trailing / "tracked.txt").write_text("different\n", encoding="utf-8")
    _git(trailing, "add", "tracked.txt")
    _git(trailing, "commit", "-qm", "distinct")

    ordinary_binding = _actual_binding(_freeze(_scope(), {"main": {"path": str(ordinary)}}))
    trailing_binding = _actual_binding(_freeze(_scope(), {"main": {"path": str(trailing)}}))

    assert trailing_binding.commit == _git(trailing, "rev-parse", "HEAD")
    assert trailing_binding.commit != ordinary_binding.commit
    assert trailing_binding.instance_key != ordinary_binding.instance_key


def test_requested_prefix_must_resolve_to_the_requested_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repository(tmp_path / "repo")
    nested = repo / "nested"
    nested.mkdir()
    actual_run_git = execution_scope._run_git

    def wrong_prefix(
        cwd: str,
        args: tuple[str, ...],
        *,
        target_key: str,
        nonzero_code: str,
    ) -> str:
        if args == ("rev-parse", "--show-prefix"):
            return "other/"
        return actual_run_git(cwd, args, target_key=target_key, nonzero_code=nonzero_code)

    monkeypatch.setattr(execution_scope, "_run_git", wrong_prefix)
    with pytest.raises(execution_scope.TargetResolutionError) as caught:
        _freeze(_scope(), {"main": {"path": str(nested)}})

    assert caught.value.code == "target_requested_path_mismatch"


@pytest.mark.parametrize(
    "binder",
    [
        "import evil as SCOPE",
        "from evil import SCOPE",
        "from evil import *",
        "try:\n    raise RuntimeError()\nexcept RuntimeError as SCOPE:\n    pass",
        "match {'x': 1}:\n    case {'x': SCOPE}:\n        pass",
        "globals()['SCOPE'] = {}",
        "vars()['SCOPE'] = {}",
        "exec('SCOPE = {}')",
        "eval(\"globals().__setitem__('SCOPE', {})\")",
        "def SCOPE():\n    pass",
        "class SCOPE:\n    pass",
    ],
)
def test_scope_parser_refuses_other_module_scope_binders(binder: str) -> None:
    with pytest.raises(execution_scope.ScopeDeclarationError) as caught:
        execution_scope.parse_scope_declaration(_scope() + binder + "\n")

    assert caught.value.code == "scope_dynamic_assignment"


@pytest.mark.parametrize("failure_kind", ["syntax", "git", "stat"])
def test_safe_errors_suppress_secret_bearing_exception_chains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_kind: str
) -> None:
    secret = str(tmp_path / "PRIVATE-CUSTOMER-SECRET")
    invoke: Callable[[], object]
    if failure_kind == "syntax":
        invoke = lambda: execution_scope.parse_scope_declaration(f"SCOPE = {{\n{secret!r}\n")
    else:
        repo = _repository(tmp_path / "repo")
        if failure_kind == "git":
            monkeypatch.setattr(
                execution_scope.subprocess,
                "run",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(secret)),
            )
        else:
            monkeypatch.setattr(
                execution_scope,
                "_filesystem_identity",
                lambda _path: (_ for _ in ()).throw(OSError(secret)),
            )
        invoke = lambda: _freeze(_scope(), {"main": {"path": str(repo)}})

    try:
        invoke()
    except (execution_scope.ScopeDeclarationError, execution_scope.TargetResolutionError):
        rendered = traceback.format_exc()
    else:
        pytest.fail("safe typed error was not raised")

    assert secret not in rendered


def test_physical_identity_change_between_baseline_reads_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repository(tmp_path / "repo")
    actual_identity = execution_scope._filesystem_identity
    calls = 0

    def changing_identity(path: str) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        device, inode = actual_identity(path)
        return (device, inode + 1) if calls > 2 else (device, inode)

    monkeypatch.setattr(execution_scope, "_filesystem_identity", changing_identity)
    with pytest.raises(execution_scope.TargetResolutionError) as caught:
        _freeze(_scope(), {"main": {"path": str(repo)}})

    assert caught.value.code == "target_binding_unstable"


def test_requested_symlink_swap_between_baseline_reads_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = _repository(tmp_path / "first")
    second = _repository(tmp_path / "second")
    link = tmp_path / "target"
    link.symlink_to(first, target_is_directory=True)
    actual_derive = execution_scope.derive_baseline
    calls = 0

    def swap_after_first_read(cwd: str) -> object:
        nonlocal calls
        result = actual_derive(cwd)
        calls += 1
        if calls == 1:
            link.unlink()
            link.symlink_to(second, target_is_directory=True)
        return result

    monkeypatch.setattr(execution_scope, "derive_baseline", swap_after_first_read)
    with pytest.raises(execution_scope.TargetResolutionError) as caught:
        _freeze(_scope(), {"main": {"path": str(link)}})

    assert caught.value.code == "target_binding_unstable"


def _agent_scope() -> execution_scope.ExecutionScope:
    return execution_scope.parse_scope_declaration(
        _scope(
            targets=(
                '"a": {"agents": ["reviewer", "developer"], "memory": "off"},'
                '"b": {"agents": ["developer"], "memory": "off"}'
            )
        )
    )


def test_agent_bindings_resolve_all_forms_in_stable_pair_order() -> None:
    resolved = execution_scope.resolve_agent_bindings(
        _agent_scope(),
        {
            "b": {"developer": {"model": "model-b", "allowed_tools": []}},
            "a": {
                "reviewer": "codex",
                "developer": {
                    "provider": "claude-code",
                    "allowed_tools": ["Read", "mcp__server__tool"],
                },
            },
        },
    )

    assert resolved == (
        execution_scope.AgentBinding(
            "a", "developer", "claude-code", None, ("Read", "mcp__server__tool")
        ),
        execution_scope.AgentBinding("a", "reviewer", "codex", None, None),
        execution_scope.AgentBinding("b", "developer", None, "model-b", ()),
    )


def test_agent_binding_surface_equivalences_and_none_empty_tools_distinction() -> None:
    declaration = execution_scope.parse_scope_declaration(
        _scope(targets='"main": {"agents": ["developer"], "memory": "off"}')
    )
    bare = execution_scope.resolve_agent_bindings(declaration, {"main": {"developer": "codex"}})
    provider_dict = execution_scope.resolve_agent_bindings(
        declaration, {"main": {"developer": {"provider": "codex"}}}
    )
    inherited_null = execution_scope.resolve_agent_bindings(
        declaration, {"main": {"developer": None}}
    )
    inherited_dict = execution_scope.resolve_agent_bindings(
        declaration, {"main": {"developer": {}}}
    )
    empty_tools = execution_scope.resolve_agent_bindings(
        declaration, {"main": {"developer": {"allowed_tools": []}}}
    )
    tuple_tools = execution_scope.resolve_agent_bindings(
        declaration,
        {
            "main": {
                "developer": {
                    "provider": "syntax-only-provider",
                    "allowed_tools": ("Read",),
                }
            }
        },
    )
    list_tools = execution_scope.resolve_agent_bindings(
        declaration,
        {
            "main": {
                "developer": {
                    "provider": "syntax-only-provider",
                    "allowed_tools": ["Read"],
                }
            }
        },
    )

    assert bare == provider_dict
    assert inherited_null == inherited_dict
    assert inherited_null[0].declared_allowed_tools is None
    assert empty_tools[0].declared_allowed_tools == ()
    assert inherited_null != empty_tools
    assert tuple_tools == list_tools


@pytest.mark.parametrize(
    ("bindings", "code"),
    [
        ([], "agent_bindings_invalid"),
        ({"a": []}, "agent_binding_type_invalid"),
        (
            {"a": {"developer": None, "reviewer": None}},
            "agent_binding_target_missing",
        ),
        (
            {"a": {"developer": None}, "b": {"developer": None}},
            "agent_binding_agent_missing",
        ),
        (
            {
                "a": {"developer": None, "reviewer": None},
                "b": {"developer": None},
                "unsafe/target": {"developer": None},
            },
            "agent_binding_target_unknown",
        ),
        (
            {
                "a": {
                    "developer": None,
                    "reviewer": None,
                    "unsafe/profile": None,
                },
                "b": {"developer": None},
            },
            "agent_binding_agent_unknown",
        ),
        (
            {
                "a": {"developer": {"engine": "v2"}, "reviewer": None},
                "b": {"developer": None},
            },
            "agent_binding_shape_invalid",
        ),
        (
            {
                "a": {"developer": "unsafe/provider", "reviewer": None},
                "b": {"developer": None},
            },
            "agent_binding_provider_invalid",
        ),
        (
            {
                "a": {"developer": {"model": ""}, "reviewer": None},
                "b": {"developer": None},
            },
            "agent_binding_model_invalid",
        ),
        (
            {
                "a": {"developer": {"allowed_tools": "Read"}, "reviewer": None},
                "b": {"developer": None},
            },
            "agent_binding_tools_invalid",
        ),
        (
            {
                "a": {"developer": {"allowed_tools": [""]}, "reviewer": None},
                "b": {"developer": None},
            },
            "agent_binding_tools_invalid",
        ),
    ],
)
def test_agent_binding_refusals_are_typed_and_value_safe(bindings: object, code: str) -> None:
    with pytest.raises(execution_scope.AgentBindingError) as caught:
        execution_scope.resolve_agent_bindings(_agent_scope(), bindings)

    assert caught.value.status_code == 422
    assert caught.value.retryable is False
    assert caught.value.code == code
    for unsafe_value in ("unsafe/provider", "unsafe/target", "unsafe/profile"):
        assert unsafe_value not in str(caught.value)
