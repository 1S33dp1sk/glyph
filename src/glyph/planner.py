# src/glyph/planner.py
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .db import GlyphDB, DbEntity
from .intel import call_ollama, GlyphRetriever


# ---------- Small helpers ----------

def _to_lines(s: str) -> List[str]:
    return [ln.strip() for ln in s.splitlines() if ln.strip()]

def _parse_numbered_list(s: str) -> List[str]:
    # Accept "1) foo", "1. foo", or plain lines
    out = []
    for ln in _to_lines(s):
        m = re.match(r"^\s*\d+[\)\.]\s*(.+)$", ln)
        out.append(m.group(1) if m else ln)
    return out

def _pick_int(text: str, default: int = 75) -> int:
    # Pick the first 0..100 integer in text; fallback to default
    nums = re.findall(r"\b([0-9]{1,3})\b", text)
    for n in nums:
        v = int(n)
        if 0 <= v <= 100:
            return v
    return default


# ---------- Data contracts ----------

@dataclass(frozen=True)
class PlanStep:
    id: str
    title: str
    deps: List[str]
    rationale: str
    expected_outcome: str

@dataclass(frozen=True)
class PlanDoc:
    goals: List[str]
    resources: List[str]
    steps: List[PlanStep]
    risks: List[Dict[str, str]]
    success_criteria: List[str]
    open_questions: List[str]
    score: int

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps({
            "goals": self.goals,
            "resources": self.resources,
            "steps": [asdict(s) for s in self.steps],
            "risks": self.risks,
            "success_criteria": self.success_criteria,
            "open_questions": self.open_questions,
            "score": self.score,
        }, indent=indent)

    def to_markdown(self) -> str:
        lines = []
        lines.append("# Plan\n")
        lines.append("## Goals")
        for i, g in enumerate(self.goals, 1):
            lines.append(f"{i}. {g}")
        lines.append("\n## Resources")
        for r in self.resources:
            lines.append(f"- {r}")
        lines.append("\n## Steps")
        for s in self.steps:
            lines.append(f"- **{s.id}** {s.title} (deps: {', '.join(s.deps) or '—'})")
            lines.append(f"  - Rationale: {s.rationale}")
            lines.append(f"  - Expected: {s.expected_outcome}")
        if self.risks:
            lines.append("\n## Risks & mitigations")
            for r in self.risks:
                lines.append(f"- {r.get('risk','')} → _{r.get('mitigation','')}_")
        if self.success_criteria:
            lines.append("\n## Success criteria")
            for c in self.success_criteria:
                lines.append(f"- {c}")
        if self.open_questions:
            lines.append("\n## Open questions")
            for q in self.open_questions:
                lines.append(f"- {q}")
        lines.append(f"\n**Score:** {self.score}/100")
        return "\n".join(lines)


# ---------- Repo “explain” ----------

@dataclass(frozen=True)
class RepoSummary:
    files: int
    entities_by_kind: Dict[str, int]
    unresolved_calls: int
    top_fanin: List[Tuple[str, int]]     # (gid, callers)
    top_fanout: List[Tuple[str, int]]    # (gid, callees)

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps({
            "files": self.files,
            "entities_by_kind": self.entities_by_kind,
            "unresolved_calls": self.unresolved_calls,
            "top_fanin": self.top_fanin,
            "top_fanout": self.top_fanout,
        }, indent=indent)

    def to_markdown(self) -> str:
        lines = []
        lines.append("# Repository Summary\n")
        lines.append(f"- Files indexed: **{self.files}**")
        lines.append(f"- Unresolved calls: **{self.unresolved_calls}**")
        lines.append("\n## Entities by kind")
        for k, v in sorted(self.entities_by_kind.items()):
            lines.append(f"- {k}: {v}")
        if self.top_fanin:
            lines.append("\n## Top fan-in (most callers)")
            for gid, n in self.top_fanin[:10]:
                lines.append(f"- {gid}: {n} callers")
        if self.top_fanout:
            lines.append("\n## Top fan-out (most callees)")
            for gid, n in self.top_fanout[:10]:
                lines.append(f"- {gid}: {n} callees")
        return "\n".join(lines)


def explain_basic(db_path: str) -> RepoSummary:
    db = GlyphDB(db_path)
    try:
        files = db.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        unresolved_calls = db.conn.execute(
            "SELECT COUNT(*) FROM calls WHERE dst_gid IS NULL"
        ).fetchone()[0]
        kinds = dict(db.conn.execute(
            "SELECT kind, COUNT(*) FROM entities GROUP BY kind"
        ).fetchall() or [])
        # fanin/out
        top_fanin = [
            (row[0], row[1])
            for row in db.conn.execute(
                "SELECT dst_gid, COUNT(*) as n FROM calls WHERE dst_gid IS NOT NULL "
                "GROUP BY dst_gid ORDER BY n DESC LIMIT 50"
            ).fetchall()
        ]
        top_fanout = [
            (row[0], row[1])
            for row in db.conn.execute(
                "SELECT src_gid, COUNT(*) as n FROM calls GROUP BY src_gid ORDER BY n DESC LIMIT 50"
            ).fetchall()
        ]
        return RepoSummary(
            files=int(files),
            entities_by_kind={str(k): int(v) for k, v in kinds.items()},
            unresolved_calls=int(unresolved_calls),
            top_fanin=top_fanin,
            top_fanout=top_fanout,
        )
    finally:
        db.close()


