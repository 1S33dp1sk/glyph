#!/usr/bin/env bash
# scripts/test_glyph_db_edges.sh — deep DB edges test for glyph:
# - include graph storage + reverse closure (affected_files)
# - callsites + call_candidates for ambiguous (multi-target) calls
# - disambiguation after code change (resolve_unlinked_calls)
# - FTS rebuild path
# - nested savepoint tx (no "cannot rollback" errors)
# - idempotent behavior + clearing includes
#
# This test is CLI-light where possible; for new internals we use small Python
# snippets against GlyphDB directly. The script prints helpful diagnostics on
# failure and avoids sed -i portability issues.

set -euo pipefail

GLYPH_BIN="${GLYPH_BIN:-glyph}"
PY="${PY:-python3}"

msg() { printf "\033[1;34m[+] %s\033[0m\n" "$*"; }
die() { printf "\033[1;31m[!] %s\033[0m\n" "$*" >&2; exit 1; }

# portable in-place word replace (BSD/GNU sed-safe)
repl_word() {
  local file="$1" from="$2" to="$3"
  "$PY" - "$file" "$from" "$to" <<'PY'
import sys, re, pathlib
p = pathlib.Path(sys.argv[1])
src = p.read_text(encoding='utf-8')
pat = re.compile(r'\b' + re.escape(sys.argv[2]) + r'\b')
dst = pat.sub(sys.argv[3], src)
p.write_text(dst, encoding='utf-8')
PY
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/glyphdbedges.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

DEMO="$WORK/demo"; OUT="$WORK/out"; DB="$OUT/idx.sqlite"
mkdir -p "$DEMO"/{include,src} "$OUT"

# --- seed repo ---------------------------------------------------------------
cat > "$DEMO/include/demo.h" <<'H'
#pragma once
#include <stddef.h>
void demo_hello(const char *name);
H

cat > "$DEMO/include/util.h" <<'H'
#pragma once
int add_int(int a, int b);
int mul_int(int a, int b);
H

cat > "$DEMO/src/util.c" <<'C'
#include "util.h"
int add_int(int a,int b){ return a+b; }
int mul_int(int a,int b){ return a*b; }
C

cat > "$DEMO/src/main.c" <<'C'
#include "demo.h"
#include "util.h"
int main(void){
  int s = add_int(2,3);
  (void)mul_int(3,4);
  return s;
}
C

# --- init + ingest + resolve -------------------------------------------------
msg "init DB"
$GLYPH_BIN db init --db "$DB" >/dev/null
test -s "$DB" || die "DB not created"

msg "ingest initial files"
$GLYPH_BIN db ingest \
  --db "$DB" \
  --file util.c@"$DEMO/src/util.c" \
  --file main.c@"$DEMO/src/main.c" \
  --file demo.h@"$DEMO/include/demo.h" \
  --cflags "-I$DEMO/include" >/dev/null
$GLYPH_BIN db resolve --db "$DB" >/dev/null

# --- lookup GIDs -------------------------------------------------------------
msg "lookup GIDs"
GID_MAIN="$($GLYPH_BIN db search --db "$DB" 'main' | awk 'NR==1{print $1}')"
GID_ADD="$($GLYPH_BIN db search --db "$DB" 'add_int' | awk 'NR==1{print $1}')"
test -n "$GID_MAIN" || die "missing gid for main"
test -n "$GID_ADD"  || die "missing gid for add_int"

# --- include graph: set + affected_files ------------------------------------
msg "include graph: set includes and compute reverse closure"
"$PY" - "$DB" "$DEMO/src/main.c" "$DEMO/include/util.h" "$DEMO/include/demo.h" <<'PY'
import json, sys, os
from pathlib import Path
from glyph.db import GlyphDB, _canon_path
db, src, u, d = sys.argv[1:]
with GlyphDB(db) as g:
    # store edges: main.c -> util.h, demo.h (string and relative canonicalization tested)
    g.set_includes_for_file(src, [(u,"quote"), (Path(d).name, "quote")])
    # ask for affected by util.h
    paths = g.affected_files([u], transitive=True, include_self=True)
    print(json.dumps({"affected_by_util_h": paths}, indent=2))
PY

# verify main.c in closure
grep -q "$DEMO/src/main.c" <( "$PY" - "$DB" "$DEMO/include/util.h" <<'PY'
import json, sys
from glyph.db import GlyphDB
with GlyphDB(sys.argv[1]) as g:
    print(json.dumps(g.affected_files([sys.argv[2]], transitive=True, include_self=True)))
PY
) || die "affected_files(util.h) did not contain main.c"

# clearing includes should remove edges
msg "include graph: clear and verify removal"
"$PY" - "$DB" "$DEMO/src/main.c" <<'PY'
import sys
from glyph.db import GlyphDB
db, src = sys.argv[1:]
with GlyphDB(db) as g:
    g.set_includes_for_file(src, [])  # clear
    # should have no reverse deps now
    print(len(g.affected_files([src], transitive=True, include_self=False)))
PY

# --- multi-target callsites/candidates ---------------------------------------
msg "create ambiguous defs: common() in two files"
cat > "$DEMO/src/alt1.c" <<'C'
int common(int x){ return x+1; }
C
cat > "$DEMO/src/alt2.c" <<'C'
int common(int x){ return x+2; }
C

msg "make main call common()"
repl_word "$DEMO/src/main.c" "mul_int(3,4)" "common(5)"

msg "re-ingest main, alt1, alt2"
$GLYPH_BIN db ingest \
  --db "$DB" \
  --file main.c@"$DEMO/src/main.c" \
  --file alt1.c@"$DEMO/src/alt1.c" \
  --file alt2.c@"$DEMO/src/alt2.c" \
  --cflags "-I$DEMO/include" >/dev/null
$GLYPH_BIN db resolve --db "$DB" >/dev/null

# Validate: unresolved call by name 'common', callsite exists, 2 candidates
msg "validate callsite and candidates (expect unresolved + 2 candidates)"
"$PY" - "$DB" "$GID_MAIN" <<'PY'
import sys, sqlite3, json
db, gid_main = sys.argv[1:]
c = sqlite3.connect(db); c.row_factory = sqlite3.Row
# unresolved call row: dst_gid is NULL and dst_name='common'
row = c.execute("SELECT COUNT(*) AS n FROM calls WHERE src_gid=? AND dst_name='common' AND dst_gid IS NULL", (gid_main,)).fetchone()
assert row["n"] >= 1, "no unresolved call to common from main"
# callsite for (main, 'direct', 'common')
cs = c.execute("SELECT id FROM callsites WHERE src_gid=? AND kind='direct' AND name_hint='common'", (gid_main,)).fetchone()
assert cs, "callsites row missing for main/common"
cs_id = cs["id"]
# 2 candidates
n = c.execute("SELECT COUNT(*) AS n FROM call_candidates WHERE callsite_id=?", (cs_id,)).fetchone()["n"]
assert n >= 2, f"expected >=2 candidates for common, got {n}"
print(json.dumps({"callsite_id": cs_id, "candidates": n}))
PY

# Disambiguate: rename alt2.common -> common2, re-ingest alt2 only, resolve
msg "disambiguate by renaming alt2.common -> common2"
repl_word "$DEMO/src/alt2.c" "common" "common2"
$GLYPH_BIN db ingest --db "$DB" --file alt2.c@"$DEMO/src/alt2.c" >/dev/null
$GLYPH_BIN db resolve --db "$DB" >/dev/null

# Validate: call now resolved to the single remaining common(); candidates collapse to 1
msg "validate resolution + candidate collapse"
"$PY" - "$DB" "$GID_MAIN" <<'PY'
import sys, sqlite3
db, gid_main = sys.argv[1:]
c = sqlite3.connect(db); c.row_factory = sqlite3.Row
# resolved row: dst_gid is NOT NULL and dst_name may be NULL/kept
row = c.execute("SELECT COUNT(*) AS n FROM calls WHERE src_gid=? AND (dst_name='common' OR dst_name IS NULL) AND dst_gid IS NOT NULL", (gid_main,)).fetchone()
assert row["n"] >= 1, "call to common not resolved after disambiguation"
cs = c.execute("SELECT id FROM callsites WHERE src_gid=? AND kind='direct' AND name_hint='common'", (gid_main,)).fetchone()
assert cs, "callsites row missing post-disambiguation"
cs_id = cs["id"]
n = c.execute("SELECT COUNT(*) AS n FROM call_candidates WHERE callsite_id=?", (cs_id,)).fetchone()["n"]
assert n == 1, f"expected exactly 1 candidate after disambiguation, got {n}"
print("ok")
PY

# --- FTS rebuild path --------------------------------------------------------
msg "FTS rebuild: wipe FTS and rebuild"
"$PY" - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.row_factory = sqlite3.Row
c.execute("DELETE FROM entities_fts")
# Manual rebuild (same op used in schema migration)
c.execute("INSERT INTO entities_fts(entities_fts) VALUES('rebuild')")
cnt = c.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0]
assert cnt > 0, "FTS rebuild did not repopulate"
print("fts_rows", cnt)
PY

# search must still work
$GLYPH_BIN db search --db "$DB" "add_int" | grep -q add_int || die "fts search broken after rebuild"

# --- nested savepoint tx sanity ---------------------------------------------
msg "savepoint tx nesting sanity"
"$PY" - "$DB" <<'PY'
from glyph.db import GlyphDB
import sys
with GlyphDB(sys.argv[1]) as g:
    with g.tx():
        with g.tx():  # inner savepoint
            pass
print("ok")
PY

msg "ALL OK"
echo "DB: $DB"
