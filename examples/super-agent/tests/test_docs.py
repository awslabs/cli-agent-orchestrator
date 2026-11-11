"""Contract checks on the profiles, playbooks and templates."""
import re

import pytest

from conftest import KIT

TOP = "top -l 1 -o mem -n 5 -stats pid,command,mem"
DOCS = sorted([*(KIT / "agents").glob("*.md"), *(KIT / "templates").glob("*.md"),
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


@pytest.mark.parametrize("rel", ["agents/cao_conductor.md", "agents/pr_shepherd.md",
                                 "conductor-playbook.md", "shepherd-playbook.md"])
def test_top_memory_holder_command_present(rel):
    assert TOP in text(rel)


# recovery never starts the loop; setup = recovery -> remap -> one guarded start
def test_shepherd_recovery_never_starts_the_loop():
    rec = section(text("shepherd-playbook.md"), "## 9. Restart recovery")
    assert "tick_loop.sh" not in rec and not re.search(r"\bStart the loop\b", rec)


def test_shepherd_setup_order():
    setup = section(text("shepherd-playbook.md"), "## 1. Setup")
    i_rec, i_map = setup.index("Restart recovery"), setup.index("remap_owners.py")
    i_guard, i_start = setup.index("pgrep -fl tick_loop.sh"), setup.index("bash PB/tick_loop.sh")
    assert i_rec < i_map < i_guard < i_start
    assert setup.count("bash PB/tick_loop.sh") == 1 and "--from <old owner id>" in setup
    assert "skip to step 3" in setup  # the remap only applies when taking over an older watcher


# no trust-on-first-use; the checker and the user own baselines
def test_no_first_use_baseline():
    for rel in ("conductor-playbook.md", "agents/cao_conductor.md"):
        t = " ".join(text(rel).split())
        assert "The first time, record the hashes" not in t
        assert "plan_hash_check.py" in t and "never record a baseline yourself" in t.lower()


# every outside write is immediately preceded by the full pre-write check
WRITES = re.compile(r"git -C \{worktree\} push |gh pr comment|gh pr merge|gh pr update-branch|gh run rerun"
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


# the reviewer takes the heavy slot and reports to its creator
def test_reviewer_copy_takes_the_heavy_slot():
    t = text("agents/cao_pr_reviewer.md")
    assert "<old owner id>" not in t and "leaving `receiver_id` out" in t
    run = section(t, "## Running a review")
    for s in ("resource_gate.py`", "--acquire-heavy pr-review-<N>", "--refresh-heavy <token>",
              "--release-heavy <token>", "every outcome", "No retry loop", "RESOURCES"):
        assert s in run, s
    assert "Every 45 minutes" in t and "one existing exception" in t


# line limits
def test_profile_body_line_limits():
    assert body_lines("agents/cao_conductor.md") <= 150
    assert body_lines("agents/pr_shepherd.md") <= 120


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

CMD_DOCS = DOCS + [KIT / "mandates.json"]
SCRIPTS = {p.name for p in (KIT / "bin").glob("*.py")}
SUBCOMMANDS = {"outbox.py": {"key", "check", "reserve", "sent", "failed", "find-post", "owner-gone", "now"},
               "holds.py": {"body", "clear"}, "go_ledger.py": {"add", "revoke", "next-id"},
               "state_schema.py": {"check", "append-plan-row"}}


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
    t = text("mandates.json")
    assert "outbox.py record" not in t and "outbox.py sent <key> --url <url>" in t


def test_readme_matches_the_mandates():
    t = " ".join(text("README.md").split())
    assert "says `UPDATE_BRANCH`" in t and "| M5 |" in t and "| M6 |" in t
    assert "| M8 | shepherd |" in t and "holds.py clear" in t
    assert "one existing exception" in t and "reviewer's 45-minute review cycle" in t
    assert "The only poll is" not in t
    ids = {m["id"].split("-")[0] for m in __import__("json").loads(text("mandates.json"))["mandates"]}
    for mid in ids:
        assert f"| {mid} |" in t, mid


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
        push = [ln for ln in steps(block) if re.search(r"\bgit -C \{worktree\} push\b", ln)]
        assert push and all("push {push_url} {new}:refs/heads/{branch}" in ln for ln in push), push
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
    curl = "curl -s -o /dev/null -w '%{url_effective} %{http_code}' http://localhost:9889/terminals/{owner}"
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
        '--search-fetched-at "$(cat {fetched_file})"', "--body-file {body_file} --login your-login"))
    # the owner's own release after a failed post searches (R4-R5) only after its post returned
    assert "run RECONCILE R4-R5 (the search only" in t


# --- the push goes to the verified URL, never to a remote name ---

def test_no_doc_pushes_or_reads_heads_through_origin():
    for doc in DOCS + [KIT / "mandates.json"]:
        t = doc.read_text()
        assert not re.search(r"git (?:-C \S+ )?push origin\b", t), doc.name
        assert "push exactly git push origin" not in t, doc.name
    t = text("templates/publisher.md")
    assert "ls-remote origin" not in t and "ls-remote {push_url} refs/heads/{branch}" in t
    assert "exactly `https://github.com/your-org/your-repo.git`" in t



# --- only the conductor writes go.jsonl ---

def test_only_the_conductor_writes_the_go_ledger():
    cp = " ".join(text("conductor-playbook.md").split())
    assert cp.count("go_ledger.py add --writer-role conductor") >= 3       # WOULD, clear_hold, one-off
    assert "go_ledger.py revoke --writer-role conductor" in cp and "Record the GO yourself first" in cp
    assert "go_ledger.py add --writer-role conductor" in " ".join(text("agents/cao_conductor.md").split())
    assert "Never write go.jsonl" in text("agents/pr_shepherd.md")
    sp = text("shepherd-playbook.md")
    assert "never write `go.jsonl`" in sp and not re.search(r"go_ledger\.py (add|revoke)", sp)
    for doc in DOCS + [KIT / "mandates.json"]:  # every command line names the conductor as writer
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
    assert cp.count("state_schema.py append-plan-row --row") == 2 and "state_schema.py check" in cp


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
    for rel in ("README.md", "agents/cao_conductor.md", "agents/pr_shepherd.md",
                "templates/publisher.md", "templates/automerge-armer.md"):
        assert "next-id" in flat(KIT / rel), rel
    assert "<N>-<action>-<head8>-<k>" in flat(KIT / "README.md")

