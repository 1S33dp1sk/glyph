# src/glyph/app.py
from __future__ import annotations
import sys, shlex
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import typer

from . import __version__

app = typer.Typer(add_completion=False, no_args_is_help=True, help="GLYPH — readable C marker & analysis")
dbv = typer.Typer(help="DB ops: init, ingest, show, callers/callees, search, resolve, vacuum")
ai = typer.Typer(help="LLM-assisted queries over a Glyph DB (Ollama-backed)")
plan = typer.Typer(help="Repo-aware planning: explain, propose, impact, status")
app.add_typer(dbv, name="dbv")
app.add_typer(dbv, name="db")  # alias
app.add_typer(ai, name="ai")
app.add_typer(plan, name="plan")


# ------------- helpers ----------------

def _read_text(p: str) -> str:
    return sys.stdin.read() if p == "-" else Path(p).read_text(encoding="utf-8", errors="ignore")

def _parse_files(specs: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for spec in specs:
        try:
            name, path = spec.split("@", 1)
        except ValueError:
            name, path = (Path(spec).name, spec)
        out[name] = _read_text(path)
    return out

def _parse_items(specs: List[str]) -> List[Tuple[str, str, str]]:
    items: List[Tuple[str, str, str]] = []
    for spec in specs:
        try:
            name, path = spec.split("@", 1)
        except ValueError:
            path = spec
            name = Path(path).name
        items.append((name, path, _read_text(path)))
    return items

# ------------- global options -------------

def _cli_version() -> str:
    import subprocess, sys
    attempts = (
        ["glyph", "--version"],
        [sys.executable, "-m", "glyph", "--version"],
    )
    last_err = None
    for cmd in attempts:
        try:
            r = subprocess.run(cmd, text=True, capture_output=True, check=True)
            out = (r.stdout or r.stderr).strip()
            if out:
                return out  # e.g., "glyph 0.0.1"
        except Exception as e:
            last_err = e
    # Fallback to package version so doctor still passes in dev envs
    try:
        from . import __version__ as _ver
        return f"glyph {_ver}"
    except Exception:
        raise RuntimeError(f"cannot execute glyph CLI: {last_err!r}")

def _version_cb(value: bool):
    if value:
        typer.echo(f"glyph {__version__}")
        raise typer.Exit()

@app.callback(invoke_without_command=True)
def _entrypoint(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show version and exit",
        callback=_version_cb,
        is_eager=True,
    )
):
    # no-op; we only use this to host global options like --version
    return
    
# ------------- core commands -------------

@app.command(help="Rewrite a snippet/file with GLYPH markers")
def rewrite(
    file: str = typer.Option("-", "--file", help="Path or '-' for stdin"),
    name: str = typer.Option("snippet.c", "--name", help="Virtual filename for parsing"),
    cflags: str = typer.Option("", "--cflags", help="Compiler flags, e.g. '-Iinclude -DHAVE_X=1'"),
):
    from .rewriter import rewrite_snippet
    res = rewrite_snippet(_read_text(file), filename=name, extra_args=shlex.split(cflags))
    typer.echo(res.code, nl=False)

@app.command(help="Emit compact JSONL pack for LLMs")
def pack(
    files: List[str] = typer.Option([], "--file", help="name@path (repeatable); if omitted, reads stdin as --name", show_default=False),
    name: str = typer.Option("snippet.c", "--name", help="Name for stdin when used"),
    cflags: str = typer.Option("", "--cflags", help="Compiler flags"),
):
    from .llm_pack import pack_snippets
    snippets = _parse_files(files) if files else {name: sys.stdin.read()}
    out = pack_snippets(snippets, extra_args=shlex.split(cflags))
    typer.echo(out.to_str(), nl=False)

@app.command(help="Summarize entities/calls/gaps across inputs")
def tree(
    files: List[str] = typer.Option([], "--file", help="name@path (repeatable); if omitted, reads stdin as --name", show_default=False),
    name: str = typer.Option("snippet.c", "--name"),
    cflags: str = typer.Option("", "--cflags"),
):
    from .tree_agent import build_units, infer_summary
    snippets = _parse_files(files) if files else {name: sys.stdin.read()}
    units = build_units(snippets, extra_args=shlex.split(cflags))
    summary = infer_summary(units)
    typer.echo(summary.to_json(indent=2))

