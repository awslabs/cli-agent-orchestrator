#!/bin/bash
# Fork feature manual test matrix. Every case is executed against the installed
# Claude Code binary. No case is inferred.
BASE=/private/tmp/forkmatrix-run
PROJ_DIR="$HOME/.claude/projects/-private-tmp-forkmatrix-run"
RESULTS=/tmp/forkmatrix/results.tsv
M=haiku
: > "$RESULTS"
rm -rf "$BASE"; mkdir -p "$BASE"
uuid() { uuidgen | tr 'A-Z' 'a-z'; }
lines() { wc -l < "$PROJ_DIR/$1.jsonl" 2>/dev/null | tr -d ' '; }
exists() { [ -f "$PROJ_DIR/$1.jsonl" ] && echo yes || echo no; }
rec() { printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >> "$RESULTS"; }

# ---- build the parent -------------------------------------------------------
cd "$BASE"
PARENT=$(uuid)
claude -p --session-id "$PARENT" --model $M "Remember the codeword QUARTZ-8. Reply OK." </dev/null >/dev/null 2>&1
P0=$(lines "$PARENT")
rec "A1" "parent with one completed turn is forkable" "$([ -n "$P0" ] && echo PASS || echo FAIL)" "parent transcript=$P0 lines"

# ---- A2: zero-turn parent ---------------------------------------------------
EMPTY=$(uuid); C=$(uuid)
OUT=$(claude -p --resume "$EMPTY" --fork-session --session-id "$C" --model $M "hi" </dev/null 2>&1 | head -c 120)
rec "A2" "fork of a zero-turn parent refuses" \
    "$(echo "$OUT" | grep -qi 'No conversation found' && echo PASS || echo FAIL)" \
    "$(echo "$OUT" | tr -d '\n' | cut -c1-70)"

