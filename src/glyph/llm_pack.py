# glyph/llm_pack.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Iterable, List, Set, Tuple
import json

from .rewriter import rewrite_snippet, Entity
from .graph import callgraph_snippet, CallGraph

# ───────────────────────────── schema (JSONL) ─────────────────────────────
# One JSON object per line, small keys, stable ordering.
#  hdr: {"t":"hdr","v":1,"files":["a.c","b.h"],"counts":{"fn":N,"pr":N,"td":N,"rec":N,"mc":N}}
#    f: {"t":"fn","id":ID,"n":"name","s":"extern|static|inline|static_inline","sig":"decl","f":0}
#    p: {"t":"pr","id":ID,"n":"name","s":"extern|...","sig":"decl","f":0}
#    d: {"t":"td","id":ID,"n":"typedef_name","sig":"typedef ...","f":1}
#    r: {"t":"rc","id":ID,"k":"struct|union|enum","n":"name|<anonymous>","sig":"struct name","f":1}
#    m: {"t":"mc","id":ID,"n":"MACRO","f":0}
#    c: {"t":"call","src":ID,"dst":ID} or {"t":"call","src":ID,"dstn":"printf"}  # unknown dst
#    g: {"t":"gap","k":"missing_def","n":"name","files":[0,1]}
#    g: {"t":"gap","k":"undef_ref","src":ID,"dstn":"name"}
# ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LLMPack:
    lines: List[str]  # JSONL lines

    def to_str(self) -> str:
        return "\n".join(self.lines) + ("\n" if self.lines and not self.lines[-1].endswith("\n") else "")

def _kind_tag(e: Entity) -> str:
    if e.kind == "fn": return "fn"
    if e.kind == "prototype": return "pr"
    if e.kind == "typedef": return "td"
    if e.kind in ("struct", "union", "enum"): return "rc"
    if e.kind == "macro": return "mc"
    return "uk"

def _counts(entities: Iterable[Entity]) -> Dict[str, int]:
    c = {"fn":0,"pr":0,"td":0,"rec":0,"mc":0}
    for e in entities:
        t = _kind_tag(e)
        if t == "fn": c["fn"] += 1
        elif t == "pr": c["pr"] += 1
        elif t == "td": c["td"] += 1
        elif t == "rc": c["rec"] += 1
        elif t == "mc": c["mc"] += 1
    return c

def pack_snippets(snippets: Dict[str, str], *, extra_args: Iterable[str] | None = None) -> LLMPack:
    # Parse + mark entities; collect callgraphs.
    units: List[Tuple[str, List[Entity], CallGraph]] = []
    for fname in sorted(snippets.keys()):
        code = snippets[fname]
        rw = rewrite_snippet(code, filename=fname, extra_args=extra_args)
        cg = callgraph_snippet(code, filename=fname, extra_args=extra_args)
        units.append((fname, rw.entities, cg))

    files: List[str] = [u[0] for u in units]
    file_ix: Dict[str, int] = {f:i for i, f in enumerate(files)}

    # Flatten entities; deterministic sort by (kind_tag, name, id)
    all_entities: List[Tuple[str, Entity, int]] = []
    for fname, ents, _ in units:
        ix = file_ix[fname]
        for e in ents:
            all_entities.append((fname, e, ix))
    all_entities.sort(key=lambda fei: (_kind_tag(fei[1]), fei[1].name, fei[1].gid))

    # Known IDs and name→decl files
    known_ids: Set[str] = {e.gid for _, e, _ in all_entities}
    decl_files: Dict[str, Set[int]] = {}
    def_names: Set[str] = set()
    for _, e, ix in all_entities:
        if e.kind == "prototype":
            decl_files.setdefault(e.name, set()).add(ix)
        if e.kind == "fn":
            def_names.add(e.name)

    # Build JSONL
    out: List[str] = []

    # Header
    counts = _counts((e for _, e, _ in all_entities))
    hdr = {"t":"hdr","v":1,"files":files,"counts":counts}
    out.append(json.dumps(hdr, separators=(",", ":")))

    # Entities
    for _, e, ix in all_entities:
        t = _kind_tag(e)
        if t == "fn":
            rec = {"t":"fn","id":e.gid,"n":e.name,"s":e.storage,"sig":e.decl_sig,"f":ix}
        elif t == "pr":
            rec = {"t":"pr","id":e.gid,"n":e.name,"s":e.storage,"sig":e.decl_sig,"f":ix}
        elif t == "td":
            rec = {"t":"td","id":e.gid,"n":e.name,"sig":e.decl_sig,"f":ix}
        elif t == "rc":
            rec = {"t":"rc","id":e.gid,"k":e.kind,"n":e.name,"sig":e.eff_sig,"f":ix}
        elif t == "mc":
            rec = {"t":"mc","id":e.gid,"n":e.name,"f":ix}
        else:
            continue
        out.append(json.dumps(rec, separators=(",", ":")))

    # Calls (deduped)
    seen_calls: Set[Tuple[str, str]] = set()
    for _, _, cg in units:
        for src in sorted(cg.roots):
            for dst in sorted(cg.edges.get(src, set())):
                key = (src, dst)
                if key in seen_calls: 
                    continue
                seen_calls.add(key)
                if dst in known_ids:
                    out.append(json.dumps({"t":"call","src":src,"dst":dst}, separators=(",", ":")))
                else:
                    out.append(json.dumps({"t":"call","src":src,"dstn":cg.names.get(dst, "unknown")}, separators=(",", ":")))

    # Gaps: prototypes with no defs
    for name, fset in sorted(decl_files.items()):
        if name not in def_names:
            out.append(json.dumps({"t":"gap","k":"missing_def","n":name,"files":sorted(fset)}, separators=(",", ":")))

    # Gaps: undefined refs (from callgraph)
    seen_undef: Set[Tuple[str, str]] = set()
    for _, _, cg in units:
        for src in cg.roots:
            for dst in cg.edges.get(src, set()):
                if dst not in known_ids:
                    key = (src, cg.names.get(dst, "unknown"))
                    if key in seen_undef:
                        continue
                    seen_undef.add(key)
                    out.append(json.dumps({"t":"gap","k":"undef_ref","src":src,"dstn":key[1]}, separators=(",", ":")))

    return LLMPack(lines=out)
