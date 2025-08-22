# src/glyph/plan.py
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from .db import GlyphDB, DbEntity

# ---------- small helpers ----------


def _extract_json(text: str) -> Optional[dict]:
    """
    Try strict parse first; then a crude slice from first '{' to last '}'.
    Returns None if nothing usable is found.
    """
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        s = text.find("{")
        e = text.rfind("}")
        if s != -1 and e != -1 and e > s:
            return json.loads(text[s:e+1])
    except Exception:
        return None
    return None


def _lines(s: str) -> List[str]:
    xs = [x.strip("- \t\r") for x in s.splitlines()]
    return [x for x in xs if x]


def _rowcount(conn, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])

def _load_plan(plan_path: str | Path) -> dict:
    """
    Load a plan JSON file. If missing or invalid, return a minimal skeleton.
    """
    try:
        p = Path(plan_path)
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {
            "goals": [],
            "resources": [],
            "steps": [],
            "risks": [],
            "success_criteria": [],
            "open_questions": [],
            "score": 0,
        }

# ---------- public: explain ----------

def explain(db_path: str) -> dict:
    """
    Return high-level repo stats used by tests:
      - files: int
      - entities_by_kind: {kind: count}
      - unresolved_calls: int
    """
    with GlyphDB(db_path) as gdb:
        files = _rowcount(gdb.conn, "SELECT COUNT(*) FROM files")
        unresolved = _rowcount(gdb.conn, "SELECT COUNT(*) FROM calls WHERE dst_gid IS NULL")
        entities_by_kind: Dict[str, int] = {}
        for k, c in gdb.conn.execute("SELECT kind, COUNT(*) FROM entities GROUP BY kind"):
            entities_by_kind[str(k)] = int(c)
        return {
            "files": files,
            "entities_by_kind": entities_by_kind,
            "unresolved_calls": unresolved,
        }

# ---------- public: status ----------

def status(db_path: str, plan_path: str | Path) -> dict:
    """
    Read a plan JSON and compute current health against success criteria.
    Minimal contract for tests:
      - unresolved_ok: "yes" | "no"
      - snapshot: {files, entities, calls, unresolved}
      - missing_symbols: {name: count}
    """
    plan = _load_plan(plan_path)
    with GlyphDB(db_path) as gdb:
        files = _rowcount(gdb.conn, "SELECT COUNT(*) FROM files")
        entities = _rowcount(gdb.conn, "SELECT COUNT(*) FROM entities")
        calls = _rowcount(gdb.conn, "SELECT COUNT(*) FROM calls")
        unresolved = _rowcount(gdb.conn, "SELECT COUNT(*) FROM calls WHERE dst_gid IS NULL")
        missing_syms: Dict[str, int] = {}
        for name, c in gdb.conn.execute(
            "SELECT COALESCE(dst_name,'') AS n, COUNT(*) FROM calls WHERE dst_gid IS NULL GROUP BY n"
        ):
            if name:
                missing_syms[name] = int(c)
        return {
            "plan_goals": plan.get("goals", []),
            "snapshot": {
                "files": files,
                "entities": entities,
                "calls": calls,
                "unresolved": unresolved,
            },
            "unresolved_ok": "yes" if unresolved == 0 else "no",
            "missing_symbols": missing_syms,
        }

# ---------- public: impact ----------

def impact(db_path: str, symbol: str) -> dict:
    """
    Return:
      {
        "target": symbol,
        "entities": [gid, ...],
        "callers": { entity_gid: [caller_gid, ...] },
        "by_name": { symbol: [caller_gid, ...] }
      }
    """
    with GlyphDB(db_path) as gdb:
        ents: List[DbEntity] = list(gdb.lookup_by_name(symbol))
        callers_map: Dict[str, List[str]] = {}
        all_callers: List[str] = []
        for e in ents:
            cs = gdb.get_callers(e.gid)  # your new helper
            callers_map[e.gid] = cs
            all_callers.extend(cs)
        return {
            "target": symbol,
            "entities": [e.gid for e in ents],
            "callers": callers_map,
            "by_name": {symbol: sorted(set(all_callers))},
        }


# ---------- public: propose (light, schema-first) ----------