@app.command(help="Print caller→callee edges for a snippet/file")
def deps(
    file: str = typer.Option("-", "--file"),
    name: str = typer.Option("snippet.c", "--name"),
    cflags: str = typer.Option("", "--cflags"),
):
    from .graph import callgraph_snippet
    cg = callgraph_snippet(_read_text(file), filename=name, extra_args=shlex.split(cflags))
    for src in sorted(cg.roots):
        for dst in sorted(cg.edges.get(src, set())):
            nm = cg.names.get(dst, "")
            typer.echo(f"{src} -> {dst}" + (f"  # {nm}" if nm else ""))

# ------------- dbv subcommands -------------

@dbv.command("init")
def dbv_init(
    db: str = typer.Option(".glyph/idx.sqlite", "--db", help="Database path"),
):
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    from .db import GlyphDB
    with GlyphDB(db) as _:
        pass
    typer.echo(db)

@dbv.command("ingest")
def dbv_ingest(
    files: List[str] = typer.Option(..., "--file", help="name@path (repeatable)"),
    cflags: str = typer.Option("", "--cflags", help="Compiler flags for parsing"),
    db: str = typer.Option(".glyph/idx.sqlite", "--db", help="Database path"),
):
    import re
    from .db import GlyphDB
    from .rewriter import rewrite_snippet, Entity as REntity
    from .graph import callgraph_snippet

    Path(db).parent.mkdir(parents=True, exist_ok=True)
    items = _parse_items(files)

    ident_rx = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
    blacklist = {"if","for","while","switch","return","sizeof","typedef","struct","union","enum"}

    def _fallback_calls(code: str, fns: List[REntity]) -> dict[str, set[str]]:
        calls_by_src: dict[str, set[str]] = {}
        b = code.encode("utf-8", "ignore")
        for e in fns:
            seg = b[e.start:e.end].decode("utf-8", "ignore")
            cands = set(m.group(1) for m in ident_rx.finditer(seg))
            cands.discard(e.name)
            cands.difference_update(blacklist)
            if cands:
                calls_by_src[e.gid] = cands
        return calls_by_src

    with GlyphDB(db) as gdb:
        for name, path, code in items:
            res = rewrite_snippet(code, filename=name, extra_args=shlex.split(cflags))
            ents = list(res.entities)

            # Only resolve to real function definitions
            name2gid_defs: dict[str, str] = {e.name: e.gid for e in ents if e.kind == "fn"}
            fn_ents: List[REntity] = [e for e in ents if e.kind == "fn"]

            cg = callgraph_snippet(code, filename=name, extra_args=shlex.split(cflags))
            edges: List[Tuple[str, str | None, str | None]] = []
            added: dict[str, set[str]] = {}

            # AST-derived edges: src must be a local definition; dst resolves only to defs
            for src in cg.roots:
                src_name = cg.names.get(src)
                src_gid = name2gid_defs.get(src_name)
                if not src_gid:
                    continue
                dsts = set()
                for dst in cg.edges.get(src, set()):
                    dst_name = cg.names.get(dst)
                    if not dst_name:
                        continue
                    dst_gid = name2gid_defs.get(dst_name)  # defs only
                    edges.append((src_gid, dst_gid, dst_name))
                    dsts.add(dst_name)
                if dsts:
                    added[src_gid] = dsts

            # Fallback textual scan: same policy (defs only)
            fb = _fallback_calls(code, fn_ents)
            for src_gid, names in fb.items():
                already = added.get(src_gid, set())
                for dst_name in names - already:
                    dst_gid = name2gid_defs.get(dst_name)  # defs only
                    edges.append((src_gid, dst_gid, dst_name))

            gdb.ingest_file(file_path=path, entities=ents, calls=edges, file_bytes=code.encode("utf-8"))

    typer.echo("ok")

@dbv.command("show")
def dbv_show(
    gid: str = typer.Argument(..., help="GLYPH ID"),
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
):
    from .db import GlyphDB
    with GlyphDB(db) as gdb:
        ent = gdb.get_entity(gid)
        if not ent:
            raise typer.Exit(code=1)
        typer.echo(f"{ent.gid}\t{ent.kind}\t{ent.storage}\t{ent.name}\t{ent.decl_sig or ent.name}")
        typer.echo(f"{ent.file_path}:{ent.start}-{ent.end}")
        if ent.eff_sig:
            typer.echo(ent.eff_sig)

@dbv.command("callers")
def dbv_callers(
    gid: str = typer.Argument(...),
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
):
    from .db import GlyphDB
    with GlyphDB(db) as gdb:
        for s in gdb.callers(gid):
            typer.echo(s)

@dbv.command("callees")
def dbv_callees(
    gid: str = typer.Argument(...),
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
):
    from .db import GlyphDB
    with GlyphDB(db) as gdb:
        for dst_gid, dst_name in gdb.callees(gid):
            typer.echo(dst_gid or f"<unresolved:{dst_name}>")

