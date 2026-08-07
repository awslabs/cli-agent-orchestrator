#!/usr/bin/env bash
# End-to-end verification of the issue tracker against an ISOLATED CAO instance.
#
#   scripts/verify-issue-tracker.sh [--port 9971] [--keep]
#
# Isolation needs three things, not one:
#
#   CAO_STATE_ROOT   its own database, logs and state
#   TMUX_TMPDIR      its own tmux socket — session discovery is tmux-based, so
#                    an instance sharing the operator's socket lists and can act
#                    on the LIVE fleet. Must be a short path; socket paths have
#                    a length limit.
#   --port           nothing else bound there
#
# Every assertion goes through HTTP against the running server, so this
# exercises route ordering, status-code mapping and query parsing — none of
# which a direct service call can check.
set -uo pipefail

PORT=9971
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="${CAO_VERIFY_PYTHON:-$HOME/Projects/cli-agent-orchestrator/.venv/bin/python}"
[[ -x "$VENV_PY" ]] || { printf 'no python at %s\n' "$VENV_PY" >&2; exit 2; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/cao-tracker-verify.XXXXXX")"
export CAO_STATE_ROOT="$WORK/state"
export TMUX_TMPDIR="$WORK/tmux"
mkdir -p "$CAO_STATE_ROOT" "$TMUX_TMPDIR"
BASE="http://127.0.0.1:$PORT"

PASS=0
FAIL=0
SERVER_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
  fi
  if [[ "$KEEP" == 1 ]]; then
    printf '\nkept: %s\n' "$WORK"
  else
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# check <id> <description> <expected> <actual>
check() {
  if [[ "$3" == "$4" ]]; then
    PASS=$((PASS + 1))
    printf '  \033[32mok\033[0m   %-5s %s\n' "$1" "$2"
  else
    FAIL=$((FAIL + 1))
    printf '  \033[31mFAIL\033[0m %-5s %s\n       expected: %s\n       actual:   %s\n' \
      "$1" "$2" "$3" "$4"
  fi
}

# status <METHOD> <path> [json-body]
status() {
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -s -o /dev/null -w '%{http_code}' -X "$method" "$BASE$path" \
      -H 'Content-Type: application/json' -d "$body"
  else
    curl -s -o /dev/null -w '%{http_code}' -X "$method" "$BASE$path"
  fi
}

# json <METHOD> <path> [json-body]  -> response body
json() {
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -s -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body"
  else
    curl -s -X "$method" "$BASE$path"
  fi
}

jqr() { printf '%s' "$1" | "$VENV_PY" -c "import json,sys; print(eval(sys.argv[1], {'d': json.load(sys.stdin)}))" "$2"; }

# ---------------------------------------------------------------------------
say "Starting an isolated cao-server on port $PORT"
printf '  state root: %s\n  tmux tmpdir: %s\n' "$CAO_STATE_ROOT" "$TMUX_TMPDIR"

PYTHONPATH="$REPO_ROOT/src" "$VENV_PY" -m uvicorn cli_agent_orchestrator.api.main:app \
  --host 127.0.0.1 --port "$PORT" --log-level warning >"$WORK/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 60); do
  if curl -s -o /dev/null "$BASE/health"; then break; fi
  sleep 0.5
done
if ! curl -s -o /dev/null "$BASE/health"; then
  printf 'server did not come up; log follows\n' >&2
  cat "$WORK/server.log" >&2
  exit 1
fi
printf '  up (pid %s)\n' "$SERVER_PID"

# Baseline the live install BEFORE this run touches anything, so J2 can assert
# that this run changed nothing rather than that the machine was pristine.
LIVE_DB_BASELINE="$HOME/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db"
LIVE_ROWS_BEFORE=0
if [[ -f "$LIVE_DB_BASELINE" ]]; then
  LIVE_ROWS_BEFORE=$("$VENV_PY" - "$LIVE_DB_BASELINE" <<'PYB'
import sqlite3, sys
try:
    conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tracker_%'")]
    print(sum(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables))
except Exception:
    print("unreadable")
PYB
)
fi
printf '  live-install tracker rows before this run: %s\n' "$LIVE_ROWS_BEFORE"

# Fixture directories. Note the sibling that shares a name prefix — A3.
CONDUCTOR="$WORK/repos/cao-conductor"
FORK="$WORK/repos/cli-agent-orchestrator"
SIBLING="$WORK/repos/cao-conductor-worktrees"
OTHER="$WORK/repos/aegix"
mkdir -p "$CONDUCTOR/conduct/lib" "$FORK/src" "$SIBLING/fire-marshal-p1" "$OTHER"

# ---------------------------------------------------------------------------
say "A. Project identity"

BODY=$(printf '{"name":"CAO System","id":"cao-system","issue_prefix":"cond","scopes":[{"kind":"path","value":"%s"},{"kind":"path","value":"%s"},{"kind":"session","value":"cao-p1-closure"}]}' "$CONDUCTOR" "$FORK")
check A1 "create a project spanning two paths and a session" 201 "$(status POST /tracker/projects "$BODY")"

DETAIL=$(json GET /tracker/projects/cao-system)
check A1b "three scopes recorded" 3 "$(jqr "$DETAIL" "len(d['scopes'])")"

R=$(json GET "/tracker/projects/resolve?cwd=$CONDUCTOR/conduct/lib")
check A2 "subdirectory resolves by path" "cao-system path" "$(jqr "$R" "d['project_id']+' '+str(d['matched_by'])")"

R=$(json GET "/tracker/projects/resolve?cwd=$SIBLING/fire-marshal-p1")
check A3 "name-prefix sibling does NOT match" "None" "$(jqr "$R" "d['project_id']")"

R=$(json GET "/tracker/projects/resolve?session=cao-p1-closure&cwd=$OTHER")
check A4 "session resolves across directories" "session" "$(jqr "$R" "d['matched_by']")"

status POST /tracker/projects/cao-system/scopes '{"kind":"project_id","value":"cao-conductor-self-heal"}' >/dev/null
R=$(json GET "/tracker/projects/resolve?alias=cao-conductor-self-heal")
check A5 "campaign alias resolves" "alias" "$(jqr "$R" "d['matched_by']")"

R=$(json GET "/tracker/projects/resolve?project=cao-system&session=unknown-session")
check A6 "explicit beats a conflicting session" "explicit" "$(jqr "$R" "d['matched_by']")"

status POST /tracker/projects '{"name":"Aegix","id":"aegix"}' >/dev/null
check A7 "path owned by another project is refused" 409 \
  "$(status POST /tracker/projects/aegix/scopes "$(printf '{"kind":"path","value":"%s"}' "$CONDUCTOR")")"

R=$(json POST /tracker/projects/cao-system/scopes "$(printf '{"kind":"path","value":"%s"}' "$FORK")")
check A8 "re-registering the same scope is idempotent" "False" "$(jqr "$R" "d['created']")"

R=$(json POST /tracker/projects/aegix/scopes "$(printf '{"kind":"path","value":"%s/"}' "$OTHER")")
FIRST_ID=$(jqr "$R" "d['id']")
R=$(json POST /tracker/projects/aegix/scopes "$(printf '{"kind":"path","value":"%s"}' "$OTHER")")
check A9 "trailing separator is the same scope" "False" "$(jqr "$R" "d['created']")"

status POST /tracker/projects/aegix/scopes '{"kind":"git_remote","value":"git@github.com:g/aegix.git"}' >/dev/null
R=$(json POST /tracker/projects/aegix/scopes '{"kind":"git_remote","value":"https://github.com/g/aegix"}')
check A10 "ssh and https remotes are one scope" "False" "$(jqr "$R" "d['created']")"

R=$(json POST /tracker/projects/aegix/scopes '{"kind":"git_remote","value":"https://user:ghp_secret@github.com/g/other.git"}')
check A11 "remote credentials are not stored" "github.com/g/other" "$(jqr "$R" "d['value']")"

check A12 "relative path scope refused" 400 \
  "$(status POST /tracker/projects/aegix/scopes '{"kind":"path","value":"relative/dir"}')"

R=$(json GET "/tracker/projects/resolve?cwd=$WORK/nowhere")
check A13 "unregistered directory resolves to nothing, not an error" "None" "$(jqr "$R" "d['project_id']")"

status POST /tracker/projects '{"name":"Gateway","id":"gateway"}' >/dev/null
status POST /tracker/projects/gateway/scopes "$(printf '{"kind":"path","value":"%s/conduct"}' "$CONDUCTOR")" >/dev/null
R=$(json GET "/tracker/projects/resolve?cwd=$CONDUCTOR/conduct/lib")
check A14 "the deeper path scope wins" "gateway" "$(jqr "$R" "d['project_id']")"
status DELETE /tracker/projects/gateway >/dev/null

# ---------------------------------------------------------------------------
say "B. Issue keys"

K1=$(jqr "$(json POST /tracker/issues '{"project_id":"cao-system","title":"first"}')" "d['key']")
K2=$(jqr "$(json POST /tracker/issues '{"project_id":"cao-system","title":"second"}')" "d['key']")
check B1 "keys use the project prefix and increment" "cond-0001 cond-0002" "$K1 $K2"

status DELETE "/tracker/issues/$K2" >/dev/null
K3=$(jqr "$(json POST /tracker/issues '{"project_id":"cao-system","title":"third"}')" "d['key']")
check B2 "a deleted key is never reissued" "cond-0003" "$K3"

status POST /tracker/issues '{"project_id":"cao-system","title":"migrated","key":"cond-0242"}' >/dev/null
K4=$(jqr "$(json POST /tracker/issues '{"project_id":"cao-system","title":"after"}')" "d['key']")
check B3 "an explicit key advances the counter past it" "cond-0243" "$K4"

status POST /tracker/issues '{"project_id":"cao-system","title":"older","key":"cond-0005"}' >/dev/null
K5=$(jqr "$(json POST /tracker/issues '{"project_id":"cao-system","title":"after2"}')" "d['key']")
check B4 "an explicit key below the counter does not rewind it" "cond-0244" "$K5"

check B5 "a duplicate explicit key is refused" 409 \
  "$(status POST /tracker/issues '{"project_id":"cao-system","title":"x","key":"cond-0242"}')"

KA=$(jqr "$(json POST /tracker/issues '{"project_id":"aegix","title":"separate"}')" "d['key']")
check B6 "projects keep independent sequences" "aegix-0001" "$KA"

status PATCH /tracker/projects/aegix '{"issue_prefix":"agx"}' >/dev/null
KB=$(jqr "$(json POST /tracker/issues '{"project_id":"aegix","title":"after prefix change"}')" "d['key']")
check B7 "a prefix change leaves existing keys alone" "aegix-0001 agx-0002" \
  "$(jqr "$(json GET "/tracker/issues/$KA")" "d['key']") $KB"

# ---------------------------------------------------------------------------
say "C. Filing and editing"

R=$(json POST /tracker/issues "$(printf '{"title":"filed from a scoped dir","cwd":"%s/src"}' "$FORK")")
check C1 "filing by cwd resolves and reports how" "cao-system path" \
  "$(jqr "$R" "d['project_id']+' '+str(d['resolved_by'])")"

check C2 "an unresolvable filing site is refused" 422 \
  "$(status POST /tracker/issues "$(printf '{"title":"orphan","cwd":"%s/nowhere"}' "$WORK")")"

TARGET=$(jqr "$(json POST /tracker/issues '{"project_id":"cao-system","title":"editable","severity":"P2","assignee":"terra"}')" "d['key']")

R=$(json PATCH "/tracker/issues/$TARGET" '{"status":"in-progress","actor":"colin"}')
check C3 "PATCH touches only what it names" "P2 terra in-progress" \
  "$(jqr "$R" "d['severity']+' '+str(d['assignee'])+' '+d['status']")"

json PATCH "/tracker/issues/$TARGET" '{"status":"in-progress","actor":"colin"}' >/dev/null
R=$(json GET "/tracker/issues/$TARGET")
check C4 "a no-op write records no audit event" 1 \
  "$(jqr "$R" "len([e for e in d['events'] if e['kind']=='field'])")"

R=$(json PATCH "/tracker/issues/$TARGET" '{"assignee":""}')
check C5 "an empty string clears a field" "None" "$(jqr "$R" "d['assignee']")"

check C7 "an unknown PATCH field is refused, not silently ignored" 422 \
  "$(status PATCH "/tracker/issues/$TARGET" '{"project_id":"aegix"}')"
check C7b "a misspelled field is refused too" 422 \
  "$(status PATCH "/tracker/issues/$TARGET" '{"assigne":"terra"}')"

R=$(json PATCH "/tracker/issues/$TARGET" '{"status":"closed"}')
check C8 "closing stamps closed_at" "True" "$(jqr "$R" "d['closed_at'] is not None")"
R=$(json PATCH "/tracker/issues/$TARGET" '{"status":"open"}')
check C9 "reopening clears closed_at" "None" "$(jqr "$R" "d['closed_at']")"

json PATCH "/tracker/issues/$TARGET" '{"status":"resolved"}' >/dev/null
R=$(json GET "/tracker/issues?project_id=cao-system&open_only=true&q=editable")
check C10 "resolved still counts as open" 1 "$(jqr "$R" "d['total']")"

check C13 "an invalid severity is refused with the accepted values" 400 \
  "$(status POST /tracker/issues '{"project_id":"cao-system","title":"x","severity":"P9"}')"

# ---------------------------------------------------------------------------
say "D. Search, filters, listing"

json POST /tracker/issues '{"project_id":"cao-system","title":"needle-title"}' >/dev/null
json POST /tracker/issues '{"project_id":"cao-system","title":"by-command","failing_command":"conduct spawn --lane x"}' >/dev/null
json POST /tracker/issues '{"project_id":"cao-system","title":"by-body","body":"a distinctive haystack phrase"}' >/dev/null

check D1a "search matches a title" 1 "$(jqr "$(json GET '/tracker/issues?q=needle-title')" "d['total']")"
check D1b "search matches a failing command" 1 "$(jqr "$(json GET '/tracker/issues?q=conduct%20spawn')" "d['total']")"
check D1c "search matches a body" 1 "$(jqr "$(json GET '/tracker/issues?q=distinctive%20haystack')" "d['total']")"
check D1d "search matches a key" 1 "$(jqr "$(json GET '/tracker/issues?q=cond-0242')" "d['total']")"

BK=$(jqr "$(json POST /tracker/issues '{"project_id":"cao-system","title":"blocked one"}')" "d['key']")
json PATCH "/tracker/issues/$BK" '{"status":"blocked"}' >/dev/null
BOTH=$(jqr "$(json GET '/tracker/issues?project_id=cao-system&status=blocked&status=triage')" "d['total']")
ONE=$(jqr "$(json GET '/tracker/issues?project_id=cao-system&status=blocked')" "d['total']")
check D2 "repeated status params are an OR" "1 1" "$BOTH $ONE"
check D2b "a comma list is refused rather than silently matching nothing" 400 \
  "$(status GET '/tracker/issues?status=blocked,triage')"

json POST /tracker/issues '{"project_id":"cao-system","title":"ui one","labels":["ui"]}' >/dev/null
json POST /tracker/issues '{"project_id":"cao-system","title":"ui two","labels":["ui-polish"]}' >/dev/null
check D3 "label filtering does not over-match a longer label" "['ui one']" \
  "$(jqr "$(json GET '/tracker/issues?project_id=cao-system&label=ui')" "[i['title'] for i in d['issues']]")"

R=$(json GET '/tracker/issues?project_id=cao-system&limit=2')
check D5 "total is the unpaged count" "True" "$(jqr "$R" "d['total'] > len(d['issues'])")"
check D6 "an out-of-range limit is refused, not silently truncated" 422 \
  "$(status GET '/tracker/issues?limit=100000')"
check D6b "the applied limit is echoed back" 500 \
  "$(jqr "$(json GET '/tracker/issues?limit=500')" "d['limit']")"

json POST /tracker/issues '{"project_id":"cao-system","title":"critical","severity":"P0"}' >/dev/null
check D7 "severity order puts P0 first" "P0" \
  "$(jqr "$(json GET '/tracker/issues?project_id=cao-system&order=severity')" "d['issues'][0]['severity']")"

json POST /tracker/issues '{"project_id":"cao-system","title":"composed","severity":"P1","component":"conduct"}' >/dev/null
json POST /tracker/issues '{"project_id":"cao-system","title":"decoy","severity":"P3","component":"conduct"}' >/dev/null
check D8 "filters compose" "['composed']" \
  "$(jqr "$(json GET '/tracker/issues?project_id=cao-system&severity=P1&component=conduct')" "[i['title'] for i in d['issues']]")"

check D9 "listing is scoped to one project" "True" \
  "$(jqr "$(json GET '/tracker/issues?project_id=aegix')" "all(i['project_id']=='aegix' for i in d['issues'])")"

# ---------------------------------------------------------------------------
say "E. Comments, links, audit"

X=$(jqr "$(json POST /tracker/issues '{"project_id":"cao-system","title":"link source"}')" "d['key']")
Y=$(jqr "$(json POST /tracker/issues '{"project_id":"cao-system","title":"link target"}')" "d['key']")

check E1 "a comment is accepted" 201 \
  "$(status POST "/tracker/issues/$X/comments" '{"body":"reproduced on main","author":"colin"}')"
R=$(json GET "/tracker/issues/$X")
check E1b "the comment is stored and audited" "True" \
  "$(jqr "$R" "len(d['comments'])==1 and any(e['kind']=='comment' for e in d['events'])")"

check E2 "an empty comment is refused" 400 \
  "$(status POST "/tracker/issues/$X/comments" '{"body":"   "}')"

LINK=$(json POST "/tracker/issues/$X/links" "$(printf '{"to_key":"%s","kind":"blocks"}' "$Y")")
LINK_ID=$(jqr "$LINK" "d['id']")
check E3 "a link is visible from both issues" "1 1" \
  "$(jqr "$(json GET "/tracker/issues/$X")" "len(d['links'])") $(jqr "$(json GET "/tracker/issues/$Y")" "len(d['links'])")"

R=$(json POST "/tracker/issues/$X/links" "$(printf '{"to_key":"%s","kind":"blocks"}' "$Y")")
check E4 "a duplicate link is idempotent" "False" "$(jqr "$R" "d['created']")"

check E5 "a link to a missing issue is refused" 404 \
  "$(status POST "/tracker/issues/$X/links" '{"to_key":"cond-9999","kind":"relates"}')"

status DELETE "/tracker/issues/$X" >/dev/null
check E7 "deleting an issue removes its links from the other side" 0 \
  "$(jqr "$(json GET "/tracker/issues/$Y")" "len(d['links'])")"

R=$(json GET "/tracker/issues/$TARGET")
check E8 "the audit trail names an actor" "True" \
  "$(jqr "$R" "any(e['actor']=='colin' for e in d['events'])")"

# ---------------------------------------------------------------------------
say "F. Project lifecycle"

check F1 "deleting a project holding issues is refused" 409 "$(status DELETE /tracker/projects/aegix)"
check F2 "force deletes the issues too" 200 "$(status DELETE '/tracker/projects/aegix?force=true')"

status PATCH /tracker/projects/cao-system '{"status":"archived"}' >/dev/null
check F3a "an archived project leaves the default list" "[]" \
  "$(jqr "$(json GET /tracker/projects)" "[p['id'] for p in d]")"
check F3b "its issues remain searchable" "True" \
  "$(jqr "$(json GET '/tracker/issues?project_id=cao-system')" "d['total'] > 0")"
status PATCH /tracker/projects/cao-system '{"status":"active"}' >/dev/null

status PATCH /tracker/projects/cao-system '{"name":"CAO System (renamed)"}' >/dev/null
check F4 "renaming leaves the id and keys alone" "cao-system cond-0242" \
  "$(jqr "$(json GET /tracker/projects/cao-system)" "d['id']") $(jqr "$(json GET /tracker/issues/cond-0242)" "d['key']")"

# ---------------------------------------------------------------------------
say "G. Ledger migration (against the real 208-entry corpus)"

LEDGER_REPO="${CAO_LEDGER_REPO:-$HOME/Projects/cao-conductor-worktrees/fire-marshal-p1}"
if [[ -f "$LEDGER_REPO/OPEN_ISSUES.md" && -f "$LEDGER_REPO/CLOSED_ISSUES.md" ]]; then
  # Keys are unique across the installation and the ledger carries explicit
  # cond-NNNN ids, so the section-B project holding cond-0001..0244 must go
  # first. Its prefix must also be released before ledger-import claims it.
  status DELETE '/tracker/projects/cao-system?force=true' >/dev/null
  status POST /tracker/projects '{"name":"Ledger Import","id":"ledger-import","issue_prefix":"cond"}' >/dev/null
  IMPORT=$(CAO_STATE_ROOT="$CAO_STATE_ROOT" PYTHONPATH="$REPO_ROOT/src" "$VENV_PY" - "$LEDGER_REPO" <<'PY'
import json, sys
from cli_agent_orchestrator.services import issue_ledger_import as imp
from cli_agent_orchestrator.services import issue_tracker as tracker

root = sys.argv[1]
out = {}
for name, default in (("CLOSED_ISSUES.md", "closed"), ("OPEN_ISSUES.md", "open")):
    text = open(f"{root}/{name}", encoding="utf-8").read()
    headings = sum(1 for line in text.splitlines() if line.startswith("## cond-"))
    report = imp.import_ledger(f"{root}/{name}", project_id="ledger-import", default_status=default)
    out[name] = {"headings": headings, "parsed": report["parsed"], "imported": report["imported"]}

issues = tracker.list_issues(project_id="ledger-import", limit=500)["issues"]
by_key = {i["key"]: i for i in issues}
out["total"] = len(issues)
out["severities"] = sorted({i["severity"] for i in issues})
out["title_prefix_leaks"] = [i["key"] for i in issues if i["title"].startswith(("P0", "P1", "P2", "P3", "P4", "[P"))]
out["stamped_today"] = [i["key"] for i in issues if (i["created_at"] or "").startswith("2026-08-07")]
out["deferred"] = sorted(i["key"] for i in issues if "deferred" in i["labels"])
out["p0"] = sorted(i["key"] for i in issues if i["severity"] == "P0")
out["cond_0200"] = by_key.get("cond-0200", {}).get("severity")
out["cond_0114"] = "absent in the markdown ledger" in (by_key.get("cond-0114", {}).get("body") or "")
out["cond_0010_note"] = "filed note" in (by_key.get("cond-0010", {}).get("body") or "")
out["preserved_fields"] = sum(
    1 for i in issues if "preserved from the markdown ledger" in (i["body"] or "")
)
out["evidence_none"] = by_key.get("cond-0025", {}).get("evidence")
rerun = imp.import_ledger(f"{root}/OPEN_ISSUES.md", project_id="ledger-import")
out["rerun"] = {"imported": rerun["imported"], "skipped": rerun["skipped"]}
print(json.dumps(out))
PY
)
  check G1 "all 208 entries import" 208 "$(jqr "$IMPORT" "d['total']")"
  check G2a "OPEN headings all parsed" "True" "$(jqr "$IMPORT" "d['OPEN_ISSUES.md']['headings']==d['OPEN_ISSUES.md']['imported']")"
  check G2b "CLOSED headings all parsed" "True" "$(jqr "$IMPORT" "d['CLOSED_ISSUES.md']['headings']==d['CLOSED_ISSUES.md']['imported']")"
  check G3 "severity before the dash is read (cond-0200)" "P2" "$(jqr "$IMPORT" "d['cond_0200']")"
  check G4 "no title keeps a severity prefix" "[]" "$(jqr "$IMPORT" "d['title_prefix_leaks']")"
  check G5 "P0 survives as P0" 2 "$(jqr "$IMPORT" "len(d['p0'])")"
  check G6 "a trailing author note is preserved (cond-0010)" "True" "$(jqr "$IMPORT" "d['cond_0010_note']")"
  check G7 "the entry with no filed line says so (cond-0114)" "True" "$(jqr "$IMPORT" "d['cond_0114']")"
  check G8 "one-off ledger fields are preserved in the body" "True" \
    "$(jqr "$IMPORT" "d['preserved_fields'] > 0")"
  check G10 "deferred entries stay open and are labelled" 3 "$(jqr "$IMPORT" "len(d['deferred'])")"
  check G12 "(none given) evidence becomes null" "None" "$(jqr "$IMPORT" "d['evidence_none']")"
  check G13 "re-running the import skips rather than duplicates" "0 80" \
    "$(jqr "$IMPORT" "str(d['rerun']['imported'])+' '+str(d['rerun']['skipped'])")"
  check G15 "only the dateless entry carries today's date" 1 "$(jqr "$IMPORT" "len(d['stamped_today'])")"

  curl -s -o "$WORK/export.md" -w '%{content_type}' \
    "$BASE/tracker/projects/ledger-import/export" > "$WORK/export.ctype"
  check I11a "the export is served as markdown" "text/markdown; charset=utf-8" \
    "$(cat "$WORK/export.ctype")"
  check I11b "the export renders one heading per open issue" 80 \
    "$(grep -cE '^## cond-[0-9]+ — ' "$WORK/export.md")"
  check I11c "severities are rendered in the heading" "True" \
    "$(grep -qE '^## cond-[0-9]+ — \[P[0-4]\] ' "$WORK/export.md" && echo True || echo False)"
else
  printf '  \033[33mskip\033[0m G*   ledger corpus not found at %s\n' "$LEDGER_REPO"
fi

# ---------------------------------------------------------------------------
say "J. Isolation"

check J1 "the verification DB lives under the isolated state root" "True" \
  "$([[ -f "$CAO_STATE_ROOT/db/cli-agent-orchestrator.db" ]] && echo True || echo False)"

LIVE_DB="$HOME/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db"
if [[ -f "$LIVE_DB" ]]; then
  # Counts ROWS, not tables. Empty tracker tables in the live database are an
  # artifact of running the CAO test suite without CAO_STATE_ROOT: `init_db`
  # calls `create_all`, which creates every model registered on Base. That is
  # pre-existing suite behaviour and harmless — the deployed server never reads
  # them. Leaked DATA is the thing that would not be harmless.
  LIVE_ROWS=$("$VENV_PY" - "$LIVE_DB" <<'PY'
import sqlite3, sys
try:
    conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tracker_%'"
        )
    ]
    print(sum(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables))
except Exception:
    print("unreadable")
PY
)
  # Asserted as a DELTA, not an absolute. The absolute was a property of the
  # machine rather than of the run: the moment the tracker is actually adopted —
  # the point of shipping it — the live install legitimately holds rows and this
  # check went red for everyone, forever. A green result whose lifetime is
  # "until somebody uses the feature" is worse than no check.
  check J2 "this run leaked no rows into the live install" "$LIVE_ROWS_BEFORE" "$LIVE_ROWS"
else
  printf '  \033[33mskip\033[0m J2   no live database at %s\n' "$LIVE_DB"
fi

# ---------------------------------------------------------------------------
printf '\n\033[1m%s passed, %s failed\033[0m\n' "$PASS" "$FAIL"
[[ "$FAIL" == 0 ]]
