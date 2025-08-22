#!/usr/bin/env bash
# scripts/test_glyph_plan.sh — end-to-end planner validation focused on OUTPUT
# - Repo with an unresolved symbol (inc_int)
# - plan.json encodes the goal and success criteria
# - Verify status before/after executing the plan (edits)
# - Check impact output shape
# - (Optional) AI plan proposal: schema-only checks

set -euo pipefail

GLYPH_BIN="${GLYPH_BIN:-glyph}"

msg() { printf "\033[1;34m[+] %s\033[0m\n" "$*"; }
die() { printf "\033[1;31m[!] %s\033[0m\n" "$*" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/glyphplan.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
export WORK

DEMO="$WORK/demo"; DB="$WORK/idx.sqlite"
mkdir -p "$DEMO"/{src,include}

# --- seed repo: unresolved 'inc_int' referenced from main ---
cat > "$DEMO/include/util.h" <<'H'
#pragma once
int add_int(int a,int b);
/* NOTE: inc_int intentionally missing to start */
H

cat > "$DEMO/src/util.c" <<'C'
#include "util.h"
int add_int(int a,int b){ return a+b; }
/* NOTE: inc_int intentionally missing to start */
C

cat > "$DEMO/src/main.c" <<'C'
#include "util.h"
int main(void){
  int s = add_int(1,2);
  s = inc_int(s); /* unresolved until implemented */
  return s;
}
C

# --- init + ingest + resolve ---
msg "init & ingest"
$GLYPH_BIN db init --db "$DB" >/dev/null
$GLYPH_BIN db ingest --db "$DB" \
  --file util.c@"$DEMO/src/util.c" \
  --file util.h@"$DEMO/include/util.h" \
  --file main.c@"$DEMO/src/main.c" >/dev/null
$GLYPH_BIN db resolve --db "$DB" >/dev/null

# Confirm unresolved present for 'inc_int'
GID_MAIN="$($GLYPH_BIN db search --db "$DB" main | awk 'NR==1{print $1}')"
test -n "$GID_MAIN" || die "main gid not found"
$GLYPH_BIN db callees --db "$DB" "$GID_MAIN" | grep -q '<unresolved:inc_int>' \
  || die "expected unresolved inc_int in callees(main)"

# --- explain (deterministic path) with robust diagnostics ---
msg "plan explain (json shape)"
EXPL_OUT="$WORK/explain.out.json"
EXPL_ERR="$WORK/explain.err.txt"

set +e
$GLYPH_BIN plan explain --db "$DB" --json >"$EXPL_OUT" 2>"$EXPL_ERR"
rc=$?
set -e

if [[ $rc -ne 0 ]]; then
  echo "[!] glyph plan explain failed (rc=$rc)" >&2
  echo "--- stdout (first 200 lines) ---" >&2
  sed -n '1,200p' "$EXPL_OUT" >&2 || true
  echo "--- stderr (first 200 lines) ---" >&2
  sed -n '1,200p' "$EXPL_ERR" >&2 || true
  die "plan explain failed"
fi

if [[ ! -s "$EXPL_OUT" ]]; then
  echo "[!] glyph plan explain produced empty stdout" >&2
  echo "--- stderr (first 200 lines) ---" >&2
  sed -n '1,200p' "$EXPL_ERR" >&2 || true
  die "explain json malformed"
fi

python3 - "$EXPL_OUT" <<'PY'
import json, sys
p = sys.argv[1]
with open(p, 'r', encoding='utf-8') as f:
    j = json.load(f)
assert "files" in j and isinstance(j["files"], int)
assert "entities_by_kind" in j and isinstance(j["entities_by_kind"], dict)
assert "unresolved_calls" in j and isinstance(j["unresolved_calls"], int)
PY

# --- static plan.json describing the intended change ---
PLAN="$WORK/plan.json"
cat > "$PLAN" <<'JSON'
{
  "goals": [
    "Implement inc_int(int) and wire it into main"
  ],
  "resources": [
    "glyph db, plan status",
    "sqlite fts5",
    "libclang-based ingest"
  ],
  "steps": [
    {
      "id": "S1",
      "title": "Add inc_int prototype",
      "deps": [],
      "rationale": "Expose function to callers",
      "expected_outcome": "util.h exports int inc_int(int)"
    },
    {
      "id": "S2",
      "title": "Implement inc_int in util.c",
      "deps": ["S1"],
      "rationale": "Provide actual behavior",
      "expected_outcome": "inc_int(x) returns x+1"
    },
    {
      "id": "S3",
      "title": "Re-ingest and resolve",
      "deps": ["S2"],
      "rationale": "Update index/calls",
      "expected_outcome": "No unresolved calls remain"
    }
  ],
  "risks": [
    {"risk": "Signature mismatch", "mitigation": "Keep header/source in sync"}
  ],
  "success_criteria": [
    "No unresolved calls in glyph DB"
  ],
  "open_questions": [],
  "score": 0
}
JSON

# --- status BEFORE executing plan: expect unresolved_ok = no ---
msg "plan status (before) — expect unresolved"
$GLYPH_BIN plan status --db "$DB" --plan "$PLAN" | tee "$WORK/status_before.json" | \
  grep -q '"unresolved_ok": "no"' || die "status BEFORE should report unresolved_ok=no"

# --- EXECUTE the plan (S1,S2,S3) ---
msg "execute steps S1,S2,S3"
# S1: add prototype
cat > "$DEMO/include/util.h" <<'H'
#pragma once
int add_int(int a,int b);
int inc_int(int x);
H
# S2: implement
cat > "$DEMO/src/util.c" <<'C'
#include "util.h"
int add_int(int a,int b){ return a+b; }
int inc_int(int x){ return x+1; }
C
# S3: re-ingest just util.* and resolve
$GLYPH_BIN db ingest --db "$DB" \
  --file util.c@"$DEMO/src/util.c" \
  --file util.h@"$DEMO/include/util.h" >/dev/null
$GLYPH_BIN db resolve --db "$DB" >/dev/null

# --- status AFTER executing plan: expect unresolved_ok = yes ---
msg "plan status (after) — expect resolved"
$GLYPH_BIN plan status --db "$DB" --plan "$PLAN" | tee "$WORK/status_after.json" | \
  grep -q '"unresolved_ok": "yes"' || die "status AFTER should report unresolved_ok=yes"

# --- plan impact (json shape) ---
msg "plan impact (json shape)"
test -f "$DB" || die "DB missing at $DB"

IMPACT_JSON="$WORK/impact.json"
IMPACT_LOG="$WORK/impact.log"

# capture both streams; ensure stdout is saved
if ! $GLYPH_BIN plan impact --db "$DB" --symbol inc_int --json --verbose >"$IMPACT_JSON" 2>"$IMPACT_LOG"; then
  echo "[!] glyph plan impact failed (non-zero rc)" >&2
  echo "---- impact.log (first 200 lines) ----" >&2
  sed -n '1,200p' "$IMPACT_LOG" >&2 || true
  die "plan impact failed"
fi

if [[ ! -s "$IMPACT_JSON" ]]; then
  echo "[!] bad JSON from plan impact (empty stdout)" >&2
  echo "---- impact.log (first 200 lines) ----" >&2
  sed -n '1,200p' "$IMPACT_LOG" >&2 || true
  die "plan impact produced no output"
fi

python3 - "$IMPACT_JSON" <<'PY'
import json, sys
p = sys.argv[1]
with open(p,'r',encoding='utf-8') as f:
    j = json.load(f)
assert j.get("target") == "inc_int"
assert "callers" in j and isinstance(j["callers"], dict)
ok_any = any(len(v) > 0 for v in j["callers"].values())
if not ok_any:
    print("no callers found in impact output", file=sys.stderr)
    sys.exit(2)
PY

# --- (Optional) AI propose: schema-only checks, non-deterministic content ---
if command -v ollama >/dev/null 2>&1 && ollama list 2>/dev/null | grep -q 'gpt-oss:20b'; then
  msg "plan propose (AI, schema checks only)"
  GOALS=$'1) Implement inc_int\n2) Ensure no unresolved calls'
  RES=$'sqlite fts5\nollama gpt-oss:20b\nlibclang'

  PROP_OUT="$WORK/propose.out.json"
  PROP_ERR="$WORK/propose.err.txt"

  set +e
  $GLYPH_BIN plan propose \
    --db "$DB" \
    --goals "$GOALS" \
    --resources "$RES" \
    --model gpt-oss:20b --endpoint http://localhost:11434 \
    --max-iters 3 --fallback-after 2 --fallback-threshold 70 \
    >"$PROP_OUT" 2>"$PROP_ERR"
  rc=$?
  set -e

  if [[ $rc -ne 0 ]]; then
    echo "[!] plan propose failed (rc=$rc)"
    echo "--- stdout (first 200) ---"; sed -n '1,200p' "$PROP_OUT" || true
    echo "--- stderr (first 200) ---"; sed -n '1,200p' "$PROP_ERR" || true
    die "plan propose failed"
  fi

  if [[ ! -s "$PROP_OUT" ]]; then
    echo "[!] plan propose produced empty output"
    echo "--- stderr (first 200) ---"; sed -n '1,200p' "$PROP_ERR" || true
    die "plan propose empty"
  fi

  # Validate it is JSON
  if ! python3 - "$PROP_OUT" <<'PY'
import json, sys; json.load(open(sys.argv[1], 'r', encoding='utf-8'))
PY
  then
    echo "[!] plan propose emitted non-JSON"
    echo "--- stdout (first 200) ---"; sed -n '1,200p' "$PROP_OUT" || true
    echo "--- stderr (first 200) ---"; sed -n '1,200p' "$PROP_ERR" || true
    die "AI plan failed schema check"
  fi

  # Schema sanity
  python3 - "$PROP_OUT" <<'PY'
import json, sys
j=json.load(open(sys.argv[1], 'r', encoding='utf-8'))
for k in ("goals","resources","steps","risks","success_criteria","open_questions"):
    assert k in j
assert isinstance(j["steps"], list) and len(j["steps"]) >= 1
PY

else
  msg "plan propose — skipped (no ollama/model)"
fi


msg "ALL OK"
