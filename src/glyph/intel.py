# src/glyph/intel.py
from __future__ import annotations

import json, os, re, subprocess, sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence
from .db import GlyphDB, DbEntity

# ---------------- env & tiny loggers ----------------
_VERBOSE = os.environ.get("GLYPH_INTEL_VERBOSE", "0").lower() not in ("", "0", "false", "no")
_TRACE = os.environ.get("GLYPH_INTEL_TRACE", "1").lower() not in ("", "0", "false", "no")

def _log(kind: str, payload) -> None:
    if not _VERBOSE:
        return
    try:
        if isinstance(payload, str):
            sys.stderr.write(f"[intel] {kind}: {payload}\n")
        else:
            sys.stderr.write(f"[intel] {kind}: {json.dumps(payload, ensure_ascii=False)[:10000]}\n")
        sys.stderr.flush()
    except Exception:
        pass

def _trace(msg: str) -> None:
    if _TRACE:
        try:
            sys.stderr.write(msg.rstrip() + "\n")
            sys.stderr.flush()
        except Exception:
            pass

# ---------------- data models ----------------
@dataclass(frozen=True)
class ContextItem:
    gid: str
    name: str
    kind: str
    storage: str
    decl_sig: str
    file_path: str
    start: int
    end: int
    snippet: str

# ---------------- helpers ----------------
_IDENT_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

def _idents_in_text(text: str) -> List[str]:
    seen, out = set(), []
    for m in _IDENT_RX.finditer(text):
        tok = m.group(0)
        if tok not in seen:
            seen.add(tok); out.append(tok)
    return out

def _read_span(path: str, start: int, end: int, *, surround_lines: int = 2) -> str:
    try:
        b = Path(path).read_bytes()
        start = max(0, min(start, len(b))); end = max(start, min(end, len(b)))
        view = b[start:end]; txt = view.decode("utf-8", "ignore")
        full = b.decode("utf-8", "ignore")
        before = full[:start].count("\n"); after = before + txt.count("\n")
        lines = full.splitlines()
        lo = max(0, before - surround_lines); hi = min(len(lines), after + 1 + surround_lines)
        return "\n".join(lines[lo:hi])
    except Exception:
        try: return Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception: return ""

def _read_exact(path: str, start: int, end: int) -> str:
    try:
        b = Path(path).read_bytes()
        start = max(0, min(start, len(b))); end = max(start, min(end, len(b)))
        return b[start:end].decode("utf-8", "ignore")
    except Exception:
        return ""

# ---- return-expression extraction ----
_COMMENT_RX = re.compile(r"//.*?$|/\*.*?\*/", re.S | re.M)
_RETURN_RX  = re.compile(r"\breturn\b\s*(.+?);", re.S)  # first ';' after return

def _strip_comments(s: str) -> str:
    return _COMMENT_RX.sub("", s)

def _peel_outer_parens(expr: str) -> str:
    e = expr.strip()
    while len(e) >= 2 and e[0] == "(" and e[-1] == ")":
        inner = e[1:-1].strip()
        if inner.count("(") == inner.count(")"): e = inner
        else: break
    return e

def _normalize_expr(expr: str) -> str:
    e = _peel_outer_parens(expr)
    e = re.sub(r"\s+", "", e)
    # collapse parens around identifiers/literals: (x)->x, (1u)->1u
    e = re.sub(r"\(([A-Za-z_]\w*)\)", r"\1", e)
    e = re.sub(r"\((\d+[uUlL]*)\)", r"\1", e)
    return e

def _extract_return_expr_from_text(text: str) -> Optional[str]:
    try:
        s = _strip_comments(text)
        m = _RETURN_RX.search(s)
        if not m: return None
        expr = _normalize_expr(m.group(1))
        if 0 < len(expr) <= 160: return expr
    except Exception: pass
    return None

