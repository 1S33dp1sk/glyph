# src/glyph/mkparse.py
from __future__ import annotations
import os, re, shlex, subprocess
from pathlib import Path
from typing import Dict, List, Iterable

_CC_RX = re.compile(r"(?:^|\s)(cc|gcc|clang|clang\+\+|c\+\+|g\+\+)(?:\s|$)")
_SRC_RX = re.compile(r"\.(c|cc|cxx|cpp|C)$", re.IGNORECASE)

def _split_chained(cmd: str) -> Iterable[str]:
    # split on && and ; while respecting quotes
    parts: List[str] = []
    buf = []
    q = None
    i = 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            if c == q:
                q = None
            buf.append(c)
        else:
            if c in ("'", '"'):
                q = c
                buf.append(c)
            elif cmd.startswith("&&", i):
                s = "".join(buf).strip()
                if s: parts.append(s)
                buf = []; i += 1
            elif c == ";":
                s = "".join(buf).strip()
                if s: parts.append(s)
                buf = []
            else:
                buf.append(c)
        i += 1
    s = "".join(buf).strip()
    if s: parts.append(s)
    return parts

def _is_compile(argv: List[str]) -> bool:
    if not argv: return False
    if not _CC_RX.search(" ".join(argv)): return False
    if "-c" not in argv: return False
    return any(_SRC_RX.search(a) for a in argv)

def _src_from(argv: List[str], cwd: Path) -> Path | None:
    # prefer the last *.c* arg that is not following -o
    last = None
    skip = False
    it = iter(range(len(argv)))
    for i in it:
        a = argv[i]
        if skip:
            skip = False
            continue
        if a == "-o":
            skip = True
            continue
        if _SRC_RX.search(a):
            last = a
    if last:
        p = Path(last)
        return p if p.is_absolute() else (cwd / p).resolve()
    return None

def _args_for(argv: List[str], cwd: Path) -> List[str]:
    out: List[str] = []
    it = iter(range(len(argv)))
    skip = False
    for i in it:
        a = argv[i]
        if skip:
            skip = False
            continue
        if a in ("-o",):
            skip = True
            continue
        if a in ("-I", "-D", "-U", "-include", "-isystem", "-std", "-x"):
            # keep pair if separate
            if i + 1 < len(argv) and not argv[i+1].startswith("-"):
                out.extend([a, argv[i+1]])
                skip = True
            else:
                out.append(a)
            continue
        if a.startswith(("-I", "-D", "-U", "-isystem", "-std=", "-x")):
            out.append(a)
            continue
    # default language if not specified
    if not any(a == "-x" or a.startswith("-x") for a in out):
        if any(_SRC_RX.search(a) and a.lower().endswith(".c") for a in argv):
            out[:0] = ["-x", "c"]
        else:
            out[:0] = ["-x", "c++"]
    return out

def extract_compile_commands(root: str | os.PathLike[str], make_cmd: List[str] | None = None, target: str | None = None) -> Dict[str, List[str]]:
    """
    Returns { abs_source_path : clang-args list } by dry-running make.
    Uses: make -nB [target]
    """
    rootp = Path(root).resolve()
    cmd = list(make_cmd or ["make", "-nB"])
    if target:
        cmd.append(target)
    env = os.environ.copy()
    env.setdefault("V", "1")
    proc = subprocess.run(cmd, cwd=str(rootp), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    out = proc.stdout.splitlines()
    mapping: Dict[str, List[str]] = {}
    cwd = rootp
    for line in out:
        line = line.strip()
        if not line:
            continue
        # handle 'cd dir && ...'
        if line.startswith("cd "):
            parts = _split_chained(line)
            for part in parts:
                if part.startswith("cd "):
                    new = shlex.split(part)[1]
                    cwd = (cwd / new).resolve() if not Path(new).is_absolute() else Path(new).resolve()
                else:
                    argv = shlex.split(part)
                    if _is_compile(argv):
                        src = _src_from(argv, cwd)
                        if not src: continue
                        mapping[str(src)] = _args_for(argv, cwd)
            continue
        # simple line
        argv = shlex.split(line)
        if _is_compile(argv):
            src = _src_from(argv, cwd)
            if not src: continue
            mapping[str(src)] = _args_for(argv, cwd)
    return mapping
