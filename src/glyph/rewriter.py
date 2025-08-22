# src/glyph/rewriter.py
from __future__ import annotations

# Ensure libclang path is set before importing clang.cindex
from .libclang_loader import ensure as _ensure_libclang
_ensure_libclang()

from dataclasses import dataclass
from clang import cindex
from .ids import short_id
from typing import Optional, Iterable, List, Tuple


# ── clang glue ────────────────────────────────────────────────────────────────
def _clang_args_for(filename: str, extra: Iterable[str] | None) -> List[str]:
    args = ["-x", "c"]
    if filename.endswith((".hpp", ".hh", ".hxx", ".cc", ".cpp", ".cxx")):
        args = ["-x", "c++"]
    if extra:
        args.extend(extra)
    return args

# ── Entities ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Entity:
    kind: str          # fn | prototype | typedef | struct | union | enum | macro
    name: str
    start: int
    end: int
    storage: str       # extern | static | inline | static_inline
    decl_sig: str
    eff_sig: str
    gid: str
    # NEW:
    sig_id: str        # canonical signature id
    linkage: str       # 'internal' | 'external'


def _extract_includes_from_tu(tu: cindex.TranslationUnit, filename: str) -> List[Tuple[str, str]]:
    """
    Returns a list of (resolved_path, kind) where kind in {"quote","angle"}.
    Only returns includes that libclang resolves to a real file path.
    """
    out: List[Tuple[str, str]] = []
    for cur in tu.cursor.get_children():
        if cur.kind != cindex.CursorKind.INCLUSION_DIRECTIVE:
            continue
        inc_file = cur.get_included_file()
        if not inc_file:
            continue
        path = inc_file.name
        # Heuristic kind from token spelling (#include "x.h" vs <x.h>)
        kind = "quote"
        try:
            toks = list(cur.get_tokens())
            # For '#include "util.h"', token spellings often contain a single token '"util.h"'
            if any(t.spelling.startswith("<") for t in toks):
                kind = "angle"
        except Exception:
            pass
        out.append((path, kind))
    return out

def scan_includes_file(filename: str, *, extra_args: Optional[Iterable[str]] = None) -> List[Tuple[str, str]]:
    """
    Parse a real file with libclang and return (resolved_path, kind) includes.
    """
    idx = cindex.Index.create()
    args = _clang_args_for(filename, extra_args)
    tu = idx.parse(
        path=filename,
        args=args,
        options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
    )
    return _extract_includes_from_tu(tu, filename)

def scan_includes_code(code: str, *, filename: str = "snippet.c",
                       extra_args: Optional[Iterable[str]] = None) -> List[Tuple[str, str]]:
    """
    Parse unsaved code (for tests) and return (resolved_path, kind) includes.
    Resolution works if libclang can locate the header on disk via include paths.
    """
    idx = cindex.Index.create()
    args = _clang_args_for(filename, extra_args)
    tu = idx.parse(
        path=filename,
        args=args,
        unsaved_files=[(filename, code)],
        options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
    )
    return _extract_includes_from_tu(tu, filename)
    
def _has_inline_token(cur: cindex.Cursor) -> bool:
    try:
        for tok in cur.get_tokens():
            s = tok.spelling
            if s == "(" or s == "{":
                break
            if s == "inline":
                return True
    except Exception:
        pass
    return False

def _storage_of(cur: cindex.Cursor) -> str:
    sc = cur.storage_class
    is_static = sc == cindex.StorageClass.STATIC
    is_inline = False
    if cur.kind == cindex.CursorKind.FUNCTION_DECL:
        fn = getattr(cur, "is_function_inlined", None)
        if callable(fn):
            try:
                is_inline = bool(fn())
            except Exception:
                is_inline = _has_inline_token(cur)
        else:
            is_inline = _has_inline_token(cur)
    if is_static and is_inline:
        return "static_inline"
    if is_static:
        return "static"
    if is_inline:
        return "inline"
    return "extern"

def _linkage_of(storage: str) -> str:
    """Map storage → linkage domain."""
    if storage in ("static", "static_inline"):
        return "internal"
    return "external"

def _effsig(cur: cindex.Cursor) -> str:
    t = cur.type.spelling or cur.displayname or cur.spelling
    return " ".join(t.split())

def _canonicalize_sig_text(sig: str) -> str:
    # Aggressive whitespace normalization; keep it simple and stable.
    return " ".join((sig or "").split())

def _sig_id_for(sig: str) -> str:
    return short_id("sig", _canonicalize_sig_text(sig))

def _extent_offsets(ext: cindex.SourceRange) -> Tuple[int, int]:
    return ext.start.offset, ext.end.offset

def _fn_signature(cur: cindex.Cursor) -> str:
    return " ".join((cur.displayname or cur.spelling).split())