def propose(
    db_path: str,
    *,
    goals_text: str,
    resources_text: str,
    model: Optional[str] = None,
    endpoint: Optional[str] = None,
    max_iters: int = 3,
    fallback_after: int = 2,
    fallback_threshold: int = 70,
) -> dict:
    """
    Optional AI plan proposal (schema-first). If AI is unavailable/fails, return a
    deterministic, sensible plan built from the provided goals/resources.

    NOTE: Tests only validate schema presence, not the specific AI content.
    """

    def _lines(s: str) -> List[str]:
        xs = [x.strip("- \t\r") for x in s.splitlines()]
        return [x for x in xs if x]

    goals = _lines(goals_text)
    resources = _lines(resources_text)

    plan = None
    if model and endpoint:
        # Try to get a minified JSON plan from an LLM via ollama, best-effort.
        try:
            from .intel import call_ollama
            schema = (
                '{"goals":[],"resources":[],"steps":[{"id":"","title":"","deps":[],"rationale":"","expected_outcome":""}],'
                '"risks":[{"risk":"","mitigation":""}],"success_criteria":[],"open_questions":[],"score":0}'
            )
            prompt = (
                "You are a software planner. Produce ONLY minified JSON with keys exactly as:\n"
                + schema +
                "\nGoals:\n" + "\n".join(goals) +
                "\nResources:\n" + "\n".join(resources)
            )
            resp = call_ollama(prompt, model=model, endpoint=endpoint)
            m = re.search(r"\{.*\}", resp, re.S)
            if m:
                plan = json.loads(m.group(0))
        except Exception:
            plan = None

    if not plan:
        # Deterministic fallback: 1 step per goal, chaining deps.
        steps: List[dict] = []
        for i, g in enumerate(goals, 1):
            steps.append({
                "id": f"S{i}",
                "title": g if len(g) < 80 else g[:77] + "...",
                "deps": [f"S{i-1}"] if i > 1 else [],
                "rationale": "Support stated goal",
                "expected_outcome": g,
            })
        if not steps:
            steps = [{
                "id": "S1",
                "title": "Initial exploration",
                "deps": [],
                "rationale": "Gather baseline",
                "expected_outcome": "Have a measurable baseline",
            }]
        plan = {
            "goals": goals,
            "resources": resources,
            "steps": steps,
            "risks": [{"risk": "Scope creep", "mitigation": "Keep steps minimal and measurable"}],
            "success_criteria": ["No unresolved calls", "All steps completed"],
            "open_questions": [],
            "score": 0,
        }

    # Ensure schema keys exist & are of correct types
    for k in ("goals","resources","steps","risks","success_criteria","open_questions"):
        plan.setdefault(k, [] if k != "steps" else [])
    plan.setdefault("score", 0)
    return plan