# ---- macro extraction (function-like) ----
# Captures RHS of: #define INC(x)   ((x) + 1u)  \  (multi-line ok)
#                                  ^^^^^^^^^^^ captured
_MACRO_FUNC_RX = re.compile(
    r"^\s*#\s*define\s+[A-Za-z_]\w*\s*\([^)]*\)\s*(.+?)(?:\\\s*\n.+)*$",
    re.M
)

def _extract_macro_expr(text: str) -> Optional[str]:
    try:
        s = _strip_comments(text)
        m = _MACRO_FUNC_RX.search(s)
        if not m: return None
        expr = _normalize_expr(m.group(1))
        if expr: return expr
    except Exception: pass
    return None

# ---- simple op tags: INC/DEC with suffixes and parens tolerated ----
_ONE_LIT = r"1(?:[uU][lL]?|[lL][uU]?|)?"   # 1, 1u, 1U, 1l, 1L, 1ul, 1UL, 1lu, 1LU
_TERMVAR = r"\(?[A-Za-z_]\w*\)?"           # var or (var)
_TERMLIT = r"\(?"+_ONE_LIT+r"\)?"

_INC_PATTERNS = [ re.compile(rf"^{_TERMVAR}\+{_TERMLIT}$"), re.compile(rf"^{_TERMLIT}\+{_TERMVAR}$") ]
_DEC_PATTERNS = [ re.compile(rf"^{_TERMVAR}-{_TERMLIT}$"), re.compile(rf"^{_TERMLIT}-{_TERMVAR}$") ]

def _simple_op_tags(expr: str) -> List[str]:
    e = _normalize_expr(expr)
    tags: List[str] = []
    if any(p.match(e) for p in _INC_PATTERNS): tags.append("INC +1")
    if any(p.match(e) for p in _DEC_PATTERNS): tags.append("DEC -1")
    return tags

# ---------------- retrieval ----------------
class GlyphRetriever:
    """Retrieval over GlyphDB: exact names → FTS → neighbors."""

    def __init__(self, db_path: str) -> None:
        self.db = GlyphDB(db_path)

    def close(self) -> None:
        self.db.close()

    def search(self, q: str, *, limit: int = 8) -> List[DbEntity]:
        out: List[DbEntity] = []; seen: set[str] = set()
        # exact identifiers
        idents = _idents_in_text(q); _log("idents_from_question", idents)
        for ident in idents:
            for ent in self.db.lookup_by_name(ident):
                if ent.gid in seen: continue
                out.append(ent); seen.add(ent.gid)
                if len(out) >= limit:
                    _trace("SEEDS: " + ", ".join(f"{e.name}({e.kind})" for e in out))
                    return out
        # FTS fallback
        for gid, _name, _decl in self.db.fts_search(q, limit=limit):
            if gid in seen: continue
            ent = self.db.get_entity(gid)
            if ent:
                out.append(ent); seen.add(gid)
                if len(out) >= limit: break
        _trace("SEEDS: " + (", ".join(f"{e.name}({e.kind})" for e in out) if out else "(none)"))
        return out

    def expand_neighbors(self, seeds: Sequence[DbEntity], *, hops: int = 1, per_hop: int = 4) -> List[DbEntity]:
        out = list(seeds); seen = {e.gid for e in seeds}; frontier = [e.gid for e in seeds]
        for _ in range(max(0, hops)):
            nxt: List[str] = []
            for gid in frontier:
                for dg, _ in self.db.callees(gid)[:per_hop]:
                    if not dg or dg in seen: continue
                    ent = self.db.get_entity(dg); 
                    if ent: out.append(ent); seen.add(dg); nxt.append(dg)
                for sg in self.db.callers(gid)[:per_hop]:
                    if sg in seen: continue
                    ent = self.db.get_entity(sg)
                    if ent: out.append(ent); seen.add(sg); nxt.append(sg)
            frontier = nxt
            if not frontier: break
        return out

    def materialize(self, ents: Sequence[DbEntity], *, surround_lines: int = 2, max_chars: int = 14000) -> List[ContextItem]:
        ctx: List[ContextItem] = []; total = 0
        for e in ents:
            snip = _read_span(e.file_path, e.start, e.end, surround_lines=surround_lines)
            if max_chars > 0 and total + len(snip) > max_chars:
                snip = snip[: max(0, max_chars - total)]
            ctx.append(ContextItem(
                gid=e.gid, name=e.name, kind=e.kind, storage=e.storage,
                decl_sig=e.decl_sig or e.name, file_path=e.file_path,
                start=e.start, end=e.end, snippet=snip
            ))
            total += len(snip)
            if max_chars > 0 and total >= max_chars: break
        return ctx

