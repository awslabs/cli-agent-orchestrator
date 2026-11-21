"""Contract checks on the profiles, playbooks and templates."""
import json
import re
import subprocess

import pytest

from conftest import KIT, KIT_MANDATES

# Profiles are templates ({{KIT}} is rendered at install); the playbooks use the KIT symbol.
TOP = {"profile": "python3 {{KIT}}/bin/top_mem.py", "playbook": "python3 KIT/bin/top_mem.py"}
DOCS = sorted([*(KIT / "agents").glob("*.md.tmpl"), *(KIT / "templates").glob("*.md"),
               KIT / "conductor-playbook.md", KIT / "shepherd-playbook.md", KIT / "README.md"])


def text(rel):
    return (KIT / rel).read_text()


def section(doc, title):
    start = doc.index(title)
    nxt = doc.find("\n## ", start + 1)
    return doc[start:] if nxt == -1 else doc[start:nxt]


def body_lines(rel):
    lines = text(rel).splitlines()
    return len(lines) - [i for i, ln in enumerate(lines) if ln == "---"][1] - 1


# top memory holders use top, not RSS
@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_no_rss_memory_holder_command(doc):
    assert "ps -axo rss" not in doc.read_text()


@pytest.mark.parametrize("rel", ["agents/cao_conductor.md.tmpl", "agents/pr_shepherd.md.tmpl",
                                 "conductor-playbook.md", "shepherd-playbook.md"])
def test_top_memory_holder_command_present(rel):
    assert TOP["profile" if rel.startswith("agents/") else "playbook"] in text(rel)


# finding 8: recovery never starts the loop; setup = recovery -> remap -> loop guard.
# Item 8 drift fix: the conductor's tick_supervisor.sh owns the loop (2026-11-14), so setup starts
# no loop, and the remap comes from the previous owner the conductor names, not a fixed id.
def test_shepherd_recovery_never_starts_the_loop():
    rec = section(text("shepherd-playbook.md"), "## 9. Restart recovery")
    assert "tick_loop.sh" not in rec and not re.search(r"\bStart the loop\b", rec)


def test_shepherd_setup_order():
    setup = section(text("shepherd-playbook.md"), "## 1. Setup")
    i_rec, i_map = setup.index("Restart recovery"), setup.index("remap_owners.py")
    i_guard = setup.index(r"pgrep -fl 'tick_[l]oop\.sh'")
    assert i_rec < i_map < i_guard
    assert "bash PB/tick_loop.sh" not in setup and "you never start the loop" in setup
    assert "tick_supervisor.sh" in setup and "SHEPHERD BLOCKED another tick loop is running" in setup
    assert "--from <previous owner id>" in setup
    wake = section(text("shepherd-playbook.md"), "## 2. On each wake")
    assert "bash PB/tick_loop.sh" not in wake and "Don't start or restart the loop" in wake


# no trust-on-first-use; the checker and the user own baselines
def test_no_first_use_baseline():
    for rel in ("conductor-playbook.md", "agents/cao_conductor.md.tmpl"):
        t = " ".join(text(rel).split())
        assert "The first time, record the hashes" not in t
        assert "approve_review_plan" not in t  # the reviewer and its plan approvals are gone


# every outside write is immediately preceded by the full pre-write check
WRITES = re.compile(r"git -C \{worktree\} (?:-c \S+ )*push |gh pr comment|gh pr merge|gh pr update-branch|gh run rerun"
                    r"|gh pr create")


def steps(block):
    return [ln for ln in block.splitlines() if re.match(r"^R?\d+\.", ln)]


@pytest.mark.parametrize("rel", ["templates/publisher.md", "templates/automerge-armer.md"])
def test_every_write_is_preceded_by_the_pre_write_check(rel):
    t = text(rel)
    found = 0
    for block in re.findall(r"```text\n(.*?)```", t, re.S):
        st = steps(block)
        for i, ln in enumerate(st):
            if WRITES.search(ln):
                found += 1
                prev = [x for x in st[max(0, i - 2):i] if "check_mandate.py" in x]
                assert prev, f"no check_mandate.py right before: {ln}"
                for opt in ("--expect-policy-version", "--expect-head", "--remote-head", "ls-remote"):
                    assert opt in prev[-1], (opt, ln)
    assert found >= (4 if "publisher" in rel else 2)


