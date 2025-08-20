# glyph/tree_agent.py
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Dict, Iterable, List, Set, Tuple
import json

from .rewriter import rewrite_snippet, Entity  # same IDs/kinds as markers
from .graph import callgraph_snippet, CallGraph

# ── Compact units ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Unit:
    filename: str
    entities: List[Entity]
    callgraph: CallGraph

@dataclass(frozen=True)
class GapMissingDef:
    name: str
    decl_files: List[str]

@dataclass(frozen=True)
class GapUndefinedRef:
    caller_id: str
    caller_name: str
    callee_name: str

@dataclass(frozen=True)
class Hotspot:
    id: str
    name: str
    fanout: int
    indegree: int

@dataclass(frozen=True)
class TreeSummary:
    files: List[str]
    totals: Dict[str, int]
    modules: Dict[str, int]                 # top-level dir -> file count
    gaps_missing_defs: List[GapMissingDef]
    gaps_undefined_refs: List[GapUndefinedRef]
    hotspots: List[Hotspot]                 # top fanout/indegree functions

    def to_json(self, *, indent: int = 2) -> str:
        obj = {
            "files": self.files,
            "totals": self.totals,
            "modules": self.modules,
            "gaps_missing_defs": [asdict(g) for g in self.gaps_missing_defs],
            "gaps_undefined_refs": [asdict(g) for g in self.gaps_undefined_refs],
            "hotspots": [asdict(h) for h in self.hotspots],
        }
        return json.dumps(obj, indent=indent)

# ── Build units from snippets ─────────────────────────────────────────────────

def build_units(snippets: Dict[str, str], *, extra_args: Iterable[str] | None = None) -> List[Unit]:
    units: List[Unit] = []
    for fname, code in snippets.items():
        rw = rewrite_snippet(code, filename=fname, extra_args=extra_args)
        cg = callgraph_snippet(code, filename=fname, extra_args=extra_args)
        units.append(Unit(filename=fname, entities=rw.entities, callgraph=cg))
    return units

# ── Inference / reasoning over the compact tree ───────────────────────────────

def infer_summary(units: List[Unit]) -> TreeSummary:
    files = [u.filename for u in units]

    # Index entities
    by_id: Dict[str, Entity] = {}
    defs_by_name: Dict[str, List[Tuple[str, str]]] = {}   # name -> [(id, file)]
    decls_by_name: Dict[str, List[str]] = {}              # name -> [file,...]
    counts = {"fn_defs": 0, "prototypes": 0, "typedefs": 0, "records": 0, "macros": 0, "entities": 0}

    for u in units:
        for e in u.entities:
            by_id[e.gid] = e
            counts["entities"] += 1
            if e.kind == "fn":
                counts["fn_defs"] += 1
                defs_by_name.setdefault(e.name, []).append((e.gid, u.filename))
            elif e.kind == "prototype":
                counts["prototypes"] += 1
                decls_by_name.setdefault(e.name, []).append(u.filename)
            elif e.kind == "typedef":
                counts["typedefs"] += 1
            elif e.kind in ("struct", "union", "enum"):
                counts["records"] += 1
            elif e.kind == "macro":
                counts["macros"] += 1

    # Missing definitions (prototypes seen, no matching fn def across units)
    gaps_missing_defs: List[GapMissingDef] = []
    for name, decl_files in decls_by_name.items():
        if name not in defs_by_name:
            gaps_missing_defs.append(GapMissingDef(name=name, decl_files=sorted(set(decl_files))))

    # Callgraph edges across units
    fanout: Dict[str, int] = {}
    indegree: Dict[str, int] = {}
    gaps_undef: List[GapUndefinedRef] = []

    # Build a reverse map name->ids for faster indegree attribution when possible
    ids_by_name: Dict[str, Set[str]] = {}
    for nm, v in defs_by_name.items():
        ids_by_name[nm] = {i for i, _ in v}

    for u in units:
        cg = u.callgraph
        for fid in cg.roots:
            fanout.setdefault(fid, 0)
            for cid in cg.edges.get(fid, set()):
                fanout[fid] += 1
                indegree[cid] = indegree.get(cid, 0) + 1
                # Undefined if callee is not a known definition in our set
                if cid not in by_id:
                    gaps_undef.append(GapUndefinedRef(
                        caller_id=fid,
                        caller_name=cg.names.get(fid, "<fn>"),
                        callee_name=cg.names.get(cid, "<ext>"),
                    ))

    # Hotspots: top N by fanout then indegree
    hs: List[Hotspot] = []
    for fid, fo in sorted(fanout.items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
        hs.append(Hotspot(
            id=fid,
            name=_pick_name(fid, units),
            fanout=fo,
            indegree=indegree.get(fid, 0),
        ))

    # Module bins = top-level dir segments
    modules: Dict[str, int] = {}
    for f in files:
        seg = f.split("/", 1)[0] if "/" in f else "."
        modules[seg] = modules.get(seg, 0) + 1

    return TreeSummary(
        files=sorted(files),
        totals=counts,
        modules=dict(sorted(modules.items(), key=lambda kv: (-kv[1], kv[0]))),
        gaps_missing_defs=sorted(gaps_missing_defs, key=lambda g: g.name)[:100],
        gaps_undefined_refs=gaps_undef[:200],
        hotspots=hs,
    )

def _pick_name(fid: str, units: List[Unit]) -> str:
    for u in units:
        for e in u.entities:
            if e.gid == fid and e.kind in ("fn", "prototype"):
                return e.name
    # Fallback: search callgraph name maps
    for u in units:
        if fid in u.callgraph.names:
            return u.callgraph.names[fid]
    return fid
