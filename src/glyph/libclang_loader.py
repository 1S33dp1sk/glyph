# src/glyph/libclang_loader.py
from __future__ import annotations

import glob
import os
import sys
from typing import Iterable, Optional


def _iter_env_overrides() -> Iterable[str]:
    p = os.environ.get("LIBCLANG_LIBRARY_FILE")
    if p:
        yield p
    sp = os.environ.get("LIBCLANG_SEARCH_PATH")
    if sp:
        for d in sp.split(os.pathsep):
            for name in ("libclang.so", "libclang.dylib"):
                yield os.path.join(d, name)
            yield from glob.glob(os.path.join(d, "libclang-*.so*"))


def _brew_candidates() -> Iterable[str]:
    try:
        import subprocess
        prefix = subprocess.check_output(["brew", "--prefix", "llvm"], text=True).strip()
    except Exception:
        return []
    c = os.path.join(prefix, "lib", "libclang.dylib")
    return [c] if os.path.exists(c) else []


def _xcode_candidates() -> Iterable[str]:
    # Command Line Tools / Xcode toolchain locations
    xs = [
        "/Library/Developer/CommandLineTools/usr/lib/libclang.dylib",
        "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/libclang.dylib",
    ]
    return [p for p in xs if os.path.exists(p)]


def _linux_candidates() -> Iterable[str]:
    globs = [
        "/usr/lib/llvm-*/lib/libclang.so",
        "/usr/lib/llvm-*/lib/libclang-*.so*",
        "/usr/lib/*-linux-gnu/libclang*.so*",
        "/usr/local/lib/libclang*.so*",
        "/usr/lib64/libclang*.so*",
        "/lib/*-linux-gnu/libclang*.so*",
    ]
    seen: set[str] = set()
    for pat in globs:
        for p in glob.glob(pat):
            rp = os.path.realpath(p)
            if rp not in seen and os.path.exists(rp):
                seen.add(rp)
                yield rp


def _ctypes_find() -> Iterable[str]:
    # May return a soname resolvable by the dynamic loader
    try:
        from ctypes.util import find_library

        s = find_library("clang")
        if s:
            yield s
    except Exception:
        return []


def _wheel_candidate() -> Iterable[str]:
    # PyPI libclang wheel bundles the shared lib
    try:
        import importlib.util as u
        spec = u.find_spec("libclang")
        if not spec or not spec.submodule_search_locations:
            return []
        base = list(spec.submodule_search_locations)[0]
        cands = (
            os.path.join(base, "lib", "libclang.so"),
            os.path.join(base, "lib", "libclang.dylib"),
        )
        return [p for p in cands if os.path.exists(p)]
    except Exception:
        return []


def _try_set(libpath: str) -> bool:
    try:
        from clang.cindex import Config, Index, LibclangError

        Config.set_library_file(libpath)
        # Validate compatibility by forcing registration
        Index.create()
        return True
    except Exception:
        return False


def _try_default() -> bool:
    # Let clang.cindex resolve via system search paths
    try:
        from clang.cindex import Index, LibclangError

        Index.create()
        return True
    except Exception:
        return False


def ensure() -> None:
    """
    Best-effort libclang resolver for macOS & Linux:
      1) Respect LIBCLANG_LIBRARY_FILE / LIBCLANG_SEARCH_PATH
      2) Homebrew LLVM (macOS), Xcode CLT
      3) Common Linux locations
      4) ctypes.util.find_library('clang')
      5) Bundled lib from PyPI 'libclang' wheel
      6) Fallback to clang.cindex default resolver
    Silent no-op if python 'clang' bindings are not installed.
    """
    try:
        import importlib.util as u

        if not u.find_spec("clang"):
            return
    except Exception:
        return

    # 1) env overrides
    for p in _iter_env_overrides():
        if _try_set(p):
            return

    # 2/3/4/5 platform candidates
    cands: list[str] = []
    if sys.platform == "darwin":
        cands.extend(_brew_candidates())
        cands.extend(_xcode_candidates())
        cands.extend(_wheel_candidate())
        cands.extend(_ctypes_find())
    else:
        cands.extend(_linux_candidates())
        cands.extend(_ctypes_find())
        cands.extend(_wheel_candidate())

    for p in cands:
        if _try_set(p):
            return

    # 6) default resolver (may succeed if system linker can find libclang)
    _try_default()