def test_publisher_reserves_before_posting_and_has_an_exact_reconcile():
    t = text("templates/publisher.md")
    assert t.index("outbox.py reserve") < t.index("gh pr comment") < t.index("outbox.py sent")
    rec = t[t.index("RECONCILE ("):t.index("RE-RUN (")]
    assert "gh api --paginate --slurp" in rec and "outbox.py find-post" in rec
    assert "outbox.py sent" in rec and "outbox.py failed" in rec and "gh pr comment" not in rec


# one worker does exactly one of update_branch or arm
def test_armer_does_exactly_one_action():
    t = text("templates/automerge-armer.md")
    upd = section(t, "## Assign message: `update_branch`")
    arm = section(t, "## Assign message: `arm`")
    assert "gh pr update-branch" in upd and "gh pr merge" not in upd
    assert "gh pr merge" in arm and "update-branch" not in arm
    assert "Do NOT arm" in upd and "--match-head-commit {head}" in arm


# heavy slot: release by token everywhere
@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_no_release_by_label(doc):
    assert not re.search(r"--release-heavy \{?label\}?", doc.read_text())


# line limits
def test_profile_body_line_limits():
    assert body_lines("agents/cao_conductor.md.tmpl") <= 150
    assert body_lines("agents/pr_shepherd.md.tmpl") <= 120


# clear-by-reading needs the conductor's GO; real findings have no clear path
def test_clear_by_reading_flow_is_documented():
    sp = " ".join(text("shepherd-playbook.md").split())
    for s_ in ("holds.py body", "WOULD <wid>: clear_hold", "holds.py clear", "also in act mode",
               "refuses to clear them", "You never write `go.jsonl`"):
        assert s_ in sp, s_
    cp = " ".join(text("conductor-playbook.md").split())
    assert "clear_hold" in cp and "same body file" in cp and "needed in act mode too" in cp
    assert "go_ledger.py add --writer-role conductor --id <wid> --action clear_hold" in cp


# --- every kit command, subcommand and flag named in a doc exists in bin/ ---

import subprocess  # noqa: E402
import sys  # noqa: E402
from functools import lru_cache  # noqa: E402

CMD_DOCS = DOCS + [KIT_MANDATES]
SCRIPTS = {p.name for p in (KIT / "bin").glob("*.py")}
SUBCOMMANDS = {"outbox.py": {"key", "check", "reserve", "sent", "failed", "find-post", "owner-gone", "now"},
               "holds.py": {"body", "clear"}, "go_ledger.py": {"add", "revoke", "next-id"},
               "state_schema.py": {"check", "append-plan-row"},
               "writer_lease.py": {"show", "claim", "release"}}


@lru_cache(maxsize=None)
def flags_of(script, sub=None):
    args = [sys.executable, "-I", str(KIT / "bin" / script)] + ([sub] if sub else []) + ["--help"]
    out = subprocess.run(args, capture_output=True, text=True, timeout=30).stdout
    return frozenset(re.findall(r"--[a-z][a-z0-9-]*", out))


def test_subcommand_table_matches_the_scripts():
    for script, subs in SUBCOMMANDS.items():
        usage = subprocess.run([sys.executable, "-I", str(KIT / "bin" / script), "--help"],
                               capture_output=True, text=True, timeout=30).stdout
        listed = set(re.search(r"\{([a-z,-]+)\}", usage).group(1).split(","))
        assert listed == subs, (script, listed)


# English words that may follow a script name in prose ("`holds.py` refuses ..."); anything else
# right after outbox.py / holds.py / go_ledger.py must be a real subcommand.
PROSE = {"refuses", "prints", "says", "reads", "re-reads", "records", "returns", "and", "or", "to",
         "is", "then", "with", "first", "exits", "writes", "never", "honours"}


