# src/glyph/gitvc.py
from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
import textwrap


# ----- helpers ---------------------------------------------------------------

def _run(cmd: list[str], cwd: str | os.PathLike[str]) -> str:
    r = subprocess.run(cmd, cwd=str(cwd), check=True, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.stdout.strip()

def _write_exe(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    st = path.stat()
    path.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

def _repo_root(root: str | os.PathLike[str]) -> Path:
    rp = Path(root).resolve()
    _run(["git", "rev-parse", "--git-dir"], cwd=rp)
    return rp

def _git_head_short(root: Path) -> str:
    try:
        return _run(["git", "rev-parse", "--short", "HEAD"], cwd=root)
    except Exception:
        return "0000000"

# ----- results ---------------------------------------------------------------

@dataclass(frozen=True)
class PlanResult:
    branch: str
    pre_commit: str
    post_merge: str
    db_path: str
    mirror_dir: str

# ----- public API ------------------------------------------------------------

def plan_branch(
    root: str | os.PathLike[str],
    branch: str,
    base: Optional[str],
    *,
    db_path: str = ".glyph/idx.sqlite",
    mirror_dir: str = ".glyph/mirror",
    make_cmd: Optional[str] = None,
    cflags: Optional[str] = None,
    strict_hooks: bool = True,
) -> PlanResult:
    """
    - switch/create branch
    - ensure .glyph/{idx.sqlite, mirror}/
    - install robust pre-commit (resolve, count unresolved; enforce if strict)
    - install post-merge (noop placeholder)
    """
    rp = _repo_root(root)

    exists = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=rp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0
    if exists:
        _run(["git", "switch", branch], cwd=rp)
    else:
        if base:
            _run(["git", "fetch", "--all", "--tags"], cwd=rp)
            _run(["git", "switch", "-c", branch, base], cwd=rp)
        else:
            _run(["git", "switch", "-c", branch], cwd=rp)

    glyph_dir = rp / ".glyph"
    dbp = rp / db_path if not os.path.isabs(db_path) else Path(db_path)
    mir = rp / mirror_dir if not os.path.isabs(mirror_dir) else Path(mirror_dir)
    hooks = rp / ".git" / "hooks"

    glyph_dir.mkdir(parents=True, exist_ok=True)
    mir.mkdir(parents=True, exist_ok=True)

    # init DB through CLI to guarantee schema
    if not dbp.exists():
        subprocess.run(["glyph", "db", "init", "--db", str(dbp)], cwd=rp, check=True)

    # persist hints
    hint = glyph_dir / "scan.hint"
    lines: list[str] = []
    if make_cmd is not None:
        lines.append(f"make_cmd={make_cmd}")
    if cflags is not None:
        lines.append(f"cflags={cflags}")
    if lines:
        hint.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # pre-commit: resolve and count unresolved; enforce if strict
    pre_commit = hooks / "pre-commit"
    pre_commit_content = textwrap.dedent(f"""\
    #!/usr/bin/env bash
    set -euo pipefail
    db="{str(dbp)}"
    strict="{ '1' if strict_hooks else '0' }"

    # try to resolve what we can (idempotent)
    glyph db resolve --db "$db" >/dev/null 2>&1 || true

    # count unresolved via sqlite3, else python
    if command -v sqlite3 >/dev/null 2>&1; then
      unresolved="$(sqlite3 "$db" 'SELECT count(*) FROM calls WHERE dst_gid IS NULL;' 2>/dev/null || echo 0)"
    else
      tmp="$(mktemp)"
      python3 - "$db" >"$tmp" <<'PY'
    import sqlite3, sys
    c = sqlite3.connect(sys.argv[1])
    print(c.execute("SELECT count(*) FROM calls WHERE dst_gid IS NULL;").fetchone()[0])
    PY
      unresolved="$(cat "$tmp")"
      rm -f "$tmp"
    fi

    # print to STDERR so stdout stays clean (important when callers capture output)
    echo "unresolved_calls=$unresolved" >&2

    # keep braces literal for bash parameter expansion
    if [ "$strict" = "1" ] && [ "${{unresolved:-0}}" -gt 0 ]; then
      echo "glyph: unresolved calls > 0 (strict)" >&2
      exit 1
    fi
    exit 0
    """)
    _write_exe(pre_commit, pre_commit_content)

    # post-merge: placeholder
    post_merge = hooks / "post-merge"
    post_merge_content = """#!/usr/bin/env bash
set -euo pipefail
# reserved for future rescans/mirror updates
exit 0
"""
    _write_exe(post_merge, post_merge_content)

    # gitattributes: sqlite as binary
    gattr = rp / ".gitattributes"
    try:
        current = gattr.read_text(encoding="utf-8", errors="ignore") if gattr.exists() else ""
        line = "*.sqlite binary\n"
        if line not in current:
            gattr.write_text(current + ("" if current.endswith("\n") or not current else "\n") + line, encoding="utf-8")
    except Exception:
        pass

    return PlanResult(
        branch=branch,
        pre_commit=str(pre_commit),
        post_merge=str(post_merge),
        db_path=str(dbp),
        mirror_dir=str(mir),
    )

def tag_db_snapshot(root: str | os.PathLike[str], db_path: str, *, prefix: str = "glyph/db") -> str:
    rp = _repo_root(root)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    head = _git_head_short(rp)
    tag = f"{prefix}/{ts}-{head}"
    msg = f"glyph DB snapshot\n\nfile: {db_path}\nhead: {head}\nuts: {ts}\n"
    _run(["git", "tag", "-a", "-f", tag, "-m", msg], cwd=rp)
    return tag

def apply_snapshot(
    root: str | os.PathLike[str],
    *,
    db_path: str = ".glyph/idx.sqlite",
    mirror_dir: str = ".glyph/mirror",
    message: str = "glyph: snapshot",
    tag_prefix: str = "glyph/db",
) -> str:
    rp = _repo_root(root)
    dbp = Path(db_path)
    mir = Path(mirror_dir)
    paths: list[str] = []

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(rp))
        except ValueError:
            return str(p)

    paths.append(_rel(dbp))
    paths.append(_rel(mir))
    paths.append(".glyph")

    subprocess.run(["git", "add", "-A", *paths], cwd=rp, check=False)
    subprocess.run(["git", "commit", "-qm", message, "--allow-empty"], cwd=rp, check=True)
    return tag_db_snapshot(rp, paths[0], prefix=tag_prefix)

def push_with_tags(root: str | os.PathLike[str], *, remote: str = "origin", branch: Optional[str] = None) -> None:
    rp = _repo_root(root)
    if branch is None:
        branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=rp)
    subprocess.run(["git", "push", remote, branch], cwd=rp, check=True)
    subprocess.run(["git", "push", "--tags"], cwd=rp, check=True)