# ---- B1: dictated id --------------------------------------------------------
C=$(uuid)
R=$(claude -p --resume "$PARENT" --fork-session --session-id "$C" --model $M --output-format json "Codeword? One word." </dev/null 2>&1)
GOT=$(echo "$R" | python3 -c "import json,sys;print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
ANS=$(echo "$R" | python3 -c "import json,sys;print(json.load(sys.stdin).get('result',''))" 2>/dev/null)
rec "B1" "destination id is dictated by caller" "$([ "$GOT" = "$C" ] && echo PASS || echo FAIL)" "asked=${C:0:8} got=${GOT:0:8}"
rec "B1b" "fork inherits parent context" "$(echo "$ANS" | grep -q QUARTZ && echo PASS || echo FAIL)" "answer=$ANS"
rec "B1c" "fork writes its own transcript" "$([ "$(exists "$C")" = yes ] && echo PASS || echo FAIL)" "child lines=$(lines "$C")"
rec "G1" "parent unchanged after fork" "$([ "$(lines "$PARENT")" = "$P0" ] && echo PASS || echo FAIL)" "before=$P0 after=$(lines "$PARENT")"

# ---- B3: fork onto its own id ----------------------------------------------
OUT=$(claude -p --resume "$PARENT" --fork-session --session-id "$PARENT" --model $M "Codeword?" </dev/null 2>&1 | head -c 150)
AFTER=$(lines "$PARENT")
rec "B3" "fork onto own id LEAVES PARENT INTACT" \
    "$([ "$AFTER" = "$P0" ] && echo PASS || echo 'FAIL-CORRUPTS-PARENT')" "before=$P0 after=$AFTER"
# B3 is destructive when it fails. Re-baseline so later cases measure themselves.
P0=$AFTER

# ---- B4: destination id collides with an existing session -------------------
VICTIM=$(uuid)
claude -p --session-id "$VICTIM" --model $M "Remember codeword ZEBRA-1. Reply OK." </dev/null >/dev/null 2>&1
V0=$(lines "$VICTIM")
OUT=$(claude -p --resume "$PARENT" --fork-session --session-id "$VICTIM" --model $M "Codeword? One word." </dev/null 2>&1)
VANS=$(echo "$OUT" | python3 -c "import json,sys;print(json.load(sys.stdin).get('result',''))" 2>/dev/null)
V1=$(lines "$VICTIM")
rec "B4" "fork onto a DIFFERENT existing id refuses" \
    "$(echo "$OUT" | grep -qi 'already in use' && [ "$V0" = "$V1" ] && echo PASS || echo FAIL)" \
    "victim ${V0}->${V1} lines, refused=$(echo "$OUT" | grep -qi 'already in use' && echo yes || echo no)"

# ---- B5: malformed destination id ------------------------------------------
OUT=$(claude -p --resume "$PARENT" --fork-session --session-id "not-a-uuid" --model $M "hi" </dev/null 2>&1 | head -c 120)
rec "B5" "malformed destination id refused" \
    "$(echo "$OUT" | grep -qiE 'invalid|uuid|error' && echo PASS || echo FAIL)" \
    "$(echo "$OUT" | tr -d '\n' | cut -c1-70)"

# ---- C2: concurrent forks ---------------------------------------------------
CC=""; for i in 1 2 3; do CC="$CC $(uuid)"; done
for c in $CC; do
  ( claude -p --resume "$PARENT" --fork-session --session-id "$c" --model $M --output-format json "Codeword? One word." </dev/null 2>&1 \
    | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('session_id','')+'|'+str(d.get('result','')))" >> /tmp/forkmatrix/conc.txt 2>/dev/null ) &
done; wait
OKC=$(grep -c QUARTZ /tmp/forkmatrix/conc.txt 2>/dev/null)
UNIQ=$(cut -d'|' -f1 /tmp/forkmatrix/conc.txt 2>/dev/null | sort -u | wc -l | tr -d ' ')
rec "C2" "3 concurrent forks inherit and stay distinct" \
    "$([ "$OKC" = 3 ] && [ "$UNIQ" = 3 ] && echo PASS || echo FAIL)" "inherited=$OKC distinct_ids=$UNIQ"
rec "G2" "parent unchanged after concurrent forks" "$([ "$(lines "$PARENT")" = "$P0" ] && echo PASS || echo FAIL)" "before=$P0 after=$(lines "$PARENT")"

# ---- D1: fork of a fork -----------------------------------------------------
F1=$(uuid)
claude -p --resume "$PARENT" --fork-session --session-id "$F1" --model $M "Also remember TOPAZ-4. Reply OK." </dev/null >/dev/null 2>&1
F1L=$(lines "$F1")
F2=$(uuid)
R=$(claude -p --resume "$F1" --fork-session --session-id "$F2" --model $M --output-format json "Name both codewords." </dev/null 2>&1)
A2=$(echo "$R" | python3 -c "import json,sys;print(json.load(sys.stdin).get('result',''))" 2>/dev/null)
rec "D1" "fork of a fork inherits BOTH generations" \
    "$(echo "$A2" | grep -q QUARTZ && echo "$A2" | grep -q TOPAZ && echo PASS || echo FAIL)" "answer=$(echo $A2|cut -c1-60)"
rec "D1b" "intermediate fork unchanged by its own child" "$([ "$(lines "$F1")" = "$F1L" ] && echo PASS || echo FAIL)" "before=$F1L after=$(lines "$F1")"

# ---- E2: different working directory ---------------------------------------
mkdir -p "$BASE/elsewhere"; cd "$BASE/elsewhere"
C=$(uuid)
R=$(claude -p --resume "$PARENT" --fork-session --session-id "$C" --model $M --output-format json "Codeword? One word." </dev/null 2>&1)
A=$(echo "$R" | python3 -c "import json,sys;print(json.load(sys.stdin).get('result',''))" 2>/dev/null)
rec "E2" "fork from a DIFFERENT cwd inherits context" "$(echo "$A" | grep -q QUARTZ && echo PASS || echo FAIL)" "answer=$A"
cd "$BASE"

# ---- H1: fork with a different model than the parent ------------------------
C=$(uuid)
R=$(claude -p --resume "$PARENT" --fork-session --session-id "$C" --model opus --output-format json "Codeword? One word." </dev/null 2>&1)
A=$(echo "$R" | python3 -c "import json,sys;print(json.load(sys.stdin).get('result',''))" 2>/dev/null)
MODEL=$(echo "$R" | python3 -c "import json,sys;print(json.load(sys.stdin).get('modelUsage',{}) or '')" 2>/dev/null | head -c 60)
rec "H1" "fork may run a different model than parent" "$(echo "$A" | grep -q QUARTZ && echo PASS || echo FAIL)" "answer=$A"

# ---- G3: parent still usable itself after being forked many times -----------
R=$(claude -p --resume "$PARENT" --fork-session --session-id "$(uuid)" --model $M --output-format json "Codeword? One word." </dev/null 2>&1)
A=$(echo "$R" | python3 -c "import json,sys;print(json.load(sys.stdin).get('result',''))" 2>/dev/null)
rec "G3" "parent still forkable after N forks" "$(echo "$A" | grep -q QUARTZ && echo PASS || echo FAIL)" "answer=$A final_parent_lines=$(lines "$PARENT")"
