# src/glyph/libclang_loader.py
from __future__ import annotations
import os, subprocess, sys
from typing import Optional

def _brew_llvm_lib() -> Optional[str]:
    try:
        prefix = subprocess.check_output(["brew", "--prefix", "llvm"], text=True).strip()
        cand = os.path.join(prefix, "lib", "libclang.dylib")
        return cand if os.path.exists(cand) else None
    except Exception:
        return None

def _linux_candidates() -> list[str]:
    import glob
    cands = []
    cands += glob.glob("/usr/lib/llvm-*/lib/libclang.so")
    cands += glob.glob("/usr/lib/x86_64-linux-gnu/libclang*.so")
    cands += glob.glob("/usr/local/lib/libclang*.so")
    return sorted(cands)

def ensure() -> None:
    # Only set path; actual load happens when Index.create() is called by clients.
    try:
        from clang.cindex import Config  # importing module does NOT load lib
    except Exception:
        return  # clang python bindings not installed; caller will raise later

    p = os.environ.get("LIBCLANG_LIBRARY_FILE")
    if p and os.path.exists(p):
        try: Config.set_library_file(p); return
        except Exception: pass

    if sys.platform == "darwin":
        cand = _brew_llvm_lib()
        if cand:
            try: Config.set_library_file(cand); return
            except Exception: pass
    else:
        for cand in _linux_candidates():
            try:
                from clang.cindex import Config as _C
                _C.set_library_file(cand)
                return
            except Exception:
                continue