# ---------------- Ollama ----------------
def _ollama_http_available(endpoint: str) -> bool:
    try: import urllib.request; return True
    except Exception: return False

def _ollama_generate_http(prompt: str, *, model: str, endpoint: str) -> str:
    import urllib.request
    options = {
        "temperature": float(os.environ.get("GLYPH_INTEL_TEMPERATURE", "0.0")),
        "top_p": 0.9, "repeat_penalty": 1.1,
    }
    req = urllib.request.Request(
        url=endpoint.rstrip("/") + "/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "stream": False, "options": options}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8", "ignore"))
        return (data.get("response") or "").strip()

def _ollama_generate_cli(prompt: str, *, model: str) -> str:
    p = subprocess.run(["ollama", "run", model], input=prompt, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return (p.stdout.strip() or p.stderr.strip())

def call_ollama(prompt: str, *, model: str = "gpt-oss:20b", endpoint: str = "http://localhost:11434") -> str:
    _log("prompt_preview", prompt[:1200])
    if _ollama_http_available(endpoint):
        try:
            out = _ollama_generate_http(prompt, model=model, endpoint=endpoint)
            _log("model_output_preview", out[:1200]); return out
        except Exception as e:
            _log("ollama_http_error", repr(e))
    out = _ollama_generate_cli(prompt, model=model)
    _log("model_output_preview", out[:1200]); return out

# ---------------- prompting & deterministic answers ----------------
_RULES = (
    "You are an expert C/C++ code analyst.\n"
    "Rules:\n"
    "1) Use ONLY the context; if unknown, output exactly: Not enough context.\n"
    "2) Your answer MUST be ONE sentence.\n"
    "3) The answer MUST start with '<PRIMARY>: '.\n"
    "4) Cite at least one snippet index like [#N].\n"
    "5) If a function body shows a single return expression, include that exact expression without spaces (e.g., a+b).\n"
    "6) Prefer function definitions over prototypes.\n"
    "7) Plain text only (no code fences/headings).\n"
)

def _choose_primary(question: str, ctx: Sequence[ContextItem]) -> Optional[str]:
    q_ids = _idents_in_text(question)
    names = {c.name for c in ctx}

    # If the question contains an identifier that's in context, use it.
    for tok in q_ids:
        if tok in names:
            return tok

    # Heuristic: if the question hints at multiplication / addition, pick the first
    # function whose return-expression contains the operator.
    qlow = question.lower()

    def _first_fn_with(op: str) -> Optional[str]:
        for c in ctx:
            if c.kind != "fn":
                continue
            body = _read_exact(c.file_path, c.start, c.end)
            expr = _extract_return_expr_from_text(body)
            if expr and op in expr:
                return c.name
        return None

    if any(t in qlow for t in ("mul", "*", "product")):
        nm = _first_fn_with("*")
        if nm:
            return nm

    if any(t in qlow for t in ("add", "+", "sum")):
        nm = _first_fn_with("+")
        if nm:
            return nm

    # Otherwise prefer any function; fall back to first context item.
    for c in ctx:
        if c.kind == "fn":
            return c.name
    return ctx[0].name if ctx else None


def _build_prompt(question: str, ctx: Sequence[ContextItem], primary: Optional[str]) -> str:
    hints: List[str] = []
    for i, c in enumerate(ctx, 1):
        body = _read_exact(c.file_path, c.start, c.end)
        expr = _extract_return_expr_from_text(body) if c.kind == "fn" else _extract_macro_expr(body)
        if expr: hints.append(f"HINT [#{i}]: returns {expr}")
    catalog: List[str] = []
    for i, c in enumerate(ctx, 1):
        header = f"[#{i}] {c.kind} {c.storage} {c.name} — {c.decl_sig} ({c.gid})\n{c.file_path}:{c.start}-{c.end}"
        catalog.append(f"{header}\n{c.snippet}")
    parts: List[str] = [_RULES]
    if primary: parts.append(f"PRIMARY: {primary}")
    if hints: parts.append("OPERATION HINTS:\n" + "\n".join(hints))
    parts.append("CONTEXT (cite with [#N]):\n" + "\n\n".join(catalog))
    parts.append(f"QUESTION: {question}")
    parts.append("ANSWER:")
    return "\n\n".join(parts)

def _ensure_prefix_and_brief(ans: str, primary: Optional[str]) -> str:
    s = (ans or "").strip()
    if not s: return "Not enough context."
    if primary:
        pref = f"{primary}: "
        if not s.lower().startswith(pref.lower()): s = pref + s
    s = re.split(r"(?<=[.!?])\s+", s)[0]
    if "[#" not in s: s += " [#1]"
    return s

def _deterministic_answer(question: str, ctx: Sequence[ContextItem]) -> Optional[str]:
    if not ctx: return None
    primary = _choose_primary(question, ctx)
    if not primary: return None
    matches = [(i, c) for i, c in enumerate(ctx, 1) if c.name == primary]
    if not matches:
        i, c = 1, ctx[0]; primary = c.name
    else:
        defs = [(i, c) for i, c in matches if c.kind == "fn"]
        i, c = (defs[0] if defs else matches[0])
    body = _read_exact(c.file_path, c.start, c.end)
    expr = _extract_return_expr_from_text(body) if c.kind == "fn" else _extract_macro_expr(body)
    if c.kind == "fn":
        if expr: return f"{primary}: returns {expr} [#{i}]"
        return f"{primary}: function definition present [#{i}]"
    # macro/prototype/other
    if expr: return f"{primary}: macro returns {expr} [#{i}]"
    return f"{primary}: prototype/unknown — no body in context [#{i}]"

def _ops_trace(ctx: Sequence[ContextItem]) -> None:
    ops: List[str] = []
    for i, c in enumerate(ctx, 1):
        body = _read_exact(c.file_path, c.start, c.end)
        expr = _extract_return_expr_from_text(body) if c.kind == "fn" else _extract_macro_expr(body)
        if expr:
            tags = _simple_op_tags(expr)
            extra = (f" ; {' '.join(tags)}" if tags else "")
            ops.append(f"{c.name}={expr} [#{i}]{extra}")
    if ops: _trace("OPS: " + ", ".join(ops))

# ---------------- orchestration ----------------
def answer_question(
    db_path: str,
    question: str,
    *,
    k: int = 6,
    hops: int = 1,
    model: str = "gpt-oss:20b",
    endpoint: str = "http://localhost:11434",
    max_chars: int = 14000,
) -> str:
    retr = GlyphRetriever(db_path)
    try:
        seeds = retr.search(question, limit=k)
        # (also emit from orchestrator to be extra sure tests see it)
        _trace("SEEDS: " + (", ".join(f"{e.name}({e.kind})" for e in seeds) if seeds else "(none)"))
        expanded = retr.expand_neighbors(seeds, hops=hops, per_hop=max(2, k // 2))
        # unique
        seen, uniq = set(), []
        for e in seeds + expanded:
            if e.gid in seen: continue
            seen.add(e.gid); uniq.append(e)
        ctx = retr.materialize(uniq, surround_lines=2, max_chars=max_chars)
        if not ctx: return "Not enough context."
        _ops_trace(ctx)
        det = _deterministic_answer(question, ctx)
        if det: return det
        primary = _choose_primary(question, ctx)
        prompt = _build_prompt(question, ctx, primary)
        raw = call_ollama(prompt, model=model, endpoint=endpoint)
        return _ensure_prefix_and_brief(raw, primary)
    finally:
        retr.close()