def explain_with_ai(db_path: str, *, model: str, endpoint: str, max_chars: int = 12000) -> str:
    # Build a compact context from key entities
    retr = GlyphRetriever(db_path)
    try:
        seeds: List[DbEntity] = []
        # Prefer high fan-in symbols as seeds
        db = retr.db
        rows = db.conn.execute(
            "SELECT dst_gid, COUNT(*) as n FROM calls WHERE dst_gid IS NOT NULL "
            "GROUP BY dst_gid ORDER BY n DESC LIMIT 6"
        ).fetchall()
        for (gid, _) in rows:
            ent = db.get_entity(gid)
            if ent:
                seeds.append(ent)
        if not seeds:
            # fallback: any 6 entities
            rows = db.conn.execute(
                "SELECT gid FROM entities LIMIT 6"
            ).fetchall()
            for (gid,) in rows:
                ent = db.get_entity(gid)
                if ent:
                    seeds.append(ent)

        ctx = retr.materialize(seeds, surround_lines=1, max_chars=max_chars)
        # Build a short “explain repo” prompt
        parts = [
            "You are a senior engineer. Explain briefly what this repository contains and how the pieces relate.",
            "Be concrete. If unknown, say so.",
            "\nContext snippets:"
        ]
        for i, c in enumerate(ctx, 1):
            parts.append(f"[#{i}] {c.kind} {c.storage} {c.name} — {c.decl_sig}\n```c\n{c.snippet}\n```")
        parts.append("\nWrite a crisp 4-8 sentence briefing:")
        prompt = "\n".join(parts)
        return call_ollama(prompt, model=model, endpoint=endpoint)
    finally:
        retr.close()


# ---------- Plan: propose (AI loop) ----------

def propose_plan(
    *,
    db_path: str,
    goals_text: str,
    resources_text: str,
    model: str,
    endpoint: str,
    threshold: int = 89,
    max_iters: int = 8,
    fallback_after: int = 5,
    fallback_threshold: int = 80,
    style: str = "balanced",
    verbose: bool = False,
) -> Tuple[PlanDoc, List[str]]:
    """
    Draft → Rate → Refine loop anchored to Goals and Resources.
    Returns (best_plan, trace_lines)
    """
    trace: List[str] = []
    goals = _parse_numbered_list(goals_text)
    resources = _parse_numbered_list(resources_text)

    # Repo context to anchor (numbers keep the LLM honest)
    summary = explain_basic(db_path)
    context_md = summary.to_markdown()

    def _draft_prompt() -> str:
        return (
            "You are a planning assistant.\n"
            "Create a REPOSITORY-AWARE plan to achieve these GOALS using the given RESOURCES.\n"
            "Return JSON only with keys: goals, resources, steps, risks, success_criteria, open_questions.\n"
            "Each step must have: id(titlecase like S1), title, deps(list of step ids), rationale, expected_outcome.\n"
            f"STYLE: {style}\n\n"
            f"GOALS (keep numbered & unchanged):\n" +
            "\n".join(f"{i}) {g}" for i, g in enumerate(goals, 1)) +
            "\n\nRESOURCES (keep all visible):\n" +
            "\n".join(f"- {r}" for r in resources) +
            "\n\nREPO CONTEXT (factual):\n" + context_md +
            "\n\nJSON:"
        )

    def _rate_prompt(plan_json: str) -> str:
        return (
            "You are a strict reviewer. Rate this plan for this repository on 0..100.\n"
            "Criteria: coverage of all GOALS (by number), feasibility with RESOURCES, clear dependencies, risk awareness, testability.\n"
            "Return a short critique and 'Score: NN' on its own line.\n\n"
            f"GOALS:\n" + "\n".join(f"{i}) {g}" for i, g in enumerate(goals, 1)) +
            "\n\nRESOURCES:\n" + "\n".join(f"- {r}" for r in resources) +
            "\n\nPLAN JSON:\n" + plan_json
        )

    def _refine_prompt(plan_json: str, critique: str) -> str:
        return (
            "Refine the plan JSON to address the critique while keeping goals & resources visible and unchanged.\n"
            "Return full JSON only (same schema). Improve step ordering and risk mitigations. Do not omit steps that cover goals.\n\n"
            "CRITIQUE:\n" + critique + "\n\n"
            "CURRENT PLAN JSON:\n" + plan_json
        )

    best: Optional[PlanDoc] = None
    best_score = -1

    def _decode_plan(s: str) -> Optional[PlanDoc]:
        try:
            j = json.loads(s)
            steps = [PlanStep(**st) for st in j.get("steps", [])]
            return PlanDoc(
                goals=j.get("goals", goals),
                resources=j.get("resources", resources),
                steps=steps,
                risks=list(j.get("risks", [])),
                success_criteria=list(j.get("success_criteria", [])),
                open_questions=list(j.get("open_questions", [])),
                score=0,
            )
        except Exception:
            return None

    plan_text = call_ollama(_draft_prompt(), model=model, endpoint=endpoint)
    if verbose:
        trace += ["--- Draft ---", plan_text]

    for it in range(1, max_iters + 1):
        plan = _decode_plan(plan_text)
        if not plan:
            # ask for stricter JSON
            plan_text = call_ollama(
                "Your last output was not valid JSON. Output JSON only per schema (no markdown). "
                + _draft_prompt(),
                model=model, endpoint=endpoint
            )
            if verbose: trace += [f"[iter {it}] re-ask JSON"]
            continue

        # rate
        rating = call_ollama(_rate_prompt(plan.to_json(indent=None)), model=model, endpoint=endpoint)
        score = _pick_int(rating, default=75)
        if verbose:
            trace += [f"--- Rate {it} ---", rating, f"(score:{score})"]

        if score > best_score:
            best, best_score = plan, score

        # stop?
        target = threshold if it <= max_iters else fallback_threshold
        if score >= target:
            best = PlanDoc(**{**asdict(best), "score": score}) if best else plan
            break

        # fallback threshold after N iters
        if it >= fallback_after and score >= fallback_threshold:
            best = PlanDoc(**{**asdict(best), "score": score}) if best else plan
            break

        # refine and loop
        plan_text = call_ollama(_refine_prompt(plan.to_json(indent=None), rating), model=model, endpoint=endpoint)

    if best and best_score >= 0:
        best = PlanDoc(**{**asdict(best), "score": best_score})
    elif best:
        best = PlanDoc(**{**asdict(best), "score": 0})

    return best or PlanDoc(goals, resources, [], [], [], [], 0), trace