@dbv.command("search")
def dbv_search(
    q: str = typer.Argument(..., help="Search over names/signatures; exact name preferred"),
    limit: int = typer.Option(50, "--limit"),
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
):
    import re
    from .db import GlyphDB
    ident = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", q) is not None
    printed: set[str] = set()
    with GlyphDB(db) as gdb:
        if ident:
            for ent in gdb.lookup_by_name(q):
                if ent.gid in printed:
                    continue
                printed.add(ent.gid)
                typer.echo(f"{ent.gid}\t{ent.name}\t{ent.decl_sig or ''}")
                if len(printed) >= limit:
                    return
        for gid, name, decl in gdb.fts_search(q, limit=limit):
            if gid in printed:
                continue
            printed.add(gid)
            typer.echo(f"{gid}\t{name}\t{decl or ''}")
            if len(printed) >= limit:
                break

# ------------- repo scanner (Make-aware) -------------

@app.command(help="Scan a repo, optionally via make -nB, rewrite, and ingest DB")
def scan(
    root: str = typer.Option(".", "--root"),
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
    mirror: str | None = typer.Option(None, "--mirror", help="Mirror rewritten files to dir"),
    make: str | None = typer.Option(None, "--make", help="e.g. 'make -nB all' to harvest flags"),
    target: str | None = typer.Option(None, "--target", help="make target when using --make"),
    ext: str = typer.Option(".c,.h,.cc,.cpp,.cxx", "--ext"),
    ignore: str = typer.Option(".git,.glyph,build", "--ignore"),
    cflags: str = typer.Option("", "--cflags", help="Fallback flags when none harvested"),
):
    from .db import GlyphDB
    from .rewriter import rewrite_snippet
    from .mkparse import extract_compile_commands

    rootp = Path(root).resolve()
    ig = set(x.strip() for x in ignore.split(",") if x.strip())
    exts = tuple(x.strip() for x in ext.split(",") if x.strip())

    per_file: Dict[str, List[str]] = {}
    if make:
        per_file = extract_compile_commands(str(rootp), shlex.split(make), target)

    files: List[Path] = []
    for p in rootp.rglob("*"):
        if not p.is_file(): continue
        if any(part in ig for part in p.parts): continue
        if p.suffix.lower() in exts:
            files.append(p)

    Path(db).parent.mkdir(parents=True, exist_ok=True)
    if mirror:
        Path(mirror).mkdir(parents=True, exist_ok=True)

    with GlyphDB(db) as gdb:
        for fp in files:
            code = fp.read_text(encoding="utf-8", errors="ignore")
            args = per_file.get(str(fp.resolve()), shlex.split(cflags))
            res = rewrite_snippet(code, filename=fp.name, extra_args=args)
            gdb.ingest_file(file_path=str(fp), entities=res.entities, calls=(), file_bytes=code.encode("utf-8"))
            if mirror:
                outp = Path(mirror) / fp.relative_to(rootp)
                outp.parent.mkdir(parents=True, exist_ok=True)
                outp.write_text(res.code, encoding="utf-8")

# ------------- db maintenance -------------

@dbv.command("resolve")
def dbv_resolve(
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
):
    from .db import GlyphDB
    with GlyphDB(db) as gdb:
        n = gdb.resolve_unlinked_calls()
        typer.echo(str(n))

@dbv.command("vacuum")
def dbv_vacuum(
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
):
    from .db import GlyphDB
    with GlyphDB(db) as gdb:
        gdb.vacuum()
        typer.echo("ok")

@dbv.command("analyze")
def dbv_analyze(
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
):
    from .db import GlyphDB
    with GlyphDB(db) as gdb:
        gdb.analyze()
        typer.echo("ok")

# ------------- git integration -------------

git = typer.Typer(help="Git integration: plan/apply/snapshot")
app.add_typer(git, name="git")

@git.command("plan", help="Create/switch to branch, install hooks, init DB")
def git_plan(
    branch: str = typer.Option(..., "--branch"),
    base: str | None = typer.Option(None, "--base"),
    strict: bool = typer.Option(False, "--strict"),
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
    mirror: str = typer.Option(".glyph/mirror", "--mirror"),
    make: str | None = typer.Option(None, "--make"),
    cflags: str | None = typer.Option(None, "--cflags"),
    root: str = typer.Option(".", "--root"),
):
    from .gitvc import plan_branch
    res = plan_branch(root, branch, base, db_path=db, mirror_dir=mirror,
                      make_cmd=make, cflags=cflags, strict_hooks=strict)
    # minimal, deterministic output
    typer.echo(f"branch: {res.branch}\npre-commit: {res.pre_commit}\npost-merge: {res.post_merge}\nDB: {res.db_path}\nmirror: {res.mirror_dir}")