def mentions(doc_text):
    """(script, rest-of-command) for every kit script named in the text."""
    for m in re.finditer(r"([A-Za-z0-9_-]+\.py)\b(`?)([^\n`;]*)", doc_text):
        yield m.group(1), m.group(2), m.group(3)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


@pytest.mark.parametrize("doc", CMD_DOCS, ids=lambda p: p.name)
def test_doc_commands_exist(doc):
    t = doc.read_text()
    for name in re.findall(r"(?:bin/|\$K/)([A-Za-z0-9_-]+\.py)", t):
        assert name in SCRIPTS, f"{doc.name}: bin/{name} does not exist"
    texts = list(strings(__import__("json").loads(t))) if doc.suffix == ".json" else [t]
    for script, tick, rest in (m for x in texts for m in mentions(x)):
        if script not in SCRIPTS:
            continue
        sub = None
        if script in SUBCOMMANDS:
            words = re.match(r"\s+([a-z][a-z-]*(?:/[a-z][a-z-]*)*)\b", rest)
            if words and words.group(1) not in PROSE:
                for w in words.group(1).split("/"):
                    assert w in SUBCOMMANDS[script], f"{doc.name}: {script} {w} is not a subcommand"
                sub = words.group(1).split("/")[0]
        if tick:
            continue  # flags only inside the same code span
        segment = re.split(r"\s(?:->|\||&&)\s", rest)[0]
        valid = flags_of(script, sub) if sub else flags_of(script) | frozenset().union(
            *(flags_of(script, s) for s in SUBCOMMANDS.get(script, ())))
        for flag in re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", segment):
            assert flag in valid, f"{doc.name}: {script} {sub or ''} has no {flag}: {segment[:120]}"


def test_mandates_name_the_real_outbox_commands():
    t = KIT_MANDATES.read_text()
    assert "outbox.py record" not in t and "outbox.py sent <key> --url <url>" in t


def test_readme_matches_the_mandates():
    t = " ".join(text("README.md").split())
    assert "says `UPDATE_BRANCH`" in t and "| M5 |" in t and "| M6 |" in t
    assert "| M8 | 2 | shepherd |" in t and "holds.py clear" in t
    assert "reviewer's 45-minute review cycle" not in t and "There is no M7" in t
    assert "The only poll is" not in t
    for m in __import__("json").loads(KIT_MANDATES.read_text())["mandates"]:
        assert f"| {m['id'].split('-')[0]} | {m['tier']} |" in t, m["id"]


# --- no reduced publisher path; one-offs are GO records checked in full ---

def test_publisher_has_no_reduced_one_off_path():
    t = text("templates/publisher.md")
    assert '["paused"] is not False' not in t and "replaced by" not in t
    assert "--granted-by user --relayed-by" in t and "ALLOW oneoff go=" in t
    block = re.search(r"## Assign message\n```text\n(.*?)```", t, re.S).group(1)
    for ln in steps(block):
        if "check_mandate.py" in ln:
            for opt in ("--expect-policy-version", "--expect-head", "--remote-head", "ls-remote"):
                assert opt in ln, (opt, ln)
    assert re.search(r"gh pr create", block)


# --- a push is pinned to the GO's commit ---

def test_push_steps_pin_the_commit():
    for rel in ("templates/publisher.md",):
        block = re.search(r"## Assign message\n```text\n(.*?)```", text(rel), re.S).group(1)
        push = [ln for ln in steps(block) if re.search(r"\bgit -C \{worktree\} (?:-c \S+ )*push\b", ln)]
        assert push and all("push --no-verify {push_url} {new}:refs/heads/{branch}" in ln for ln in push), push
        check = [ln for ln in steps(block) if "--action {push_action}" in ln]
        assert check and all(o in check[0] for o in ("--new-head {new}", '--local-head "$L"',
                                                    "--branch {branch}", "rev-parse refs/heads/{branch}"))
    for rel in DOCS:
        assert not re.search(r"git (?:-C \S+ )?push origin \{?branch\}?(?:\s|$|`)", rel.read_text()), rel.name


# --- the fetch recipe captures the HTTP status ---

