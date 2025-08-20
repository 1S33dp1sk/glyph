#!/usr/bin/env bash
# scripts/dev.sh — bootstrap editable dev env for glyph (Linux/macOS, libclang-safe)

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

# --- helpers ---------------------------------------------------------------

_echo() { printf '%s\n' "$*"; }
die()   { printf '\033[1;31m[!] %s\033[0m\n' "$*" >&2; exit 1; }

find_libclang_linux() {
  # Prefer /usr/lib/llvm-*/lib/libclang.so, then multiarch, then resolve symlinks
  local cand
  cand="$(ls -1 /usr/lib/llvm-*/lib/libclang.so 2>/dev/null | sort -V | tail -n1 || true)"
  [[ -z "$cand" ]] && cand="$(ls -1 /usr/lib/*/libclang-*.so* 2>/dev/null | sort -V | tail -n1 || true)"
  [[ -z "$cand" ]] && cand="$(command -v ldconfig >/dev/null 2>&1 && ldconfig -p | awk '/libclang-.*\.so/{print $NF}' | sort -V | tail -n1 || true)"
  [[ -n "$cand" ]] && readlink -f "$cand" || true
}

libclang_major_from_path() {
  # Extract major version (18 from .../llvm-18/... or libclang-18.so.1)
  local p="$1"
  [[ "$p" =~ llvm-([0-9]+) ]] && { echo "${BASH_REMATCH[1]}"; return; }
  [[ "$p" =~ libclang-([0-9]+)\.so ]] && { echo "${BASH_REMATCH[1]}"; return; }
  # Unknown → empty
  echo ""
}

python_probe() {
  # Args: LIBFILE (optional; empty to let clang.cindex resolve)
  local libfile="${1:-}"
  python - "$libfile" <<'PY'
import os, sys
from clang.cindex import Config, Index, LibclangError
libfile = sys.argv[1]
try:
    if libfile:
        Config.set_library_file(libfile)
    idx = Index.create()
    print("OK")
except LibclangError as e:
    print("ERR", str(e))
    sys.exit(2)
except Exception as e:
    print("ERR", repr(e))
    sys.exit(2)
PY
}

install_clang_bindings_for_major() {
  # pin python-clang to detected MAJOR
  local major="$1"
  if [[ -n "$major" ]]; then
    pip install -U "clang>=${major},<$(("$major"+1))" >/dev/null
  else
    pip install -U "clang>=16" >/dev/null
  fi
}

wheel_libclang_path() {
  python - <<'PY'
import sys, importlib.util as u, pathlib as p
spec = u.find_spec('libclang')
if not spec or not spec.submodule_search_locations:
    print("")
    sys.exit(0)
base = p.Path(list(spec.submodule_search_locations)[0])
# common wheel layout: libclang/lib/libclang.so (linux) or .dylib (mac)
cands = [base/'lib'/'libclang.so', base/'lib'/'libclang.dylib']
for c in cands:
    if c.exists():
        print(str(c))
        sys.exit(0)
print("")
PY
}

# --- ensure clang Python bindings present ---------------------------------

if ! python - <<'PY'
import importlib.util as u, sys
sys.exit(0 if u.find_spec("clang") else 1)
PY
then
  pip install "clang>=16"
fi

# --- resolve libclang path + align python bindings -------------------------

UNAME_S="$(uname -s)"
LIBFILE=""
MAJOR=""

if [[ "$UNAME_S" == "Darwin" ]]; then
  if [[ -z "${LIBCLANG_LIBRARY_FILE:-}" ]]; then
    if command -v brew >/dev/null 2>&1; then
      prefix="$(brew --prefix llvm 2>/dev/null || true)"
      if [[ -n "$prefix" && -f "$prefix/lib/libclang.dylib" ]]; then
        LIBFILE="$prefix/lib/libclang.dylib"
      fi
    fi
  else
    LIBFILE="$LIBCLANG_LIBRARY_FILE"
  fi
elif [[ "$UNAME_S" == "Linux" ]]; then
  if [[ -n "${LIBCLANG_LIBRARY_FILE:-}" ]]; then
    LIBFILE="$LIBCLANG_LIBRARY_FILE"
  else
    LIBFILE="$(find_libclang_linux || true)"
  fi
  [[ -n "$LIBFILE" ]] && MAJOR="$(libclang_major_from_path "$LIBFILE")"
fi

# align python-clang to system libclang major if known
install_clang_bindings_for_major "$MAJOR"

# try system lib first
if [[ -n "$LIBFILE" ]]; then
  export LIBCLANG_LIBRARY_FILE="$LIBFILE"
  _echo "libclang: $LIBCLANG_LIBRARY_FILE"
fi

PROBE_OUT="$(python_probe "${LIBCLANG_LIBRARY_FILE:-}")" || true
if [[ "${PROBE_OUT%%$'\n'*}" != "OK" ]]; then
  # If mismatch (undefined symbol), fall back to wheel-provided libclang matching bindings
  _echo "$PROBE_OUT"
  # Try to match python-clang major if we have it; else default latest compatible
  if [[ -z "$MAJOR" ]]; then
    # guess from installed python-clang
    MAJOR="$(python - <<'PY'
import pkgutil, pkg_resources, re
ver = None
try:
    d = pkg_resources.get_distribution("clang").version
    m = re.match(r"^(\d+)\.", d)
    ver = m.group(1) if m else None
except Exception:
    pass
print(ver or "")
PY
)"
  fi
  pip install -U "libclang${MAJOR:+==}${MAJOR:+${MAJOR}.*}" >/dev/null || pip install -U libclang >/dev/null
  LIBFROMWHEEL="$(wheel_libclang_path || true)"
  if [[ -n "$LIBFROMWHEEL" && -f "$LIBFROMWHEEL" ]]; then
    export LIBCLANG_LIBRARY_FILE="$LIBFROMWHEEL"
    _echo "libclang (wheel): $LIBCLANG_LIBRARY_FILE"
    PROBE_OUT="$(python_probe "$LIBCLANG_LIBRARY_FILE")" || true
  fi
fi

if [[ "${PROBE_OUT%%$'\n'*}" != "OK" ]]; then
  die "libclang unusable. Hint: on Linux, install llvm (e.g. sudo apt install llvm-18 libclang-18-dev) or rely on the libclang wheel."
fi

# --- clean + install project ----------------------------------------------

pip uninstall -y glyph glyph-ai >/dev/null 2>&1 || true
[[ -f requirements-dev.txt ]] && pip install -r requirements-dev.txt
pip install -e .

# --- sanity checks ---------------------------------------------------------

python - <<'PY'
import importlib.util as u
assert u.find_spec('glyph'), 'glyph package not importable'
print('OK: glyph importable')
PY

glyph --version || { echo "ERR: console script not found"; exit 1; }

echo "✔ Dev env ready. Activate with:  source .venv/bin/activate"
echo "✔ Try: glyph --version"
[[ $REBUILD -eq 1 ]] && echo "Rebuild complete."
