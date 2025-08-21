#!/usr/bin/env bash
# scripts/test_glyph_ai.sh — minimal AI/RAG smoke

set -euo pipefail
GLYPH_BIN="${GLYPH_BIN:-glyph}"

# skip if no ollama or model
if ! command -v ollama >/dev/null 2>&1; then
  echo "skip: no ollama"; exit 0
fi
if ! ollama list 2>/dev/null | grep -q 'gpt-oss:20b'; then
  echo "skip: model gpt-oss:20b not present"; exit 0
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/glyphai.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
DEMO="$WORK/demo"; DB="$WORK/idx.sqlite"
mkdir -p "$DEMO"/{src,include}

cat > "$DEMO/include/util.h" <<'H'
#pragma once
int add_int(int a,int b);
H
cat > "$DEMO/src/util.c" <<'C'
#include "util.h"
int add_int(int a,int b){ return a+b; }
C

$GLYPH_BIN db init --db "$DB" >/dev/null
$GLYPH_BIN db ingest --db "$DB" \
  --file util.c@"$DEMO/src/util.c" \
  --file util.h@"$DEMO/include/util.h" >/dev/null
$GLYPH_BIN db resolve --db "$DB" >/dev/null

ANS="$($GLYPH_BIN ai ask --db "$DB" "What does add_int do?" | tr -d '\r')"
echo "$ANS" | grep -qi "add_int" || { echo "[!] AI did not mention add_int"; exit 1; }
echo "$ANS" | grep -Eiq "a\s*\+\s*b" || { echo "[!] AI did not identify a+b"; exit 1; }

echo "OK"