def test_required_contexts_recipe_captures_status():
    sec = section(text("shepherd-playbook.md"), "## 4. Auto-merge flow")
    for ep in ("$R/rules/branches/main", "$R/branches/main/protection"):
        assert f"gh api -i {ep} >" in sec and f"api_envelope.py --endpoint {ep} " in sec
    assert '--required "$D/required-rules.json" --required "$D/required-classic.json"' in sec
    assert "2>/dev/null" not in sec and "C=()" not in sec


# --- reconcile proves the owner gone before `failed` ---

def test_reconcile_proves_the_owner_gone():
    t = text("templates/publisher.md")
    rec = t[t.index("RECONCILE ("):t.index("RE-RUN (")]
    curl = "curl -s -o /dev/null -w '%{url_effective} %{http_code}' {cao_api}/terminals/{owner}"
    assert curl in rec and "outbox.py owner-gone {key} --generation {generation} --evidence {gone_file}" in rec
    assert rec.index(curl) < rec.index("outbox.py owner-gone") < rec.index("outbox.py failed")
    assert '--owner-terminal "$CAO_TERMINAL_ID"' in t and "--token <T>" in t


# --- owner gone first, then a fresh search, then the same generation ---

def test_reconcile_searches_only_after_the_owner_gone_proof():
    t = text("templates/publisher.md")
    rec = t[t.index("RECONCILE ("):t.index("RE-RUN (")]
    st = steps(rec)
    assert [ln.split(".")[0] for ln in st] == ["R1", "R2", "R3", "R4", "R5", "R6"], st
    proof, search = rec.index("outbox.py owner-gone"), rec.index("gh api --paginate --slurp")
    assert rec.index("outbox.py now > {fetched_file}") < search and proof < rec.index("outbox.py now")
    assert proof < search < rec.index("outbox.py find-post") < rec.index("outbox.py sent")
    assert "outbox.py sent {key} --url <url> --generation {generation}" in rec
    failed = [ln for ln in st if "outbox.py failed" in ln]
    assert len(failed) == 1 and all(o in failed[0] for o in (
        "--owner-gone --generation {generation} --search-file {search_file}",
        '--search-fetched-at "$(cat {fetched_file})"', "--body-file {body_file} --login {login}"))
    # the owner's own release after a failed post searches (R4-R5) only after its post returned
    assert "run RECONCILE R4-R5 (the search only" in t


# --- the push goes to the verified URL, never to a remote name ---

def test_no_doc_pushes_or_reads_heads_through_origin():
    for doc in DOCS + [KIT_MANDATES]:
        t = doc.read_text()
        assert not re.search(r"git (?:-C \S+ )?push origin\b", t), doc.name
        assert "push exactly git push origin" not in t, doc.name
    t = text("templates/publisher.md")
    assert "ls-remote origin" not in t and "ls-remote {push_url} refs/heads/{branch}" in t
    assert "exactly `{repo_url}`" in t



# --- only the conductor writes go.jsonl ---

def test_only_the_conductor_writes_the_go_ledger():
    cp = " ".join(text("conductor-playbook.md").split())
    assert cp.count("go_ledger.py add --writer-role conductor") >= 3       # WOULD, clear_hold, one-off
    assert "go_ledger.py revoke --writer-role conductor" in cp and "Record the GO yourself first" in cp
    assert "go_ledger.py add --writer-role conductor" in " ".join(text("agents/cao_conductor.md.tmpl").split())
    assert "Never write go.jsonl" in text("agents/pr_shepherd.md.tmpl")
    sp = text("shepherd-playbook.md")
    assert "never write `go.jsonl`" in sp and not re.search(r"go_ledger\.py (add|revoke)", sp)
    for doc in DOCS + [KIT_MANDATES]:  # every command line names the conductor as writer
        for m in re.finditer(r"go_ledger\.py (add|revoke) (--[^\n`]*)", doc.read_text()):
            assert m.group(2).startswith("--writer-role conductor"), (doc.name, m.group(0)[:100])
    assert "discipline control, not an operating-system boundary" in text("README.md")


# --- state files fail closed; the README says what is and isn't covered ---