@git.command("snapshot", help="Create/replace annotated tag for current DB state")
def git_snapshot(
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
    root: str = typer.Option(".", "--root"),
    prefix: str = typer.Option("glyph/db", "--prefix"),
):
    from .gitvc import tag_db_snapshot
    tag = tag_db_snapshot(root, db, prefix=prefix)
    typer.echo(tag, nl=False)  # no trailing newline

@git.command("apply", help="Stage .glyph, commit allow-empty, tag snapshot; prints tag")
def git_apply(
    message: str = typer.Option("glyph: snapshot", "--message"),
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
    mirror: str = typer.Option(".glyph/mirror", "--mirror"),
    root: str = typer.Option(".", "--root"),
    prefix: str = typer.Option("glyph/db", "--prefix"),
):
    from .gitvc import apply_snapshot
    tag = apply_snapshot(root, db_path=db, mirror_dir=mirror, message=message, tag_prefix=prefix)
    typer.echo(tag, nl=False)  # no trailing newline

@git.command("push", help="Push branch and tags to remote")
def git_push(
    remote: str = typer.Option("origin", "--remote"),
    branch: str | None = typer.Option(None, "--branch"),
    root: str = typer.Option(".", "--root"),
):
    from .gitvc import push_with_tags
    push_with_tags(root, remote=remote, branch=branch)


@app.command(help="Check glyph health: env, libclang, sqlite FTS5, bundled tests")
def doctor(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show details for all checks"),
):
    from .doctor import run as _run_doctor
    code = _run_doctor(verbose=verbose)
    raise typer.Exit(code)


@app.command(help="Scour a codebase and emit a full summary (files, entities, calls)")
def summary(
    root: str = typer.Option(".", "--root"),
    make: str | None = typer.Option(None, "--make", help="e.g. 'make -nB all' to harvest per-file flags"),
    target: str | None = typer.Option(None, "--target", help="make target for --make"),
    cflags: str = typer.Option("", "--cflags", help="Fallback compiler flags"),
    ext: str = typer.Option(".c,.h,.cc,.cpp,.cxx,.hpp,.hh,.hxx", "--ext"),
    ignore: str = typer.Option(".git,.glyph,build", "--ignore"),
    pretty: bool = typer.Option(True, "--pretty/--no-pretty"),
):
    from .summary import summarize_repo
    res = summarize_repo(
        root,
        make_cmd=make,
        make_target=target,
        cflags=cflags,
        ext_csv=ext,
        ignore_csv=ignore,
    )
    typer.echo(res.to_json(indent=2 if pretty else 0), nl=True)


@ai.command("ask")
def ai_ask(
    q: str = typer.Argument(..., help="Natural-language question, e.g. 'what calls add_int?'"),
    db: str = typer.Option(".glyph/idx.sqlite", "--db", help="Path to Glyph DB"),
    k: int = typer.Option(6, "--k", help="Seed results to retrieve"),
    hops: int = typer.Option(1, "--hops", help="Neighbor expansion around seeds"),
    model: str = typer.Option("gpt-oss:20b", "--model", help="Ollama model name"),
    endpoint: str = typer.Option("http://localhost:11434", "--endpoint", help="Ollama HTTP endpoint"),
    max_chars: int = typer.Option(14000, "--max-chars", help="Context size cap"),
):
    from .intel import answer_question
    out = answer_question(db, q, k=k, hops=hops, model=model, endpoint=endpoint, max_chars=max_chars)
    typer.echo(out)

@plan.command("explain", help="Explain the codebase (basic DB metrics, optional AI summary)")
def plan_explain(
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
    ai: bool = typer.Option(False, "--ai", help="Use AI to generate a natural-language summary"),
    model: str = typer.Option("gpt-oss:20b", "--model"),
    endpoint: str = typer.Option("http://localhost:11434", "--endpoint"),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON (repo metrics)"),
):
    import json as _json
    from .plan import explain as plan_explain_basic

    metrics = plan_explain_basic(db)  # dict: {files, entities_by_kind, unresolved_calls}

    if json_out:
        typer.echo(_json.dumps(metrics, indent=2))
        return

    # Minimal markdown view (deterministic)
    ents = ", ".join(f"{k}:{v}" for k, v in sorted(metrics.get("entities_by_kind", {}).items()))
    md = [
        "# Repo summary",
        f"- Files: {metrics.get('files', 0)}",
        f"- Unresolved calls: {metrics.get('unresolved_calls', 0)}",
        f"- Entities by kind: {ents or '(none)'}",
    ]
    typer.echo("\n".join(md))

    if ai:
        # Inline AI summary using ollama via intel.call_ollama
        try:
            from .intel import call_ollama
            prompt = (
                "You are a concise codebase analyst. Using ONLY the JSON metrics below, "
                "write a short 2–4 sentence summary of the repository health and risks. "
                "Do not invent details.\n\n"
                f"METRICS: {_json.dumps(metrics, separators=(',',':'))}\n\n"
                "Summary:"
            )
            typer.echo("\n--- AI summary ---\n")
            typer.echo(call_ollama(prompt, model=model, endpoint=endpoint).strip())
        except Exception as e:
            typer.echo(f"\n--- AI summary (unavailable) ---\n{e}", err=True)


