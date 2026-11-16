import hashlib
from test.backends.fake_hc import FakeHc, layout_with

import pytest

from cli_agent_orchestrator.backends.factory import ConfigurationError
from cli_agent_orchestrator.backends.herdr_app_backend import HerdrAppBackend, space_id_for


@pytest.fixture
def hc():
    return FakeHc()


@pytest.fixture
def backend(hc, monkeypatch):
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.mcp_resolution.cao_server_env",
        lambda: {"CAO_API": "http://x"},
    )
    return HerdrAppBackend(herdr_app_session="cao", client=hc)


def test_space_id_is_deterministic():
    expected = "sp" + hashlib.sha256(b"cao:cao-a").hexdigest()[:32]
    assert space_id_for("cao-a") == expected
    assert space_id_for("cao-a") == space_id_for("cao-a")
    assert space_id_for("cao-b") != expected


def test_incompatible_core_is_a_configuration_error():
    with pytest.raises(ConfigurationError, match="generation 2"):
        HerdrAppBackend(client=FakeHc(generation=2))


def test_create_session_and_window_share_a_space(backend, hc, tmp_path):
    assert backend.create_session("cao-a", "sup", "aaaa0001", str(tmp_path)) == "sup"
    assert (
        backend.create_window("cao-a", "w1", "aaaa0002", str(tmp_path), extra_env={"FOO": "1"})
        == "w1"
    )
    runs = hc.calls_to("terminal.run")
    assert [r["name"] for r in runs] == ["cao-aaaa0001", "cao-aaaa0002"]
    for run, tid, window in zip(runs, ("aaaa0001", "aaaa0002"), ("sup", "w1")):
        assert run["no_parent"] is True and run["no_notify"] is True
        assert run["cwd"] == str(tmp_path)
        assert run["env"]["CAO_TERMINAL_ID"] == tid
        assert run["env"]["CAO_SESSION_NAME"] == "cao-a"
        labels = run["labels"]
        assert labels["herdr.space"] == space_id_for("cao-a")
        assert labels["herdr.tab"].startswith("tb") and len(labels["herdr.tab"]) == 34
        assert labels["cao.managed"] == "1"
        assert (labels["cao.terminal_id"], labels["cao.session"], labels["cao.window"]) == (
            tid,
            "cao-a",
            window,
        )
    assert runs[1]["env"]["FOO"] == "1"
    assert runs[0]["labels"]["herdr.tab"] != runs[1]["labels"]["herdr.tab"]


def test_window_shell_is_passed_as_line(backend, hc, tmp_path):
    backend.create_session(
        "cao-a",
        "sup",
        "aaaa0001",
        str(tmp_path),
    )
    backend.create_window("cao-a", "w1", "aaaa0002", str(tmp_path), window_shell="kiro-cli chat")
    assert hc.calls_to("terminal.run")[1]["line"] == "kiro-cli chat"
    assert "line" not in hc.calls_to("terminal.run")[0]


def test_invalid_working_directory_raises(backend):
    with pytest.raises(ValueError):
        backend.create_session("cao-a", "sup", "aaaa0001", "/no/such/dir")


def test_names_written_when_app_adopted(backend, hc, tmp_path):
    sp = space_id_for("cao-a")
    hc.set_doc("herdr-app", layout_with(sp, [("tbX", "", "t1")]))
    backend.create_session("cao-a", "sup", "aaaa0001", str(tmp_path))
    doc = hc.docs["herdr-app"]["value"]
    assert doc["spaces"][0]["name"] == "cao-a"
    assert doc["spaces"][0]["tabs"][0]["name"] == "sup"
    # nothing else changed
    expected = layout_with(sp, [("tbX", "sup", "t1")], space_name="cao-a")
    assert doc == expected


def test_names_retry_on_conflict(backend, hc, tmp_path):
    hc.set_doc("herdr-app", layout_with(space_id_for("cao-a"), [("tbX", "", "t1")]))
    hc.conflicts_to_inject = 1
    backend.create_session("cao-a", "sup", "aaaa0001", str(tmp_path))
    assert hc.docs["herdr-app"]["value"]["spaces"][0]["name"] == "cao-a"
    assert len(hc.calls_to("doc.set")) == 2


def test_no_write_when_space_absent(backend, hc, tmp_path):
    backend.create_session("cao-a", "sup", "aaaa0001", str(tmp_path))
    assert hc.calls_to("doc.set") == []


def test_no_write_when_names_already_match(backend, hc, tmp_path):
    hc.set_doc("herdr-app", layout_with(space_id_for("cao-a"), [("tbX", "sup", "t1")], "cao-a"))
    backend.create_session("cao-a", "sup", "aaaa0001", str(tmp_path))
    assert hc.calls_to("doc.set") == []


def test_persistent_conflict_does_not_fail_creation(backend, hc, tmp_path, caplog):
    hc.set_doc("herdr-app", layout_with(space_id_for("cao-a"), [("tbX", "", "t1")]))
    hc.conflicts_to_inject = 99
    assert backend.create_session("cao-a", "sup", "aaaa0001", str(tmp_path)) == "sup"
    assert len(hc.calls_to("doc.set")) == 5
    assert "could not name" in caplog.text
