#!/usr/bin/env bash
# scripts/test_glyph_make.sh — verify make-based scanning

set -euo pipefail
GLYPH_BIN="${GLYPH_BIN:-glyph}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/glyphmake.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
DEMO="$WORK/demo"; DB="$WORK/idx.sqlite"; MIR="$WORK/mirror"
mkdir -p "$DEMO/src" "$DEMO/include"

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
cat > "$DEMO/Makefile" <<'MK'
CC ?= cc
CFLAGS