@plan.command("propose", help="Draft a repo-aware plan; schema-first (AI optional)")
def plan_propose(
    goals: str = typer.Option(..., "--goals", help='Numbered goals, e.g. "1) …; 2) …" or multi-line'),
    resources: str = typer.Option("", "--resources", help='Constraints/tools, e.g. "X, Y, Z" or multi-line'),
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
    model: str = typer.Option("", "--model", help="Ollama model (optional)"),
    endpoint: str = typer.Option("http://localhost:11434", "--endpoint"),
    max_iters: int = typer.Option(3, "--max-iters"),
    fallback_after: int = typer.Option(2, "--fallback-after"),
    fallback_threshold: int = typer.Option(70, "--fallback-threshold"),
    verbose: bool = typer.Option(False, "--verbose"),
    md: bool = typer.Option(False, "--md", help="Also render markdown-ish view"),
):
    import json as _json
    from .plan import propose as plan_propose_fn

    plan = plan_propose_fn(
        db_path=db,
        goals_text=goals,
        resources_text=resources,
        model=(model or None),
        endpoint=(endpoint if model else None),
        max_iters=max_iters,
        fallback_after=fallback_after,
        fallback_threshold=fallback_threshold,
    )
    typer.echo(_json.dumps(plan, indent=2))

    if md:
        # lightweight markdown rendering
        lines = ["# Proposed plan"]
        if plan.get("goals"):
            lines.append("## Goals")
            lines += [f"- {g}" for g in plan["goals"]]
        if plan.get("steps"):
            lines.append("## Steps")
            for s in plan["steps"]:
                deps = f" (deps: {', '.join(s.get('deps', []))})" if s.get("deps") else ""
                lines.append(f"- **{s.get('id','?')}**: {s.get('title','')} {deps}")
        if plan.get("risks"):
            lines.append("## Risks")
            for r in plan["risks"]:
                lines.append(f"- {r.get('risk','')} — _mitigation_: {r.get('mitigation','')}")
        typer.echo("\n".join(lines))


@plan.command("impact", help="Show callers/callees blast radius for a symbol")
def plan_impact(
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
    symbol: Optional[str] = typer.Option(None, "--symbol"),
    json_out: bool = typer.Option(False, "--json"),
    verbose: bool = typer.Option(False, "--verbose"),
):
    import json as _json
    from .plan import impact as plan_impact_fn

    if not symbol:
        typer.echo("error: --symbol is required", err=True)
        raise typer.Exit(2)

    try:
        rep = plan_impact_fn(db, symbol)
        if json_out:
            typer.echo(_json.dumps(rep, indent=2))
        else:
            # simple text view
            lines = [f"target: {rep.get('target')}"]
            lines.append(f"entities: {', '.join(rep.get('entities', [])) or '(none)'}")
            lines.append("callers:")
            callers = rep.get("callers", {})
            if callers:
                for k, v in callers.items():
                    lines.append(f"  {k}: {', '.join(v) if v else '(none)'}")
            else:
                lines.append("  (none)")
            typer.echo("\n".join(lines))
    except Exception as e:
        # deterministic JSON error payload (our tests may rely on JSON)
        out = {"target": symbol, "entities": [], "callers": {}, "by_name": {}, "error": str(e)}
        if verbose:
            typer.echo(f"[impact:error] {e}", err=True)
        typer.echo(_json.dumps(out))


@plan.command("status", help="Evaluate a plan.json against current repo signals")
def plan_status(
    db: str = typer.Option(".glyph/idx.sqlite", "--db"),
    plan_json: str = typer.Option(..., "--plan", help="Path to plan.json"),
):
    import json as _json
    from .plan import status as plan_status_fn
    out = plan_status_fn(db, plan_json)
    typer.echo(_json.dumps(out, indent=2))