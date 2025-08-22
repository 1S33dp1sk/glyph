from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Iterable, Set

from .libclang_loader import ensure as _ensure_libclang
_ensure_libclang()

from clang import cindex

# Reuse the same helpers as the rewriter to keep IDs consistent.
from .rewriter import _effsig as _effsig_fn, _storage_of as _storage_of_fn  # internal, deliberate import
from .ids import short_id

def _clang_args_for(filename: str, extra: Iterable[str] | None) -> list[str]:
    args = ["-x", "c"]
    if filename.endswith((".hpp", ".hh", ".hxx", ".cc", ".cpp", ".cxx")):
        args = ["-x", "c++"]
    if extra:
        args.extend(extra)
    return args

@dataclass(frozen=True)
class CallGraph:
    roots: list[str]                 # function IDs that have definitions in the snippet/TU
    edges: Dict[str, Set[str]]       # caller_id -> { callee_id, ... }
    names: Dict[str, str]            # id -> human name (spelling)

def _fn_id(cur: cindex.Cursor, filename: str) -> str:
    eff = _effsig_fn(cur)
    storage = _storage_of_fn(cur)
    kind = "fn" if cur.is_definition() else "proto"
    return short_id(kind, eff, storage, filename)

def _callee_id(ref: cindex.Cursor, fallback_name: str, filename: str) -> str:
    if ref is None:
        # Unknown/builtin; keep stable by hashing name + filename.
        return short_id("callee", fallback_name, "extern", filename)
    eff = _effsig_fn(ref)
    storage = _storage_of_fn(ref) if hasattr(ref, "storage_class") else "extern"
    fn = ref.location.file.name if ref.location and ref.location.file else filename
    return short_id("fn", eff, storage, fn)

def callgraph_snippet(code: str, *, filename: str = "snippet.c", extra_args: Iterable[str] | None = None) -> CallGraph:
    """
    Build an intra-TU call graph:
      - parses with bodies (no skip)
      - collects FUNCTION_DECL definitions as roots
      - for each, records CALL_EXPR → callee IDs (resolving .referenced when possible)
    """
    idx = cindex.Index.create()
    tu = idx.parse(
        path=filename,
        args=_clang_args_for(filename, extra_args),
        unsaved_files=[(filename, code)],
        options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
    )
    edges: Dict[str, Set[str]] = {}
    names: Dict[str, str] = {}
    roots: list[str] = []

    def visit_fn(fn: cindex.Cursor) -> None:
        fid = _fn_id(fn, filename)
        names[fid] = fn.spelling
        roots.append(fid)
        edges.setdefault(fid, set())
        # Walk only within the function extent
        def walk(cur: cindex.Cursor) -> None:
            for ch in cur.get_children():
                if ch.kind == cindex.CursorKind.CALL_EXPR:
                    ref = ch.referenced if hasattr(ch, "referenced") else None
                    name = (ref.spelling if ref else ch.displayname) or "unknown"
                    cid = _callee_id(ref, name, filename)
                    edges[fid].add(cid)
                    if cid not in names:
                        names[cid] = name
                walk(ch)
        walk(fn)

    for cur in tu.cursor.get_children():
        if not cur.location.file or cur.location.file.name != filename:
            continue
        if cur.kind == cindex.CursorKind.FUNCTION_DECL and cur.is_definition():
            visit_fn(cur)

    return CallGraph(roots=roots, edges=edges, names=names)
