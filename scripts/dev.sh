#!/usr/bin/env bash
# scripts/dev.sh — bootstrap editable dev env for glyph

set -euo pipefail

PY="python3"
RESET=0
REBUILD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) PY="$2"; shift 2;;
    --reset)  RESET=1; shift;;
    --rebuild) REBUILD=1; shift;;
    -h|--help)
      cat <<'USAGE'
Usage: scripts/dev.sh [--python PYBIN] [--reset] [--rebuild]
  --python   Choose Python binary (default: python3)
  --reset    Remove .venv before setup
  --rebuild  Reinstall deps into existing .venv
USAGE
      exit 0;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[[ $RESET -eq 1 ]] && rm -rf .venv
if [[ ! -d .venv ]]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

python -VV
pip install -U pip setuptools wheel

# Ensure clang Python bindings
if ! python - <<'PY'
import importlib.util as u, sys
sys.exit(0 if u.find_spec("clang") else 1)
PY
then
  pip install "clang>=16"
fi

# Locate libclang (macOS Homebrew first, then common Linux paths)
if [[ -z "${LIBCLANG_LIBRARY_FILE:-}" ]]; then
  if command -v brew >/dev/null 2>&1; then
    prefix="$(brew --prefix llvm 2>/dev/null || true)"
    if [[ -n "$prefix" && -f "$prefix/lib/libclang.dylib" ]]; then
      export LIBCLANG_LIBRARY_FILE="$prefix/lib/libclang.dylib"
      echo "libclang: $LIBCLANG_LIBRARY_FILE"
    fi
  fi
  if [[ -z "${LIBCLANG_LIBRARY_FILE:-}" ]]; then
    cand="$(ls -1 /usr/lib/llvm-*/lib/libclang.so 2>/dev/null | sort -V | tail -n1 || true)"
    [[ -z "$cand" ]] && cand="$(ls -1 /usr/lib/x86_64-linux-gnu/libclang*.so 2>/dev/null | sort -V | tail -n1 || true)"
    if [[ -n "$cand" && -f "$cand" ]]; then
      export LIBCLANG_LIBRARY_FILE="$cand"
      echo "libclang: $LIBCLANG_LIBRARY_FILE"
    fi
  fi
fi

# Verify libclang loadability (use Index.create(), not Config.library)
if ! python - <<'PY'
import os, sys
from clang.cindex import Config, Index
p=os.environ.get("LIBCLANG_LIBRARY_FILE")
if p: Config.set_library_file(p)
# Force load; raises LibclangError if not found/usable
Index.create()
print("OK: libclang loaded via", p if p else "<default resolver>")
PY
then
  echo "ERROR: libclang not found/loaded. On macOS: brew install llvm; then re-run." >&2
  exit 1
fi

# Clean conflicting installs, then editable install (src/ layout)
pip uninstall -y glyph glyph-ai >/dev/null 2>&1 || true
[[ -f requirements-dev.txt ]] && pip install -r requirements-dev.txt
pip install -e .

# Sanity checks
python - <<'PY'
import importlib.util as u
assert u.find_spec('glyph'), 'glyph package not importable'
print('OK: glyph importable')
PY

glyph --version || { echo "ERR: console script not found"; exit 1; }

echo "✔ Dev env ready. Activate with:  source .venv/bin/activate"
echo "✔ Try: glyph --version"
[[ $REBUILD -eq 1 ]] && echo "Rebuild complete."
