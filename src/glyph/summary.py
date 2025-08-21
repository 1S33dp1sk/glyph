# src/glyph/summary.py
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .rewriter import Entity, rewrite_snippet
from .graph import callgraph_snippet

try:
    from .mkparse import extract_compile_commands  # optional
except Exception:  # noqa: BLE001
    extract_compile_commands = None  # type: ignore[misc]


@dataclass(frozen=True)
class EntityOut:
    gid: str
    kind: str
    name: str
    storage: str
    decl_sig: str
    eff_sig: str
    start: int
    end: int


@dataclass(frozen=True)
class FileOut:
    path: str
    args: List[str]
    entities: List[EntityOut]


@dataclass(frozen=True)
class CallOut:
    src_gid: str
    src_name: str
    dst_gid: Optional[str]
    dst_name: Optional[str]


@dataclass(frozen=True)
class RepoSummary:
    root: str
    files: List[FileOut]
    calls: List[CallOut]
    totals: Dict[str, int]

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(
            {
                "root": self.root,
                "files": [asdict(f) for f in self.files],
                "calls": [asdict(c) for c in self.calls],
                "totals": self.totals,
            },
            indent=indent,
        )


def _walk_sources(root: Path, exts: Tuple[str, ...], ignore: Iterable[str]) -> List[Path]:
    ig = set(x.strip() for x in ignore if x.strip())
    out: List[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in ig for part in p.parts):
            continue
        if p.suffix.lower() in exts:
            out.append(p)
    return out


def summarize_repo(
    root: str,
    *,
    ext_csv: str = ".c,.h,.cc,.cpp,.cxx,.hpp,.hh,.hxx",
    ignore_csv: str = ".git,.glyph,build",
    make_cmd: Optional[str] = None,
    make_target: Optional[str] = None,
    cflags: str = "",
) -> RepoSummary:
    """
    Two-pass scan:
      1) Parse every source/header → collect entities and global name→gid map.
      2) Parse calls per file → produce edges with global resolution (dst_gid if known).
    """
    rootp = Path(root).resolve()
    exts = tuple(x.strip() for x in ext_csv.split(",") if x.strip())
    ignore = tuple(x.strip() for x in ignore_csv.split(",") if x.strip())

    # Optional per-file args harvested from make -nB
    per_file: Dict[str, List[str]] = {}
    if make_cmd and extract_compile_commands is not None:
        per_file = extract_compile_commands(str(rootp), shlex.split(make_cmd), make_target)

    # Pass 1: entities + global symbol table
    files: List[FileOut] = []
    global_fn_name_to_gid: Dict[str, str] = {}
    paths = _walk_sources(rootp, exts, ignore)
    for fp in paths:
        code = fp.read_text(encoding="utf-8", errors="ignore")
        args = per_file.get(str(fp.resolve()), shlex.split(cflags))
        rr = rewrite_snippet(code, filename=fp.name, extra_args=args)
        ents_out = [
            EntityOut(
                gid=e.gid,
                kind=e.kind,
                name=e.name,
                storage=e.storage,
                decl_sig=e.decl_sig,
                eff_sig=e.eff_sig,
                start=int(e.start),
                end=int(e.end),
            )
            for e in rr.entities
        ]
        files.append(FileOut(path=str(fp), args=args, entities=ents_out))
        for e in rr.entities:
            if e.kind in ("fn", "prototype") and e.name and e.gid:
                global_fn_name_to_gid.setdefault(e.name, e.gid)

    # Pass 2: calls with global resolution
    calls: List[CallOut] = []
    for f in files:
        code = Path(f.path).read_text(encoding="utf-8", errors="ignore")
        cg = callgraph_snippet(code, filename=Path(f.path).name, extra_args=f.args)
        # Build local name→gid for defined fns in this file
        local_fn_name_to_gid = {e.name: e.gid for e in f.entities if e.kind == "fn"}
        for src in cg.roots:
            src_name = cg.names.get(src)
            if not src_name:
                continue
            src_gid = local_fn_name_to_gid.get(src_name)
            if not src_gid:
                # skip calls originating from prototypes/externs or non-local definitions
                continue
            for dst in cg.edges.get(src, set()):
                dst_name = cg.names.get(dst)
                if not dst_name:
                    continue
                dst_gid = global_fn_name_to_gid.get(dst_name)
                calls.append(CallOut(src_gid=src_gid, src_name=src_name, dst_gid=dst_gid, dst_name=dst_name))

    # Totals
    totals: Dict[str, int] = {
        "files": len(files),
        "entities": sum(len(f.entities) for f in files),
        "calls": len(calls),
        "unresolved_calls": sum(1 for c in calls if c.dst_gid is None),
    }
    for k in ("fn", "prototype", "typedef", "struct", "union", "enum", "macro"):
        totals[f"entities_{k}"] = sum(1 for f in files for e in f.entities if e.kind == k)

    return RepoSummary(root=str(rootp), files=files, calls=calls, totals=totals)