def rate_plan(
    plan: dict,
    *,
    goals_text: str,
    resources_text: str,
    model: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> dict:
    """
    Score the plan 0–100 and return:
      {
        "score": int,
        "strengths": [str, ...],
        "gaps": [str, ...],
        "missing_steps": [str, ...],
        "risk_flags": [str, ...],
        "confidence": int,       # 0–100
        "notes": str
      }
    Keeps goals/resources explicitly in the prompt to anchor context.
    """
    goals = _lines(goals_text)
    resources = _lines(resources_text)

    # --- AI path
    if model and endpoint:
        try:
            from .intel import call_ollama
            schema = (
                '{"score":0,"strengths":[],"gaps":[],"missing_steps":[],'
                '"risk_flags":[],"confidence":0,"notes":""}'
            )
            prompt = (
                "You are a senior eng planner. Rate the given plan ONLY using this JSON schema:\n"
                + schema +
                "\nRules:\n"
                "- score is 0..100 based on feasibility, completeness, ordering, risks, and testability.\n"
                "- missing_steps should enumerate concrete steps needed to meet the goals.\n"
                "- Do not add prose outside JSON. Output must be a single JSON object.\n"
                "\nGOALS (keep in mind):\n" + "\n".join(goals or ["<none>"]) +
                "\nRESOURCES/CONSTRAINTS:\n" + "\n".join(resources or ["<none>"]) +
                "\nPLAN JSON:\n" + json.dumps(plan, separators=(",", ":"), ensure_ascii=False)
            )
            resp = call_ollama(prompt, model=model, endpoint=endpoint)
            obj = _extract_json(resp)
            if isinstance(obj, dict) and "score" in obj:
                # Normalize types
                obj["score"] = int(max(0, min(100, int(obj.get("score", 0)))))
                obj["confidence"] = int(max(0, min(100, int(obj.get("confidence", 0)))))
                for k in ("strengths", "gaps", "missing_steps", "risk_flags"):
                    v = obj.get(k, [])
                    obj[k] = [str(x) for x in v] if isinstance(v, list) else []
                obj["notes"] = str(obj.get("notes", ""))
                return obj
        except Exception:
            pass  # fall through to heuristic

    # --- Heuristic fallback
    # Very simple structural checks to get a stable, explainable score.
    score = 0
    strengths: List[str] = []
    gaps: List[str] = []
    missing_steps: List[str] = []
    risk_flags: List[str] = []

    goals_ok = bool(plan.get("goals"))
    steps = plan.get("steps") or []
    success = plan.get("success_criteria") or []
    risks = plan.get("risks") or []

    if goals_ok:
        score += 20
        strengths.append("Goals present")
    else:
        gaps.append("Goals missing")

    # Steps coverage: at least one step per goal (rough heuristic via count)
    if goals_ok and len(steps) >= len(plan["goals"]):
        score += 30
        strengths.append("Steps roughly cover goals")
    else:
        gaps.append("Insufficient steps to cover goals")
        # propose missing step names
        for i, g in enumerate(plan.get("goals", []), 1):
            if i > len(steps):
                missing_steps.append(f"Add step for: {g}")

    # Step fields quality
    if steps and all(s.get("id") and s.get("title") for s in steps):
        score += 10
        strengths.append("Steps have ids/titles")
    else:
        gaps.append("Some steps missing id/title")

    # Success criteria
    if success:
        score += 20
        strengths.append("Success criteria present")
    else:
        gaps.append("No success criteria defined")

    # Risks
    if risks:
        score += 10
        strengths.append("Risks documented")
    else:
        gaps.append("No risks documented")

    # Cap and assemble
    score = max(0, min(95, score))
    return {
        "score": int(score),
        "strengths": strengths,
        "gaps": gaps,
        "missing_steps": missing_steps,
        "risk_flags": risk_flags,
        "confidence": 50 if score >= 70 else 30,
        "notes": "Heuristic rating (no model available)",
    }

# ---------- public: refine_plan (AI first, deterministic fallback) --------------

def refine_plan(
    plan: dict,
    *,
    goals_text: str,
    resources_text: str,
    prior_feedback: Optional[dict] = None,
    model: Optional[str] = None,
    endpoint: Optional[str] = None,
    style: str = "balanced",
    iteration: int = 1,
) -> dict:
    """
    Produce an improved plan that keeps the same schema and context
    and explicitly considers the original GOALS/RESOURCES on every iteration.

    Returns a new plan dict (does not mutate the input plan).
    """
    goals = _lines(goals_text)
    resources = _lines(resources_text)
    fb = prior_feedback or {}

    # Defensive copy
    base = json.loads(json.dumps(plan))

    # --- AI path
    if model and endpoint:
        try:
            from .intel import call_ollama
            schema = (
                '{"goals":[],"resources":[],"steps":[{"id":"","title":"","deps":[],"rationale":"",'
                '"expected_outcome":""}],"risks":[{"risk":"","mitigation":""}],'
                '"success_criteria":[],"open_questions":[],"score":0}'
            )
            prompt = (
                "You are a senior eng planner. Refine the plan to better satisfy the GOALS, using ONLY this schema:\n"
                + schema +
                "\nConstraints:\n"
                "- Keep GOALS and RESOURCES explicitly in mind; do not drop or change them.\n"
                "- Ensure each goal has at least one concrete step; add dependencies (deps) when order matters.\n"
                "- Tighten success_criteria to be testable; enumerate relevant risks.\n"
                "- Improve titles/rationales; prefer measurable outcomes.\n"
                "- Output ONLY a single JSON object matching the schema (minified).\n"
                f"- Style hint: {style}; iteration={iteration}\n"
                "\nGOALS:\n" + "\n".join(goals or ["<none>"]) +
                "\nRESOURCES/CONSTRAINTS:\n" + "\n".join(resources or ["<none>"]) +
                "\nPRIOR FEEDBACK (score/strengths/gaps/missing_steps):\n" + json.dumps(fb, ensure_ascii=False) +
                "\nCURRENT PLAN JSON:\n" + json.dumps(base, separators=(",", ":"), ensure_ascii=False)
            )
            resp = call_ollama(prompt, model=model, endpoint=endpoint)
            obj = _extract_json(resp)
            if isinstance(obj, dict) and all(k in obj for k in
                ("goals","resources","steps","risks","success_criteria","open_questions")):
                # Keep original explicit goals/resources if the model tries to alter them
                obj["goals"] = goals if goals else obj.get("goals", [])
                obj["resources"] = resources if resources else obj.get("resources", [])
                obj["score"] = int(obj.get("score", 0))
                return obj
        except Exception:
            pass  # fall through

    # --- Deterministic fallback refinement
    refined = json.loads(json.dumps(base))

    # Ensure each goal has at least one step referencing it
    steps = refined.get("steps") or []
    step_ids = {s.get("id") for s in steps if s.get("id")}
    next_idx = 1
    if step_ids:
        try:
            # extract trailing numbers to continue numbering
            nums = []
            for sid in step_ids:
                m = re.search(r"(\d+)$", str(sid))
                if m:
                    nums.append(int(m.group(1)))
            if nums:
                next_idx = max(nums) + 1
        except Exception:
            pass
    existing_titles = [s.get("title","") for s in steps]

    for g in goals:
        if not any(g.lower() in t.lower() for t in existing_titles):
            steps.append({
                "id": f"S{next_idx}",
                "title": g if len(g) < 80 else g[:77] + "...",
                "deps": [steps[-1]["id"]] if steps else [],
                "rationale": "Ensure coverage of stated goal",
                "expected_outcome": g,
            })
            next_idx += 1

    refined["steps"] = steps

    # Ensure success criteria are present & measurable
    sc = set(refined.get("success_criteria") or [])
    sc.add("All steps completed")
    sc.add("No unresolved calls in glyph DB")
    refined["success_criteria"] = sorted(sc)

    # Ensure at least one risk exists
    if not refined.get("risks"):
        refined["risks"] = [{"risk": "Scope creep", "mitigation": "Prioritize MVP; time-box iterations"}]

    # Recompute a simple score lift for the refined output
    rated = rate_plan(refined, goals_text=goals_text, resources_text=resources_text)
    refined["score"] = int(rated.get("score", 0))

    return refined


    
__all__ = ["explain", "status", "impact", "propose", "rate_plan", "refine_plan"]