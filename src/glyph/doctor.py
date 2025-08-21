# src/glyph/doctor.py
from __future__ import annotations

import os
import sys
import time
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Tuple

@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""

def _ok(name: str, detail: str = "") -> CheckResult:
    return CheckResult(name, True, detail)

def _fail(name: str, detail: str) -> CheckResult:
    return CheckResult(name, False, detail)

def _run_cmd(cmd: list[str], cwd: Path | None = None, env: dict | None = None, timeout: int = 600) -> Tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=timeout,
        check=False,
    )
    return p.returncode, p.stdout, p.stderr

def _project_root() -> Path:
    # src/glyph/doctor.py -> src -> repo root
    return Path(__file__).resolve().parents[2]

def check_python() -> CheckResult:
    v = sys.version.split()[0]
    parts = tuple(int(x) for x in v.split(".")[:2])
    if parts >= (3, 10):
        return _ok("python", v)
    return _fail("python", f"{v} (<3.10)")

def check_cli() -> CheckResult:
    # Prefer running the module to stay inside current venv
    rc, out, err = _run_cmd([sys.executable, "-m", "glyph", "--version"])
    if rc == 0 and out.strip():
        return _ok("glyph_cli", out.strip())
    return _fail("glyph_cli", err.strip() or "version check failed")

def check_sqlite_fts5() -> CheckResult:
    try:
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        c.close()
        return _ok("sqlite_fts5", sqlite3.sqlite_version)
    except Exception as e:
        return _fail("sqlite_fts5", repr(e))

def check_libclang() -> CheckResult:
    try:
        from .libclang_loader import ensure as _ensure
        _ensure()
        from clang.cindex import Index
        # best-effort path report
        path = os.environ.get("LIBCLANG_LIBRARY_FILE", "<resolver default>")
        Index.create()  # force load
        return _ok("libclang", f"loaded via {path}")
    except Exception as e:
        return _fail("libclang", repr(e))

def check_typer() -> CheckResult:
    try:
        import typer  # noqa: F401
        return _ok("typer", "ok")
    except Exception as e:
        return _fail("typer", repr(e))

def _list_test_scripts(scripts_dir: Path) -> List[Path]:
    out: List[Path] = []
    if scripts_dir.is_dir():
        for p in sorted(scripts_dir.iterdir()):
            if p.is_file() and p.name.startswith("test_") and os.access(p, os.X_OK):
                out.append(p)
    return out

def run_scripts() -> List[CheckResult]:
    root = _project_root()
    scripts_dir = root / "scripts"
    scripts = _list_test_scripts(scripts_dir)
    results: List[CheckResult] = []
    env = os.environ.copy()

    # Ensure GLYPH_BIN resolves to a single executable token (no spaces)
    glyph_path = shutil.which("glyph") or "glyph"
    env["GLYPH_BIN"] = glyph_path

    for script in scripts:
        t0 = time.monotonic()
        rc, out, err = _run_cmd([str(script)], cwd=root, env=env)
        dt = f"{time.monotonic() - t0:.2f}s"
        name = f"script:{script.name}"
        if rc == 0:
            results.append(_ok(name, dt))
        else:
            tail = (err or out).splitlines()[-20:]
            results.append(_fail(name, dt + " | " + "\n".join(tail)))
    return results

def run(verbose: bool = False) -> int:
    checks: List[Callable[[], CheckResult]] = [
        check_python,
        check_cli,
        check_typer,
        check_sqlite_fts5,
        check_libclang,
    ]
    results: List[CheckResult] = [fn() for fn in checks]
    # Only run test_* scripts that ship with glyph repo; this is part of glyph’s own health
    results.extend(run_scripts())

    ok_all = all(r.ok for r in results)
    for r in results:
        prefix = "✔" if r.ok else "✖"
        line = f"{prefix} {r.name}"
        if verbose or r.detail:
            line += f" — {r.detail}"
        print(line)
    print("OK" if ok_all else "FAIL")
    return 0 if ok_all else 1
