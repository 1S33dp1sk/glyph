# src/glyph/intel.py
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from .db import GlyphDB, DbEntity

# ----------------------- tiny logger -----------------------
_VERBOSE = os.environ.get("GLYPH_INTEL_VERBOSE", "0") not in ("", "0", "false", "False")

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

# ----------------------- data models -----------------------

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

# ----------------------- helpers -----------------------

_IDENT_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

def _idents_in_text(text: str) -> List[str]:
    # keep order, dedupe
    seen: set[str] = set()
    out: List[str] = []
    for m in _IDENT_RX.finditer(text):
        tok = m.group(0)
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out

def _read_span(path: str, start: int, end: int, *, surround_lines: int = 2) -> str:
    try:
        b = Path(path).read_bytes()
        start = max(0, min(start, len(b)))
        end = max(start, min(end, len(b)))
        view = b[start:end]
        txt = view.decode("utf-8", "ignore")

        full = b.decode("utf-8", "ignore")
        before = full[:start].count("\n")
        after = before + txt.count("\n")
        lines = full.splitlines()
        lo = max(0, before - surround_lines)
        hi = min(len(lines), after + 1 + surround_lines)
        return "\n".join(lines[lo:hi])
    except Exception:
        try:
            return Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""

# ----------------------- retrieval -----------------------

class GlyphRetriever:
    """Thin retrieval layer over GlyphDB (exact-name seeds + FTS + neighbor expansion)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.db = GlyphDB(db_path)

    def close(self) -> None:
        self.db.close()

    def search(self, q: str, *, limit: int = 8) -> List[DbEntity]:
        out: List[DbEntity] = []
        seen: set[str] = set()

        # 1) extract identifiers from the question and do exact lookups first
        idents = _idents_in_text(q)
        _log("idents_from_question", idents)
        for ident in idents:
            for ent in self.db.lookup_by_name(ident):
                if ent.gid in seen:
                    continue
                out.append(ent)
                seen.add(ent.gid)
                if len(out) >= limit:
                    _log("seeds", [e.name for e in out])
                    return out

        # 2) FTS fallback (db.fts_search already quotes/escapes)
        for gid, name, decl in self.db.fts_search(q, limit=limit):
            if gid in seen:
                continue
            ent = self.db.get_entity(gid)
            if ent:
                out.append(ent)
                seen.add(gid)
                if len(out) >= limit:
                    break

        _log("seeds", [e.name for e in out])
        return out

    def expand_neighbors(self, seeds: Sequence[DbEntity], *, hops: int = 1, per_hop: int = 4) -> List[DbEntity]:
        out: List[DbEntity] = list(seeds)
        seen: set[str] = {e.gid for e in seeds}
        frontier: List[str] = [e.gid for e in seeds]
        for _ in range(max(0, hops)):
            nxt: List[str] = []
            for gid in frontier:
                # callees
                for dg, dn in self.db.callees(gid)[:per_hop]:
                    if not dg or dg in seen:
                        continue
                    ent = self.db.get_entity(dg)
                    if ent:
                        out.append(ent)
                        seen.add(dg)
                        nxt.append(dg)
                # callers
                for sg in self.db.callers(gid)[:per_hop]:
                    if sg in seen:
                        continue
                    ent = self.db.get_entity(sg)
                    if ent:
                        out.append(ent)
                        seen.add(sg)
                        nxt.append(sg)
            frontier = nxt
            if not frontier:
                break
        _log("expanded_neighbors", [e.name for e in out])
        return out

    def materialize(self, ents: Sequence[DbEntity], *, surround_lines: int = 2, max_chars: int = 14000) -> List[ContextItem]:
        ctx: List[ContextItem] = []
        total = 0
        for e in ents:
            snip = _read_span(e.file_path, e.start, e.end, surround_lines=surround_lines)
            if max_chars > 0 and total + len(snip) > max_chars:
                snip = snip[: max(0, max_chars - total)]
            ctx.append(
                ContextItem(
                    gid=e.gid,
                    name=e.name,
                    kind=e.kind,
                    storage=e.storage,
                    decl_sig=e.decl_sig or e.name,
                    file_path=e.file_path,
                    start=e.start,
                    end=e.end,
                    snippet=snip,
                )
            )
            total += len(snip)
            if max_chars > 0 and total >= max_chars:
                break
        _log("materialized", [{"name": c.name, "bytes": len(c.snippet)} for c in ctx])
        return ctx

# ----------------------- Ollama client -----------------------

def _ollama_http_available(endpoint: str) -> bool:
    try:
        import urllib.request  # noqa: F401
        return True
    except Exception:
        return False

def _ollama_generate_http(prompt: str, *, model: str, endpoint: str) -> str:
    import urllib.request
    req = urllib.request.Request(
        url=endpoint.rstrip("/") + "/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "stream": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8", "ignore"))
        return data.get("response", "").strip()

def _ollama_generate_cli(prompt: str, *, model: str) -> str:
    p = subprocess.run(
        ["ollama", "run", model],
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return p.stdout.strip() or p.stderr.strip()

def call_ollama(prompt: str, *, model: str = "gpt-oss:20b", endpoint: str = "http://localhost:11434") -> str:
    _log("prompt_preview", prompt[:1200])
    if _ollama_http_available(endpoint):
        try:
            out = _ollama_generate_http(prompt, model=model, endpoint=endpoint)
            _log("model_output_preview", out[:1200])
            return out
        except Exception as e:
            _log("ollama_http_error", repr(e))
    out = _ollama_generate_cli(prompt, model=model)
    _log("model_output_preview", out[:1200])
    return out

# ----------------------- Prompting & Orchestration -----------------------

def _build_prompt(question: str, ctx: Sequence[ContextItem]) -> str:
    q_idents = _idents_in_text(question)
    primary = q_idents[0] if q_idents else None

    rules = [
        "You are an expert C/C++ code analyst.",
        "Answer ONLY using the provided context snippets. If unknown, say so briefly.",
        "Cite snippet indices like [#1], [#2] when referring to code.",
        "Be concise (1–2 sentences).",
        "Explicitly describe the core operation in code terms if visible (e.g., 'returns a + b').",
    ]
    if primary:
        rules.append(f"Your FIRST LINE MUST start with '{primary}: ' and include that exact identifier.")
    else:
        rules.append("Your FIRST LINE MUST start with the primary identifier you are describing, followed by ': '.")

    parts: List[str] = []
    parts.append("\n".join(rules))
    parts.append("\nContext:")
    for i, c in enumerate(ctx, 1):
        header = f"[#{i}] {c.kind} {c.storage} {c.name} — {c.decl_sig}\nfile://{c.file_path}  bytes:{c.start}-{c.end}"
        fence = "```c"
        parts.append(f"{header}\n{fence}\n{c.snippet}\n```\n")
    parts.append(f"User question: {question}\n")
    parts.append("Answer:")
    return "\n".join(parts)

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
        expanded = retr.expand_neighbors(seeds, hops=hops, per_hop=max(2, k // 2))
        seen: set[str] = set()
        uniq: List[DbEntity] = []
        for e in seeds + expanded:
            if e.gid in seen:
                continue
            seen.add(e.gid)
            uniq.append(e)
        ctx = retr.materialize(uniq, surround_lines=2, max_chars=max_chars)
        prompt = _build_prompt(question, ctx)
        return call_ollama(prompt, model=model, endpoint=endpoint)
    finally:
        retr.close()
