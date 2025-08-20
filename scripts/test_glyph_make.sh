#!/usr/bin/env bash
# scripts/test_glyph_make.sh — verify make-based scanning (rules, cd/chain, pattern)

set -euo pipefail
GLYPH_BIN="${GLYPH_BIN:-glyph}"

msg() { printf "\033[1;34m[+] %s\033[0m\n" "$*"; }
die() { printf "\033[1;31m[!] %s\033[0m\n" "$*" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/glyphmake.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
DEMO="$WORK/demo"; DB="$WORK/idx.sqlite"; MIR="$WORK/mirror"
mkdir -p "$DEMO/src/dir" "$DEMO/include" "$MIR"

# --- demo sources -------------------------------------------------------------
cat > "$DEMO/include/util.h" <<'H'
#pragma once
int add_int(int a,int b);
int mul_int(int a,int b);
H

cat > "$DEMO/src/util.c" <<'C'
#include "util.h"
int add_int(int a,int b){ return a+b; }
int mul_int(int a,int b){ return a*b; }
C

cat > "$DEMO/src/dir/sub.c" <<'C'
#include "util.h"
int sub_add3(int x){ return add_int(x,3); }
C

cat > "$DEMO/src/dir/alt.c" <<'C'
#include "util.h"
int alt_use(int x){ return mul_int(x,2); }
C

# --- Makefile exercising patterns, cd && chain, flags -------------------------
cat > "$DEMO/Makefile" <<'MK'
# Variables + flags
CC ?= cc
CFLAGS += -Iinclude -O2 -Wall -Wextra -DMK=1

SRC := src/util.c src/dir/sub.c
OBJ := $(SRC:.c=.o)

.PHONY: all build-sub
all: $(OBJ) build-sub

# Pattern rule (simple)
src/%.o: src/%.c
	$(CC) $(CFLAGS) -c $< -o $@

# Chained rule using cd && ... ; ...
build-sub:
	cd src && $(CC) $(CFLAGS) -c dir/alt.c -o dir/alt.o ; echo "built alt"
MK

# --- run scan via make -nB ----------------------------------------------------
msg "scan (make -nB)"
glyph db init --db "$DB" >/dev/null
$GLYPH_BIN scan --root "$DEMO" --db "$DB" --mirror "$MIR" --make "make -nB" >/dev/null

# --- assertions ---------------------------------------------------------------
msg "mirror contains markers"
grep -q "/* GLYPH:S " "$MIR/src/util.c"      || die "util.c not rewritten"
grep -q "/* GLYPH:S " "$MIR/src/dir/sub.c"   || die "sub.c not rewritten"
grep -q "/* GLYPH:S " "$MIR/src/dir/alt.c"   || die "alt.c not rewritten"

msg "db indexed functions"
glyph db search --db "$DB" 'add_int'   | grep -q . || die "missing add_int"
glyph db search --db "$DB" 'mul_int'   | grep -q . || die "missing mul_int"
glyph db search --db "$DB" 'sub_add3'  | grep -q . || die "missing sub_add3"
glyph db search --db "$DB" 'alt_use'   | grep -q . || die "missing alt_use"

msg "counts sane"
python3 - "$DB" <<'PY' || exit 1
import sqlite3, sys
c=sqlite3.connect(sys.argv[1])
f=c.execute("select count(*) from files").fetchone()[0]
e=c.execute("select count(*) from entities").fetchone()[0]
assert f>=3 and e>=4, (f,e)
print("OK")
PY

msg "ALL OK"
echo "DB: $DB"
echo "MIRROR: $MIR"
