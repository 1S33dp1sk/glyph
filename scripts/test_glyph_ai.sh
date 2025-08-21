#!/usr/bin/env bash
# scripts/test_glyph_ai.sh — AI/RAG smoke + verbose trace

set -euo pipefail

GLYPH_BIN="${GLYPH_BIN:-glyph}"
MODEL="${GLYPH_MODEL:-gpt-oss:20b}"
ENDPOINT="${GLYPH_OLLAMA_ENDPOINT:-http://localhost:11434}"

msg() { printf "\033[1;34m[+] %s\033[0m\n" "$*"; }
die() { printf "\033[1;31m[!] %s\033[0m\n" "$*" >&2; exit 1; }
skip() { echo "skip: $1"; exit 0; }

# --- prerequisites ---
command -v ollama >/dev/null 2>&1 || skip "no ollama"
ollama list 2>/dev/null | grep -q "$MODEL" || skip "model $MODEL not present"

# --- workspace ---
WORK="$(mktemp -d "${TMPDIR:-/tmp}/glyphai.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
DEMO="$WORK/demo"; DB="$WORK/idx.sqlite"
mkdir -p "$DEMO"/{src,include}

# --- tiny project ---
cat > "$DEMO/include/util.h" <<'H'
#pragma once
int add_int(int a,int b);
H
cat > "$DEMO/src/util.c" <<'C'
#include "util.h"
int add_int(int a,int b){ return a+b; }
C

# --- index ---
msg "init db"
$GLYPH_BIN db init --db "$DB" >/dev/null

msg "ingest util.[ch]"
$GLYPH_BIN db ingest --db "$DB" \
  --file util.c@"$DEMO/src/util.c" \
  --file util.h@"$DEMO/include/util.h" >/dev/null
$GLYPH_BIN db resolve --db "$DB" >/dev/null

# --- query (normal) ---
msg "ai ask (normal)"
ANS="$($GLYPH_BIN ai ask --db "$DB" --model "$MODEL" --endpoint "$ENDPOINT" "What does add_int do?" | tr -d '\r')"
echo "$ANS" | grep -qi "add_int" || die "AI did not mention add_int"
echo "$ANS" | grep -Eiq "a\s*\+\s*b" || die "AI did not identify a+b"

# --- show the AI output on stdout (no extra model call) ---
msg "ai output (stdout)"
printf '%s\n' "$ANS"

# --- query (verbose; traces go to stderr log) ---
msg "ai ask (verbose trace)"
LOG="$WORK/ai_verbose.log"
GLYPH_INTEL_VERBOSE=1 \
  $GLYPH_BIN ai ask --db "$DB" --model "$MODEL" --endpoint "$ENDPOINT" "What does add_int do?" \
  >/dev/null 2>"$LOG"

grep -q '^\[intel\] seeds' "$LOG" || die "missing seeds trace"
grep -q 'prompt_preview' "$LOG"   || die "missing prompt_preview trace"
grep -q 'model_output_preview' "$LOG" || die "missing model_output_preview trace"

echo "OK"
