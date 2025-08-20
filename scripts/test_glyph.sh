#!/usr/bin/env bash
# scripts/test_glyph.sh — end-to-end + edge cases for glyph

set -euo pipefail

GLYPH_BIN="${GLYPH_BIN:-glyph}"
PY="${PY:-python3}"

msg() { printf "\033[1;34m[+] %s\033[0m\n" "$*"; }
die() { printf "\033[1;31m[!] %s\033[0m\n" "$*" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/glyphtest.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
DEMO="$WORK/demo"; OUT="$WORK/out"
mkdir -p "$DEMO"/{include,src,build} "$OUT"

# --- demo repo (self-contained) ---
cat > "$DEMO/CMakeLists.txt" <<'C'
cmake_minimum_required(VERSION 3.16)
project(demo C)
set(CMAKE_C_STANDARD 99)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
add_library(demo STATIC src/util.c src/netbuf.c src/macro_emit.c)
target_include_directories(demo PUBLIC include)
add_executable(demo_cli src/main.c)
target_link_libraries(demo_cli PRIVATE demo)
C
cat > "$DEMO/include/demo.h" <<'H'
#pragma once
#include <stddef.h>
#include <stdio.h>
typedef struct NetBuf { unsigned char *data; size_t used, cap; } NetBuf;
int  netbuf_put(NetBuf *b, const void *p, size_t n);
void demo_hello(const char *name);
H
cat > "$DEMO/include/util.h" <<'H'
#pragma once
int add_int(int a, int b);
int mul_int(int a, int b);
static inline int clamp_int(int x, int lo, int hi){ return x<lo?lo:(x>hi?hi:x); }
H
cat > "$DEMO/include/macros.h" <<'H'
#pragma once
#define DEFINE_HANDLER(NAME) int NAME##_handler(int x){ return x+1; }
#define MAGIC 42
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
cat > "$DEMO/src/netbuf.c" <<'C'
#include <string.h>
#include "demo.h"
void demo_hello(const char *name){ printf("hello, %s\n", name); }
int netbuf_put(NetBuf *b,const void *p,size_t n){
  if(!b||!p) return -1;
  if(b->used+n>b->cap) return -1;
  memcpy(b->data+b->used,p,n);
  b->used+=n;
  return (int)n;
}
C
cat > "$DEMO/src/macro_emit.c" <<'C'
#include "macros.h"
DEFINE_HANDLER(foo)
int call_macro(int x){ return foo_handler(x); }
C

cmake -S "$DEMO" -B "$DEMO/build" -DCMAKE_EXPORT_COMPILE_COMMANDS=ON >/dev/null
cmake --build "$DEMO/build" -j >/dev/null

# 1) rewrite: file mode + idempotence
msg "rewrite: markers + idempotence"
"$GLYPH_BIN" rewrite --file "$DEMO/src/util.c" --name util.c > "$OUT/util.marked.c"
grep -q "/* GLYPH:S " "$OUT/util.marked.c" || die "missing start marker"
grep -q "/* GLYPH:E " "$OUT/util.marked.c" || die "missing end marker"
S_IDS=$(grep -o 'GLYPH:S [A-Z0-9]\+' "$OUT/util.marked.c" | awk '{print $2}' | sort -u)
E_IDS=$(grep -o 'GLYPH:E [A-Z0-9]\+' "$OUT/util.marked.c" | awk '{print $2}' | sort -u)
diff -u <(printf '%s\n' "$S_IDS") <(printf '%s\n' "$E_IDS") >/dev/null || die "S/E ID mismatch"
"$GLYPH_BIN" rewrite --file - --name util.c < "$OUT/util.marked.c" > "$OUT/util.marked.2.c"
diff -u "$OUT/util.marked.c" "$OUT/util.marked.2.c" >/dev/null || die "rewrite not idempotent"

# 1b) ID stability under body whitespace changes
msg "rewrite: ID stability under body whitespace"
cp "$DEMO/src/util.c" "$OUT/util.body.c"
$PY - "$OUT/util.body.c" <<'PY'
import sys,re
p=sys.argv[1]
s=open(p,'r',encoding='utf-8').read()
s=re.sub(r'return\s*a\+b;', 'return  a +  b ;', s)
open(p,'w',encoding='utf-8').write(s)
PY
"$GLYPH_BIN" rewrite --file "$OUT/util.body.c" --name util.c > "$OUT/util.body.marked.c"
S1=$(grep -o 'GLYPH:S [A-Z0-9]\+' "$OUT/util.marked.c" | awk '{print $2}' | sort -u)
S2=$(grep -o 'GLYPH:S [A-Z0-9]\+' "$OUT/util.body.marked.c" | awk '{print $2}' | sort -u)
diff -u <(printf '%s\n' "$S1") <(printf '%s\n' "$S2") >/dev/null || die "IDs changed after body-only edits"

# 2) pack: JSONL validity + hdr/fn/pr/rc/call
msg "pack: JSONL valid + hdr/fn/pr/rc/call"
"$GLYPH_BIN" pack \
  --file util.c@"$DEMO/src/util.c" \
  --file main.c@"$DEMO/src/main.c" \
  --file demo.h@"$DEMO/include/demo.h" \
  --cflags "-I$DEMO/include" > "$OUT/pack_full.jsonl"
grep -q '"t":"hdr"' "$OUT/pack_full.jsonl" || die "pack missing hdr"
grep -q '"t":"fn"'  "$OUT/pack_full.jsonl" || die "pack missing fn"
grep -q '"t":"pr"'  "$OUT/pack_full.jsonl" || die "pack missing prototypes"
grep -q '"t":"rc"'  "$OUT/pack_full.jsonl" || die "pack missing record (struct/union/enum)"
$PY - "$OUT/pack_full.jsonl" <<'PY' || die "pack JSONL invalid"
import sys,json
ok=True
for i,l in enumerate(open(sys.argv[1],'r',encoding='utf-8'),1):
    try: json.loads(l)
    except Exception as e: print("bad json line",i,e); ok=False
print("OK") if ok else sys.exit(1)
PY

# 2b) storage classification
msg "pack: storage classification"
cat > "$DEMO/src/stor.c" <<'C'
static inline int si(int x){return x;}
static int st(int x){return x;}
inline int inl(int x){return x;}
int ex(int x){return x;}
C
"$GLYPH_BIN" pack --file stor.c@"$DEMO/src/stor.c" > "$OUT/pack_stor.jsonl"
grep -q '"s":"static_inline"' "$OUT/pack_stor.jsonl" || die "missing static_inline"
grep -q '"s":"static"'        "$OUT/pack_stor.jsonl" || die "missing static"
grep -q '"s":"inline"'        "$OUT/pack_stor.jsonl" || die "missing inline"
grep -q '"s":"extern"'        "$OUT/pack_stor.jsonl" || die "missing extern"

# 2c) macros: function-like only
msg "pack: function-like macros only"
"$GLYPH_BIN" pack --file macros.h@"$DEMO/include/macros.h" > "$OUT/pack_macros.jsonl"
grep -q '"t":"mc"' "$OUT/pack_macros.jsonl" || die "macro function-like not packed"
! grep -q 'MAGIC' "$OUT/pack_macros.jsonl" || die "constant macro wrongly packed"

# 2d) gaps: missing definitions when only headers given
msg "pack: gaps missing_def"
"$GLYPH_BIN" pack --file demo.h@"$DEMO/include/demo.h" > "$OUT/pack_hdr_only.jsonl"
grep -q '"k":"missing_def"' "$OUT/pack_hdr_only.jsonl" || die "missing_def gap not emitted"

# 2e) gaps: undefined refs when only main.c given
msg "pack: gaps undef_ref"
"$GLYPH_BIN" pack --file main.c@"$DEMO/src/main.c" --cflags "-I$DEMO/include" > "$OUT/pack_main_only.jsonl"
grep -q '"k":"undef_ref"' "$OUT/pack_main_only.jsonl" || die "undef_ref gap not emitted"

# 3) tree: summary keys exist
msg "tree: summary keys"
"$GLYPH_BIN" tree \
  --file util.c@"$DEMO/src/util.c" \
  --file main.c@"$DEMO/src/main.c" \
  --file demo.h@"$DEMO/include/demo.h" \
  --cflags "-I$DEMO/include" > "$OUT/tree.json"
$PY - "$OUT/tree.json" <<'PY' || die "tree summary malformed"
import sys,json
j=json.load(open(sys.argv[1],'r',encoding='utf-8'))
assert isinstance(j.get("files"), list)
assert isinstance(j.get("totals"), dict)
assert isinstance(j.get("modules"), dict)
print("OK")
PY

# 4) deps: has edges and expected symbol
msg "deps: edges present + add_int referenced"
"$GLYPH_BIN" deps --file "$DEMO/src/main.c" --name main.c --cflags "-I$DEMO/include" > "$OUT/deps.txt"
test -s "$OUT/deps.txt" || die "deps output empty"
grep -q '# add_int' "$OUT/deps.txt" || die "deps missing add_int edge"

# 5) stdin path + markers
msg "rewrite: stdin path"
cat "$DEMO/src/netbuf.c" | "$GLYPH_BIN" rewrite --file - --name netbuf.c > "$OUT/netbuf.marked.c"
grep -q "/* GLYPH:S " "$OUT/netbuf.marked.c" || die "stdin rewrite missing markers"

# 6) pre-marked: passthrough unchanged
msg "rewrite: pre-marked passthrough"
cat > "$OUT/pre_marked.c" <<'C'
/* GLYPH:S TESTID */
int foo(void){return 1;}
/* GLYPH:E TESTID */
C
"$GLYPH_BIN" rewrite --file "$OUT/pre_marked.c" --name pre_marked.c > "$OUT/pre_marked.out.c"
diff -u "$OUT/pre_marked.c" "$OUT/pre_marked.out.c" >/dev/null || die "pre-marked file should not change"

# 7) records: struct NetBuf present (always regenerate from known pack)
msg "pack: record contains NetBuf"
"$GLYPH_BIN" pack \
  --file util.c@"$DEMO/src/util.c" \
  --file main.c@"$DEMO/src/main.c" \
  --file demo.h@"$DEMO/include/demo.h" \
  --cflags "-I$DEMO/include" > "$OUT/pack_full.jsonl"
grep -q '"NetBuf"' "$OUT/pack_full.jsonl" || die "record name NetBuf missing"

# 8) empty file: no crash
msg "rewrite: empty file ok"
: | "$GLYPH_BIN" rewrite --file - --name empty.c > "$OUT/empty.out.c" || die "empty file crashed"

msg "ALL OK"
echo "Artifacts: $OUT"
