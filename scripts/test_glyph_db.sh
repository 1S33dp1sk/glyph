#!/usr/bin/env bash
# scripts/test_glyph_db.sh — DB coverage for glyph

set -euo pipefail

GLYPH_BIN="${GLYPH_BIN:-glyph}"
PY="${PY:-python3}"

msg() { printf "\033[1;34m[+] %s\033[0m\n" "$*"; }
die() { printf "\033[1;31m[!] %s\033[0m\n" "$*" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/glyphdbtest.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

DEMO="$WORK/demo"; OUT="$WORK/out"; DB="$OUT/idx.sqlite"
mkdir -p "$DEMO"/{include,src} "$OUT"

# --- demo repo (no build required) ---
cat > "$DEMO/include/demo.h" <<'H'
#pragma once
#include <stddef.h>
#include <stdio.h>
typedef struct NetBuf { unsigned char *data; size_t used, cap; } NetBuf;
int  netbuf_put(struct NetBuf *b, const void *p, size_t n);
void demo_hello(const char *name);
H
cat > "$DEMO/include/util.h" <<'H'
#pragma once
int add_int(int a, int b);
int mul_int(int a, int b);
static inline int clamp_int(int x, int lo, int hi){ return x<lo?lo:(x>hi?hi:x); }
H
cat > "$DEMO/src/main.c" <<'C'
#include "demo.h"
#include "util.h"
int main(void){
  demo_hello("world");
  int s=add_int(2,3), m=mul_int(3,4);
  (void)clamp_int(s+m,0,100);
  return 0;
}
C
cat > "$DEMO/src/util.c" <<'C'
#include "util.h"
int add_int(int a,int b){ return a+b; }
int mul_int(int a,int b){ return a*b; }
C

# --- init DB ---
msg "db: init"
$GLYPH_BIN db init --db "$DB" >/dev/null
test -s "$DB" || die "DB not created"

# --- ingest set 1 ---
msg "db: ingest util.c, main.c, demo.h"
$GLYPH_BIN db ingest \
  --db "$DB" \
  --file util.c@"$DEMO/src/util.c" \
  --file main.c@"$DEMO/src/main.c" \
  --file demo.h@"$DEMO/include/demo.h" \
  --cflags "-I$DEMO/include" >/dev/null

# Link unresolved by unique names
$GLYPH_BIN db resolve --db "$DB" >/dev/null

# --- locate GIDs via FTS ---
msg "db: lookup gids"
GID_MAIN="$($GLYPH_BIN db search --db "$DB" 'main' | awk 'NR==1{print $1}')"
GID_ADD="$($GLYPH_BIN db search --db "$DB" 'add_int' | awk 'NR==1{print $1}')"
test -n "$GID_MAIN" || die "missing gid for main"
test -n "$GID_ADD"  || die "missing gid for add_int"

# --- show sanity ---
msg "db: show entity"
$GLYPH_BIN db show --db "$DB" "$GID_ADD" | grep -q 'add_int' || die "show missing decl"

# --- callers/callees after resolve ---
msg "db: callers/callees"
$GLYPH_BIN db callees --db "$DB" "$GID_MAIN" | grep -q "$GID_ADD" || die "callees(main) missing add_int"
$GLYPH_BIN db callers --db "$DB" "$GID_ADD" | grep -q "$GID_MAIN" || die "callers(add_int) missing main"

# --- counts snapshot ---
msg "db: count snapshot"
read -r FILES1 ENTS1 CALLS1 <<EOF
$($PY - "$DB" <<'PY'
import sqlite3, sys
c=sqlite3.connect(sys.argv[1])
def q(s): return c.execute(s).fetchone()[0]
print(q("select count(*) from files"),
      q("select count(*) from entities"),
      q("select count(*) from calls"))
PY
)
EOF

# --- idempotent re-ingest ---
msg "db: re-ingest idempotence"
$GLYPH_BIN db ingest \
  --db "$DB" \
  --file util.c@"$DEMO/src/util.c" \
  --file main.c@"$DEMO/src/main.c" \
  --file demo.h@"$DEMO/include/demo.h" \
  --cflags "-I$DEMO/include" >/dev/null
$GLYPH_BIN db resolve --db "$DB" >/dev/null
read -r FILES2 ENTS2 CALLS2 <<EOF
$($PY - "$DB" <<'PY'
import sqlite3, sys
c=sqlite3.connect(sys.argv[1])
def q(s): return c.execute(s).fetchone()[0]
print(q("select count(*) from files"),
      q("select count(*) from entities"),
      q("select count(*) from calls"))
PY
)
EOF
[[ "$FILES1 $ENTS1 $CALLS1" == "$FILES2 $ENTS2 $CALLS2" ]] || die "counts changed after re-ingest"

# --- mutate util.*: rename mul_int -> mul2 and re-ingest only util.* ---
msg "db: mutate util.* and re-ingest"
sed -i '' 's/mul_int/mul2/g' "$DEMO/src/util.c"
sed -i '' 's/mul_int/mul2/g' "$DEMO/include/util.h"
$GLYPH_BIN db ingest \
  --db "$DB" \
  --file util.c@"$DEMO/src/util.c" \
  --file util.h@"$DEMO/include/util.h" \
  --cflags "-I$DEMO/include" >/dev/null

# resolve again; now main still calls mul_int (unresolved)
$GLYPH_BIN db resolve --db "$DB" >/dev/null

# mul2 present, mul_int entity gone, callees(main) shows unresolved:mul_int
$GLYPH_BIN db search --db "$DB" 'mul2'   | grep -q . || die "mul2 not indexed"
! $GLYPH_BIN db search --db "$DB" 'mul_int' | grep -q . || die "stale mul_int entity remains"
$GLYPH_BIN db callees --db "$DB" "$GID_MAIN" | grep -q '<unresolved:mul_int>' || die "expected unresolved mul_int"

# --- vacuum + analyze ---
msg "db: vacuum/analyze"
$GLYPH_BIN db vacuum  --db "$DB" >/dev/null
$GLYPH_BIN db analyze --db "$DB" >/dev/null

# --- error path: show unknown gid (expect non-zero) ---
msg "db: show unknown gid"
set +e
$GLYPH_BIN db show --db "$DB" DEADBEAF 2>/dev/null
rc=$?
set -e
[[ $rc -ne 0 ]] || die "unknown gid should fail"

msg "ALL OK"
echo "DB: $DB"