def test_readme_limits_state_files():
    t = " ".join(section(text("README.md"), "## Limits").split())
    assert "deliberately forging a well-formed state row" in t and "out of scope" in t.lower()
    for s_ in ("in scope is accidental damage", "a truncated line, a missing field or a wrong type",
               "always fails closed", "hand repair by the conductor after it inspects the file"):
        assert s_ in t, s_
    cp = " ".join(text("conductor-playbook.md").split())
    assert "state_schema.py check" in cp


# --- the old <N>-<action>-<head8> recipe is gone everywhere; next-id replaces it ---

def flat(p):
    return " ".join(p.read_text().split())


def test_old_would_id_format_is_gone():
    for p in DOCS:
        t = flat(p)
        assert "`<N>-<action>-<head8>`" not in t, p.name
        assert "<N>-clear_hold-<id>" not in t, p.name
        assert "WOULD id `<N>-<action>-<head8>`" not in t and "= `<N>-<action>-<head8>`." not in t, p.name


def test_docs_build_every_would_id_with_next_id():
    sp = flat(KIT / "shepherd-playbook.md")
    assert sp.count("go_ledger.py next-id --pr <N> --action") == 2  # §4 clear_hold and §5 WOULD
    assert "`<N>-<action>-<head8>-<k>`" in sp and "with a new id from `go_ledger.py next-id`, never the old one" in sp
    assert "go_ledger.py next-id --pr <N> --action <a> --head <sha>" in flat(KIT / "conductor-playbook.md")
    for rel in ("README.md", "agents/cao_conductor.md.tmpl", "agents/pr_shepherd.md.tmpl",
                "templates/publisher.md", "templates/automerge-armer.md"):
        assert "next-id" in flat(KIT / rel), rel
    assert "<N>-<action>-<head8>-<k>" in flat(KIT / "README.md")



@pytest.mark.parametrize('rel',['conductor-playbook.md','shepherd-playbook.md','agents/cao_conductor.md.tmpl','agents/pr_shepherd.md.tmpl'])
def test_lease_guidance_pinned(rel):
    doc=text(rel)
    for expected in ('writer_lease.py show','At setup and on every wake','this host: yes',
                     'socket.gethostname()', 'BLOCKED not_writer', 'operator/conductor-only',
                     'never claim or force', 'claim, release or --force'):
        assert expected in doc,expected


@pytest.mark.parametrize('rel',['conductor-playbook.md','shepherd-playbook.md'])
def test_handwritten_state_is_fenced(rel):
    doc=text(rel)
    for expected in ('state_write.py', '--append', 'shepherd-prs.json','shepherd-pending.json',
                     'shepherd-workers.json', 'hold-findings.json', 'watch.json.pre-shepherd-',
                     'mandates.json.bak-', 'PB/log.md', 'reader host can acquire'):
        assert expected in doc
    assert 'cp PB/watch.json' not in doc and 'cp KIT/mandates.json' not in doc


def test_publisher_rerun_check_binds_the_run_id():
    """A rerun GO binds its run, so step 15's check passes --run-id, and the conductor's GO row says so."""
    (step,) = [line for line in text("templates/publisher.md").splitlines() if line.startswith("15. ")]
    assert '--remote-head "$R" --run-id {run_id} {go_rerun}' in step
    cp = text("conductor-playbook.md")
    assert "a `rerun_failed_outage` GO needs `--run-id`" in cp


def test_conductor_oneoff_row_takes_its_own_fresh_id():
    """The one-off row itself names next-id; the WOULD row saying it too must not hide its loss."""
    (row,) = [line for line in text("conductor-playbook.md").splitlines()
              if line.startswith("| The user OKs one such action once")]
    assert "Take a fresh `<wid>` from `python3 KIT/bin/go_ledger.py next-id --pr <N> --action <a> --head <sha>`" in row


def test_publisher_go_post_always_needs_a_go_and_act_mode_push_and_rerun_take_none():
    """M2 is tier 2, so a reply needs the conductor's GO even in act mode; a push or re-run doesn't."""
    flat_pub = " ".join(text("templates/publisher.md").split())
    assert "with nothing in act mode" not in flat_pub
    assert ("In act mode `{go_post}` still needs `--go-id <wid>` (M2 is tier 2), and `{go_push}` and "
            "`{go_rerun}` are empty.") in flat_pub


