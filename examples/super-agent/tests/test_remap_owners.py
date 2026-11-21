import json

from conftest import base_env, run, write_json

OLD, NEW = "c0ffee02", "5eedf00d"


def watch():
    return {
        "repo": "your-org/your-repo",
        "prs": {"101": {"owner": {"session": "cao-old-session", "terminal": OLD}, "issues": []},
                "999": {"owner": {"session": "s", "terminal": "0b0e0b0e"}},
                "998": {"owner": None},
                "997": {"owner": {"session": None, "terminal": None}}},
        "issues": {"810": {"owner": {"session": "cao-old-session", "terminal": OLD}},
                   "811": {"owner": {"session": "x", "terminal": OLD}}},
        "our_pr_rules": {"scope": "add it with owner c0ffee02"},
    }


def remap(tmp_path, doc, *extra):
    p = write_json(tmp_path / "watch.json", doc) if not isinstance(doc, str) else tmp_path / "watch.json"
    if isinstance(doc, str):
        p.write_text(doc)
    r = run("remap_owners.py", "--watch", p, "--from", OLD, "--to", NEW, "--session", "cao-super",
            *extra, env=base_env(tmp_path))
    return r, p


def test_every_old_owner_moves_ours_external_and_issues(tmp_path):
    r, p = remap(tmp_path, watch())
    assert r.returncode == 0 and r.stdout.strip() == "REMAPPED 3"
    d = json.loads(p.read_text())
    assert d["prs"]["101"]["owner"] == {"session": "cao-super", "terminal": NEW}
    assert d["issues"]["810"]["owner"]["terminal"] == NEW and d["issues"]["811"]["owner"]["terminal"] == NEW


def test_other_and_null_owners_are_untouched(tmp_path):
    r, p = remap(tmp_path, watch())
    d = json.loads(p.read_text())
    assert d["prs"]["999"]["owner"] == {"session": "s", "terminal": "0b0e0b0e"}
    assert d["prs"]["998"]["owner"] is None
    assert d["prs"]["997"]["owner"] == {"session": None, "terminal": None}
    assert d["our_pr_rules"] == watch()["our_pr_rules"]


def test_identities_added_once(tmp_path):
    r, p = remap(tmp_path, watch())
    d = json.loads(p.read_text())
    assert d["identities"]["ours"] == ["your-login"] and "[bot]" in d["identities"]["bot_suffixes"]
    w = watch()
    w["identities"] = {"ours": ["your-login", "alt"], "bots": [], "bot_suffixes": ["[bot]"]}
    r, p = remap(tmp_path, w)
    assert json.loads(p.read_text())["identities"]["ours"] == ["your-login", "alt"]


def test_corrupt_watch_is_left_alone(tmp_path):
    r, p = remap(tmp_path, "{broken")
    assert r.returncode == 2 and p.read_text() == "{broken"


def test_bad_ids_refused(tmp_path):
    p = write_json(tmp_path / "watch.json", watch())
    r = run("remap_owners.py", "--watch", p, "--from", OLD, "--to", "not-an-id", "--session", "s",
            env=base_env(tmp_path))
    assert r.returncode == 2 and json.loads(p.read_text()) == watch()


def test_identities_follow_the_configured_login_and_fail_closed_when_unset(tmp_path):
    local = tmp_path / "local.json"
    local.write_text('{"github_login": "new-user"}')
    p = write_json(tmp_path / "watch.json", watch())
    args = ("--watch", p, "--from", OLD, "--to", NEW, "--session", "cao-super")
    r = run("remap_owners.py", *args, env=base_env(tmp_path, CAO_WORKPLACE_CONFIG=local))
    assert r.returncode == 0 and json.loads(p.read_text())["identities"]["ours"] == ["new-user"]
    local.write_text('{"github_login": null}')
    p = write_json(tmp_path / "watch.json", watch())
    before = p.read_bytes()
    r = run("remap_owners.py", *args, env=base_env(tmp_path, CAO_WORKPLACE_CONFIG=local))
    assert r.returncode == 2 and "workplace_login_unset" in r.stdout and p.read_bytes() == before
