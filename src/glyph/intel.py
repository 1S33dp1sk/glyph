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


# ----------------------- Context dataclass -----------------------

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


# ----------------------- Small utilities -----------------------

_IDENT_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

def _is_ident(token: str) -> bool:
    return _IDENT_RX.fullmatch(token) is not None

def _extract_identifiers(q: str) -> List[str]:
    """
    Pull out identifier-ish tokens from a natural-language question.
    Prefer tokens with '_' or length >= 4 to avoid noise.
    Deduplicate while preserving order.
    """
    seen: set[str] = set()
    out: List[str] = []
    for tok in _IDENT_RX.findall(q):
        if "_" in tok or len(tok) >= 4:
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out

def _read_span(path: str, start: int, end: int, *, surround_lines: int = 2) -> str:
    """
    Safely extract a line-rounded snippet for [start,end) byte offsets.
    If offsets are out-of-sync, fall back to the whole file.
    """
    try:
        b = Path(path).read_bytes()
        s = max(0, min(start, len(b)))
        e = max(s, min(end, len(b)))
        full_txt = b.decode("utf-8", "ignore")
        # Convert byte offsets to line numbers by counting newlines before s and e
        # (approximate; good enough for readable context)
        pre = full_txt[:s]
        seg = full_txt[s:e]
        start_ln = pre.count("\n")
        end_ln = start_ln + seg.count("\n")
        lines = full_txt.splitlines()
        lo = max(0, start_ln - surround_lines)
        hi = min(len(lines), end_ln + 1 + surround_lines)
        return "\n".join(lines[lo:hi])
    except Exception:
        try:
            return Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""


# ----------------------- Retriever -----------------------

class GlyphRetriever:
    """Retrieval over GlyphDB (identifier-first, FTS fallback, neighbor expansion)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.db = GlyphDB(db_path)

    def close(self) -> None:
        self.db.close()

    def search(self, q: str, *, limit: int = 8) -> List[DbEntity]:
        """
        Strategy:
          1) For each identifier token in the question:
               - exact name lookup
               - then FTS for that single token
          2) Broad FTS on the whole question
        """
        out: List[DbEntity] = []
        seen: set[str] = set()

        def add(ent: Optional[DbEntity]) -> bool:
            if not ent:
                return False
            if ent.gid in seen:
                return False
            out.append(ent)
            seen.add(ent.gid)
            return len(out) >= limit

        # 1) Identifier-focused passes
        for tok in _extract_identifiers(q):
            for ent in self.db.lookup_by_name(tok):
                if add(ent):
                    return out
            for gid, name, decl in self.db.fts_search(tok, limit=max(3, limit // 2)):
                if gid in seen:
                    continue
                ent = self.db.get_entity(gid)
                if add(ent):
                    return out

        # 2) Broad FTS
        for gid, name, decl in self.db.fts_search(q, limit=limit):
            if gid in seen:
                continue
            ent = self.db.get_entity(gid)
            if add(ent):
                break

        return out

    def expand_neighbors(self, seeds: Sequence[DbEntity], *, hops: int = 1, per_hop: int = 4) -> List[DbEntity]:
        """Add callers/callees around seeds (breadth-limited)."""
        out: List[DbEntity] = list(seeds)
        seen: set[str] = {e.gid for e in seeds}
        frontier: List[str] = [e.gid for e in seeds]
        for _ in range(max(0, hops)):
            nxt: List[str] = []
            for gid in frontier:
                # callees
                for dg, _dn in self.db.callees(gid)[:per_hop]:
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
        return out

    def materialize(
        self,
        ents: Sequence[DbEntity],
        *,
        surround_lines: int = 2,
        max_chars: int = 14_000,
    ) -> List[ContextItem]:
        ctx: List[ContextItem] = []
        total = 0
        for e in ents:
            snip = _read_span(e.file_path, e.start, e.end, surround_lines=surround_lines)
            # enforce budget gently
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
        return ctx


# ----------------------- Ollama client -----------------------

def _http_alive(endpoint: str) -> bool:
    """
    Quick probe: GET /api/tags (Ollama provides this). Timeout ~1s.
    """
    try:
        import urllib.request
        url = endpoint.rstrip("/") + "/api/tags"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=1) as resp:  # noqa: S310
            return resp.status == 200
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
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8", "ignore"))
        return data.get("response", "").strip()

def _ollama_generate_cli(prompt: str, *, model: str) -> str:
    # `ollama run MODEL` reads prompt from stdin
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
    if _http_alive(endpoint):
        try:
            return _ollama_generate_http(prompt, model=model, endpoint=endpoint)
        except Exception:
            pass
    return _ollama_generate_cli(prompt, model=model)


# ----------------------- Prompting -----------------------

_SYSTEM = (
    "You are an expert C/C++ code analyst. Answer ONLY from the provided code snippets.\n"
    "If something isn't present in the snippets, say you don't know.\n"
    "When a function returns a simple expression (e.g., `return a+b;`), include that exact expression "
    "verbatim in backticks (e.g., `a+b`). Be concise.\n"
)

def _build_prompt(question: str, ctx: Sequence[ContextItem]) -> str:
    parts: List[str] = []
    parts.append(_SYSTEM)
    parts.append("\nContext:")
    for i, c in enumerate(ctx, 1):
        header = f"[#{i}] {c.kind} {c.storage} {c.name} — {c.decl_sig}\nfile://{c.file_path}  bytes:{c.start}-{c.end}"
        parts.append(f"{header}\n```c\n{c.snippet}\n```\n")
    parts.append(f"User question: {question}\n")
    parts.append("Answer in one short sentence. If applicable, include the exact return expression in backticks.")
    return "\n".join(parts)


# ----------------------- Orchestration -----------------------

def answer_question(
    db_path: str,
    question: str,
    *,
    k: int = 6,
    hops: int = 1,
    model: str = "gpt-oss:20b",
    endpoint: str = "http://localhost:11434",
    max_chars: int = 14_000,
) -> str:
    retr = GlyphRetriever(db_path)
    try:
        # Seed & expand
        seeds = retr.search(question, limit=k)
        expanded = retr.expand_neighbors(seeds, hops=hops, per_hop=max(2, k // 2))

        # De-dup by gid, preserve order
        seen: set[str] = set()
        uniq: List[DbEntity] = []
        for e in seeds + expanded:
            if e.gid in seen:
                continue
            seen.add(e.gid)
            uniq.append(e)

        # Materialize code context
        ctx = retr.materialize(uniq, surround_lines=2, max_chars=max_chars)

        # If somehow empty, return a clear message (helps tests)
        if not ctx:
            return "I couldn't find relevant code in the database for this question."

        # Prompt and ask
        prompt = _build_prompt(question, ctx)
        return call_ollama(prompt, model=model, endpoint=endpoint)
    finally:
        retr.close()