def test_publisher_readback_uses_the_body_equals_helper():
    (step,) = [line for line in text("templates/publisher.md").splitlines() if line.startswith("13. ")]
    assert step.startswith("13. python3 $K/body_equals.py {readback_file} {body_file}  -> expect BODY-OK")
    assert "python3 -c" not in step


def test_publisher_rerun_reads_the_runs_head_and_branch_before_rerunning():
    (step,) = [line for line in text("templates/publisher.md").splitlines() if line.startswith("14. ")]
    assert "--json status,conclusion,headSha,event,headBranch" in step
    assert "the run's headSha must equal {run_head}" in step and "STATUS: blocked" in step


def test_the_would_and_the_go_reply_carry_the_run_id_a_rerun_go_binds():
    spb = flat(KIT / "shepherd-playbook.md")
    assert "body_sha256=<shasum -a 256 of the body | null> run_id=<the run a rerun_* re-runs | null>" in spb
    cpb = flat(KIT / "conductor-playbook.md")
    assert "body_sha256=<hex or null> run_id=<id or null> policy_version=<v> expires_in=<minutes>`" in cpb


def test_tier1_is_described_as_checked_scope_plus_written_judgment():
    """Tier 1 isn't 'a script can check every condition': M1's competing-PR check and M3's log reading are judgment."""
    readme = flat(KIT / "README.md")
    about = json.loads(KIT_MANDATES.read_text())["about"]
    for t in (readme, about):
        assert "can check every condition" not in t
        assert "checks the scope, heads, pins and caps" in t and "the shepherd's judgment" in t


def run_step14(tmp_path, pr_branch, run_line):
    """Run publisher step 14 as written, with a fake gh that answers the PR view and the run view."""
    (step,) = [line for line in text("templates/publisher.md").splitlines() if line.startswith("14. ")]
    cmd = step[len("14. "):].split("  -> expect", 1)[0]
    for k, v in {"{pr}": "101", "{repo}": "your-org/your-repo", "{run_id}": "12345678901",
                 "{run_head}": "a" * 40}.items():
        cmd = cmd.replace(k, v)
    fake = tmp_path / "bin"
    fake.mkdir(parents=True)
    (fake / "gh").write_text("#!/bin/bash\n"
                             f'case "$1 $2" in "pr view") echo {pr_branch!r};; "run view") echo {run_line!r};; esac\n')
    (fake / "gh").chmod(0o755)
    return subprocess.run(["bash", "-c", cmd], env={"PATH": f"{fake}:/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=20)


def test_publisher_step14_takes_the_branch_from_the_pr_and_refuses_another_run(tmp_path):
    (step,) = [line for line in text("templates/publisher.md").splitlines() if line.startswith("14. ")]
    assert "gh pr view {pr} -R {repo} --json headRefName --jq .headRefName" in step
    head = "a" * 40
    ok = run_step14(tmp_path / "ok", "feat/x", f"completed failure {head} feat/x pull_request")
    assert ok.returncode == 0 and ok.stdout.startswith("RUN-OK ")
    for name, line in (("branch", f"completed failure {head} main push"),
                       ("prefix", f"completed failure {head} feat/x-other pull_request"),
                       ("head", f"completed failure {'b' * 40} feat/x pull_request"),
                       ("success", f"completed success {head} feat/x pull_request")):
        bad = run_step14(tmp_path / name, "feat/x", line)
        assert bad.returncode == 1 and bad.stdout.startswith("RUN-WRONG "), name


def test_a_new_ok_in_the_conductor_playbook_carries_its_tier():
    cpb = flat(KIT / "conductor-playbook.md")
    assert "**New OK:** a new mandate with `granted_by: \"user\"`, `granted_at`, a `source`, a `tier` (1, or 2" in cpb


def test_the_watcher_readme_describes_the_write_ahead_order():
    t = flat(KIT / "pr-watch" / "README.md")
    assert "(fsync'd) **before** the first delivery attempt" in t
    assert "repeats that WAKE at most once" in t
