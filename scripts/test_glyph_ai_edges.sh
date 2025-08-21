#!/usr/bin/env bash
# scripts/test_glyph_ai_edges.sh — deeper AI/RAG coverage with tricky cases

set -euo pipefail
GLYPH_BIN="${GLYPH_BIN:-glyph}"

msg() { printf "\033[1;34m[+] %s\033[0m\n" "$*"; }
die() { printf "\033[1;31m[!] %s\033[0m\n" "$*" >&2; exit 1; }

# --- prerequisites ---
if ! command -v ollama >/dev/null 2>&1; then
  echo "skip: no ollama"; exit 0
fi
if ! ollama list 2>/dev/null | grep -q 'gpt-oss:20b'; then
  echo "skip: model gpt-oss:20b not present"; exit 0
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/glyphai.edge.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
DEMO="$WORK/demo"; DB="$WORK/idx.sqlite"
mkdir -p "$DEMO"/{src,include}

# --- corpus with tricky bits ---
cat > "$DEMO/include/util.h" <<'H'
#pragma once
int add_int(int a,int b);
int mul_int(int a,int b);
int only_decl(int a,int b); // prototype only, no definition to test "unknown"
#define INC(x) ((x)+1)
typedef int (*binop_t)(int,int);
int apply(binop_t f, int a, int b);
H

cat > "$DEMO/src/util.c" <<'C'
#include "util.h"
int add_int(int a,int b){ return a+b; }
int mul_int(int a,int b){ return a*b; }
int apply(binop_t f, int a, int b){ return f(a,b); }
C

# --- DB: init + ingest + resolve ---
msg "init db"
$GLYPH_BIN db init --db "$DB" >/dev/null

msg "ingest entire corpus"
$GLYPH_BIN db ingest --db "$DB" \
  --file util.c@"$DEMO/src/util.c" \
  --file util.h@"$DEMO/include/util.h" >/dev/null

$GLYPH_BIN db resolve --db "$DB" >/dev/null

# helper to ask and assert
ask() {
  local q="$1"
  $GLYPH_BIN ai ask --db "$DB" "$q"
}

# 1) add_int → must mention 'add_int' and 'a+b'
msg "ai: add_int behavior"
ANS="$(ask 'What does add_int do?')"
echo "$ANS" | grep -qi "add_int" || die "add_int not mentioned"
echo "$ANS" | grep -Eiq "a\s*\+\s*b" || die "a+b not identified"

# 2) mul_int → must mention 'mul_int' and 'a*b'
msg "ai: mul_int behavior"
ANS="$(ask 'What does mul_int do?')"
echo "$ANS" | grep -qi "mul_int" || die "mul_int not mentioned"
echo "$ANS" | grep -Eiq "a\s*\*\s*b" || die "a*b not identified"

# 3) macro INC(x) → must mention 'INC' and '+1'
msg "ai: macro INC"
ANS="$(ask 'What does INC(x) do?')"
echo "$ANS" | grep -qi "\bINC\b" || die "INC not mentioned"
echo "$ANS" | grep -Eiq "\+\s*1" || die "INC +1 not identified"

# 4) prototype-only only_decl → answer should say unknown / prototype / not defined
msg "ai: prototype-only only_decl"
ANS="$(ask 'What does only_decl do?')"
echo "$ANS" | grep -Eiq "unknown|prototype|declar|no definition|not defined" \
  || die "only_decl should be reported as unknown/prototype"

# 5) function-pointer adapter apply → must reference function pointer or f(a,b)
msg "ai: function-pointer adapter apply"
ANS="$(ask 'What does apply do?')"
echo "$ANS" | grep -Eiq "function pointer|callback|f\s*\(\s*a\s*,\s*b\s*\)" \
  || die "apply behavior not identified (function pointer / f(a,b))"

# 6) verbose trace: show the model output clearly (stdout)
msg "ai: verbose run (prints output)"
GLYPH_INTEL_VERBOSE=1 ANS="$(ask 'Summarize add_int and apply briefly.')"
printf "%s\n" "$ANS"

msg "ALL OK"
