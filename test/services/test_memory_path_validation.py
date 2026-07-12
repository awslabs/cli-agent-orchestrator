"""Path-component confinement for the memory wiki filesystem layout.

``MemoryService`` composes on-disk paths out of user/context-derived
segments (``scope``, ``scope_id``, ``key``). These tests lock in that every
such segment is validated as a single safe path component and that any
traversal attempt raises ``ValueError`` rather than escaping the memory base
directory. Closes the CodeQL ``py/path-injection`` alerts in
``memory_service.py``.
"""

import os
from pathlib import Path

import pytest

from cli_agent_orchestrator.services.memory_service import MemoryService


@pytest.fixture
def svc(tmp_path: Path) -> MemoryService:
    return MemoryService(base_dir=tmp_path)


class TestGetWikiPathValid:
    def test_global_scope(self, svc, tmp_path):
        p = svc.get_wiki_path("global", None, "my-topic")
        assert p == Path(
            os.path.realpath(str(tmp_path / "global" / "wiki" / "global" / "my-topic.md"))
        )

    def test_project_scope_with_scope_id(self, svc, tmp_path):
        p = svc.get_wiki_path("project", "proj_a", "topic")
        assert p == Path(
            os.path.realpath(str(tmp_path / "proj_a" / "wiki" / "project" / "topic.md"))
        )

    def test_session_scope_nests_scope_id(self, svc, tmp_path):
        p = svc.get_wiki_path("session", "sess-1", "topic")
        assert p == Path(
            os.path.realpath(str(tmp_path / "global" / "wiki" / "session" / "sess-1" / "topic.md"))
        )

    def test_result_is_confined_under_base(self, svc, tmp_path):
        p = svc.get_wiki_path("project", "proj_a", "topic")
        assert str(p).startswith(os.path.realpath(str(tmp_path)) + os.sep)


class TestGetWikiPathRejects:
    @pytest.mark.parametrize("key", ["../evil", "..", ".", "", "a/b", "a\\b", "a\x00b"])
    def test_malicious_key_rejected(self, svc, key):
        with pytest.raises(ValueError):
            svc.get_wiki_path("global", None, key)

    @pytest.mark.parametrize("scope_id", ["../other", "a/b", "..", "", "/abs"])
    def test_malicious_scope_id_rejected(self, svc, scope_id):
        with pytest.raises(ValueError):
            svc.get_wiki_path("project", scope_id, "topic")

    @pytest.mark.parametrize("scope", ["../wiki", "a/b", ".."])
    def test_malicious_scope_rejected(self, svc, scope):
        with pytest.raises(ValueError):
            svc.get_wiki_path(scope, None, "topic")

    def test_traversal_does_not_escape_base(self, svc, tmp_path):
        # A ../ laden key must raise rather than produce a path outside base.
        try:
            result = svc.get_wiki_path("project", "proj_a", "../../../../etc/passwd")
        except ValueError:
            return
        # If (unexpectedly) no error, the path must still be confined.
        assert str(result).startswith(os.path.realpath(str(tmp_path)) + os.sep)
        pytest.fail("expected ValueError for traversal key")


class TestGetProjectDirRejects:
    def test_valid_project_scope_id(self, svc, tmp_path):
        p = svc._get_project_dir("project", "proj_a")
        assert p == Path(os.path.realpath(str(tmp_path / "proj_a")))

    @pytest.mark.parametrize("scope_id", ["../escape", "a/b", ".."])
    def test_malicious_project_scope_id_rejected(self, svc, scope_id):
        with pytest.raises(ValueError):
            svc._get_project_dir("project", scope_id)
