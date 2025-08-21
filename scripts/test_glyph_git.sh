#!/usr/bin/env bash
# scripts/test_glyph_git_edge.sh — thorough git integration + hooks edge cases

set -euo pipefail
GLYPH_BIN="${GLYPH_BIN:-glyph}"

msg() { printf "\033[1;34m[+] %s\033[0m\n" "$*"; }
die() { printf "\033[1;31m[!] %s\033[0m\n" "$*" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/glyphgit.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

git init -q
git config user.email test@example.com
git config user.name  glyph-test

mkdir -p src include
cat > include/util.h <<'H'
#pragma once
int add_int(int a,int b);
H
cat > src/util.c <<'C'
#include "util.h"
int add_int(int a,int b){ return a+b; }
C
cat > src/main.c <<'C'
#include "util.h"
// unresolved call on purpose:
int ghost(int); // comment this line to increase severity
int main(void){
  int s = add_int(1,2);
  s += ghost(3); // unresolved unless a definition exists somewhere
  return s;
}
C
git add .
git commit -qm "base: minimal project with unresolved call"

# plan strict branch + verify hooks/DB
msg "plan strict branch"
$GLYPH_BIN git plan --branch glyph/test --strict >/dev/null
test -x .git/hooks/pre-commit || die "pre-commit hook missing/executable bit off"
test -f .glyph/idx.sqlite     || die "DB not created by plan"

# ingest + resolve and verify unresolved present
msg "ingest and verify unresolved"
$GLYPH_BIN db ingest \
  --db .glyph/idx.sqlite \
  --file util.c@src/util.c \
  --file util.h@include/util.h \
  --file main.c@src/main.c >/dev/null
$GLYPH_BIN db resolve --db .glyph/idx.sqlite >/dev/null

GID_MAIN="$($GLYPH_BIN db search --db .glyph/idx.sqlite main | awk 'NR==1{print $1}')"
test -n "$GID_MAIN" || die "main gid not found"
$GLYPH_BIN db callees --db .glyph/idx.sqlite "$GID_MAIN" | grep -q '<unresolved:ghost>' \
  || die "expected unresolved ghost in callees(main)"

# strict pre-commit SHOULD block commit when unresolved > 0
msg "pre-commit strict should block commit"
echo "// touch" >> src/main.c
git add -A
set +e
git commit -qm "should be blocked by hook"
rc=$?
set -e
if [[ $rc -eq 0 ]]; then
  die "strict pre-commit did NOT block unresolved commit (bug: hook must count unresolved, not resolved)"
fi
echo "blocked OK"

# fix unresolved; re-ingest; commit must pass
msg "fix unresolved and commit"
cat >> src/util.c <<'C'

// define ghost to resolve the call
int ghost(int x){ return x+10; }
C
$GLYPH_BIN db ingest \
  --db .glyph/idx.sqlite \
  --file util.c@src/util.c >/dev/null
$GLYPH_BIN db resolve --db .glyph/idx.sqlite >/dev/null
$GLYPH_BIN db callees --db .glyph/idx.sqlite "$GID_MAIN" | grep -vq '<unresolved:ghost>' \
  || die "ghost still unresolved after fix"

git add -A
git commit -qm "resolved: ghost now defined"

# --- apply snapshot; ensure .glyph is staged/committed and tag exists
msg "apply snapshot + tag"
TAG="$($GLYPH_BIN git apply --message 'glyph: mirror+db')"
test -n "$TAG" || die "no tag returned by apply"
git rev-parse -q --verify "$TAG^{}" >/dev/null || die "snapshot tag missing"
git tag -l 'glyph/db/*' | grep -q . || die "tag not listed by glob"
git ls-files -s .glyph/idx.sqlite | grep -q . || die "sqlite not tracked"
test -d .glyph/mirror && echo "mirror dir present"

msg "ALL OK"