# ---------- Impact (blast radius) ----------

@dataclass(frozen=True)
class ImpactReport:
    target: str
    depth: int
    callees: Dict[str, List[str]]
    callers: Dict[str, List[str]]

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps({
            "target": self.target,
            "depth": self.depth,
            "callees": self.callees,
            "callers": self.callers,
        }, indent=indent)

    def to_markdown(self) -> str:
        lines = [f"# Impact: {self.target} (depth {self.depth})"]
        if self.callers:
            lines.append("\n## Callers")
            for k, vs in self.callers.items():
                lines.append(f"- {k}")
                for v in vs:
                    lines.append(f"  - {v}")
        if self.callees:
            lines.append("\n## Callees")
            for k, vs in self.callees.items():
                lines.append(f"- {k}")
                for v in vs:
                    lines.append(f"  - {v}")
        return "\n".join(lines)


def impact(db_path: str, *, symbol: Optional[str], path: Optional[str], depth: int = 2) -> ImpactReport:
    db = GlyphDB(db_path)
    try:
        # find seed gid(s)
        gids: List[str] = []
        if symbol:
            for ent in db.lookup_by_name(symbol):
                gids.append(ent.gid)
        if not gids and symbol:
            for gid, _, _ in db.fts_search(symbol, limit=1):
                gids.append(gid)
        if not gids and path:
            for ent in db.entities_in_file(path):
                gids.append(ent.gid)
        if not gids:
            return ImpactReport(target=symbol or path or "<unknown>", depth=depth, callees={}, callers={})

        # BFS callers/callees
        seen = set(gids)
        frontier = list(gids)
        callers_map: Dict[str, List[str]] = {}
        callees_map: Dict[str, List[str]] = {}

        for _ in range(max(0, depth)):
            nxt: List[str] = []
            for g in frontier:
                cs = [x for x in db.callers(g)]
                if cs:
                    callers_map[g] = cs
                es = [x for (x, _name) in db.callees(g)]
                es = [x for x in es if x]  # linked only
                if es:
                    callees_map[g] = es
                for h in cs + es:
                    if h and h not in seen:
                        seen.add(h)
                        nxt.append(h)
            frontier = nxt
            if not frontier:
                break

        return ImpactReport(target=symbol or path or "<unknown>", depth=depth,
                            callees=callees_map, callers=callers_map)
    finally:
        db.close()


# ---------- Status (plan.json vs repo) ----------

def status(db_path: str, plan_json_path: str) -> Dict[str, str]:
    """
    Very light heuristic check against success criteria.
    You can expand criteria evaluation later.
    """
    plan = json.loads(Path(plan_json_path).read_text(encoding="utf-8"))
    db = GlyphDB(db_path)
    try:
        unresolved = db.conn.execute("SELECT COUNT(*) FROM calls WHERE dst_gid IS NULL").fetchone()[0]
        ok_unresolved = unresolved == 0
        out = {
            "unresolved_calls": str(unresolved),
            "unresolved_ok": "yes" if ok_unresolved else "no",
            "goals_count": str(len(plan.get("goals", []))),
            "steps_count": str(len(plan.get("steps", []))),
        }
        return out
    finally:
        db.close()