def _typedef_sig(cur: cindex.Cursor) -> str:
    return " ".join((cur.displayname or f"typedef {cur.spelling}").split())

def _record_sig(cur: cindex.Cursor) -> str:
    k = cur.kind
    prefix = "struct" if k == cindex.CursorKind.STRUCT_DECL else ("union" if k == cindex.CursorKind.UNION_DECL else "enum")
    n = (cur.spelling or "<anonymous>").strip()
    return f"{prefix} {n}"

def _macro_is_function_like(cur: cindex.Cursor) -> bool:
    toks = list(cur.get_tokens())
    for i, t in enumerate(toks[:4]):
        if i == 0 and t.kind.name == "IDENTIFIER":
            if len(toks) > 1 and toks[1].spelling == "(":
                return True
            break
    return False

def _collect_entities(tu: cindex.TranslationUnit, filename: str) -> List[Entity]:
    ents: List[Entity] = []
    for cur in tu.cursor.get_children():
        loc = cur.location
        if not loc.file or loc.file.name != filename:
            continue
        k = cur.kind
        if k == cindex.CursorKind.FUNCTION_DECL:
            s, e = _extent_offsets(cur.extent)
            storage = _storage_of(cur)
            decl = _fn_signature(cur)
            eff  = _effsig(cur)
            kind = "fn" if cur.is_definition() else "prototype"
            gid  = short_id("fn" if cur.is_definition() else "proto", decl, eff, storage, filename)
            sig_id   = _sig_id_for(eff)
            linkage  = _linkage_of(storage)
            ents.append(Entity(kind, cur.spelling, s, e, storage, decl, eff, gid, sig_id, linkage))
        elif k in (cindex.CursorKind.STRUCT_DECL, cindex.CursorKind.UNION_DECL, cindex.CursorKind.ENUM_DECL):
            if not cur.is_definition():
                continue
            s, e = _extent_offsets(cur.extent)
            eff  = _record_sig(cur)
            kind = "struct" if k == cindex.CursorKind.STRUCT_DECL else ("union" if k == cindex.CursorKind.UNION_DECL else "enum")
            gid  = short_id(kind, eff, "extern", filename)
            sig_id  = _sig_id_for(eff)
            linkage = "external"
            ents.append(Entity(kind, cur.spelling or "<anonymous>", s, e, "extern", eff, eff, gid, sig_id, linkage))
        elif k == cindex.CursorKind.TYPEDEF_DECL:
            s, e = _extent_offsets(cur.extent)
            decl = _typedef_sig(cur)
            eff  = _effsig(cur)
            gid  = short_id("typedef", eff, "extern", filename)
            sig_id  = _sig_id_for(eff)
            linkage = "external"
            ents.append(Entity("typedef", cur.spelling, s, e, "extern", decl, eff, gid, sig_id, linkage))
        elif k == cindex.CursorKind.MACRO_DEFINITION:
            if not _macro_is_function_like(cur):
                continue
            s, e = _extent_offsets(cur.extent)
            name = cur.spelling
            eff  = f"#define {name}(...)"
            gid  = short_id("macro", name, filename)
            sig_id  = _sig_id_for(eff)
            linkage = "external"
            ents.append(Entity("macro", name, s, e, "extern", eff, eff, gid, sig_id, linkage))
    ents.sort(key=lambda x: (x.start, x.end))
    return ents

# ── Marker insertion (idempotent) ────────────────────────────────────────────
def _already_marked(buf: bytes) -> bool:
    return b"/* GLYPH:S " in buf or b"/* GLYPH:E " in buf

def _insert_markers(buf: bytes, ents: List[Entity]) -> bytes:
    out = bytearray(buf)
    for e in sorted(ents, key=lambda x: x.start, reverse=True):
        start_line = b"\n/* GLYPH:S " + e.gid.encode("ascii") + b" */\n"
        end_line   = b"\n/* GLYPH:E " + e.gid.encode("ascii") + b" */\n"
        out[e.end:e.end] = end_line
        out[e.start:e.start] = start_line
    return bytes(out)

# ── Public API ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RewriteResult:
    code: str
    entities: List[Entity]

def rewrite_snippet(code: str, *, filename: str = "snippet.c", extra_args: Iterable[str] | None = None) -> RewriteResult:
    """
    Parses with full bodies (no skip) to classify fn vs prototype reliably.
    Inserts GLYPH markers and returns entity metadata.
    """
    if _already_marked(code.encode("utf-8", "ignore")):
        return RewriteResult(code=code, entities=[])
    idx = cindex.Index.create()
    args = _clang_args_for(filename, extra_args)
    tu = idx.parse(
        path=filename,
        args=args,
        unsaved_files=[(filename, code)],
        options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
    )
    ents = _collect_entities(tu, filename)
    rewritten = _insert_markers(code.encode("utf-8"), ents).decode("utf-8")
    return RewriteResult(code=rewritten, entities=ents)
