# src/glyph/app.py
from __future__ import annotations
import sys, shlex
from pathlib import Path
from typing import Dict, List, Tuple
import typer

from . import __version__

app = typer.Typer(add_completion=False, no_args_is_help=True, help="GLYPH — readable C marker & analysis")
dbv = typer.Typer(help="DB ops: init, ingest, show, callers/callees, search, resolve, vacuum")
app.add_typer(dbv, name="dbv")
app.add_typer(dbv, name="db")  # alias

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

@app.callback()
def _version(version: bool = typer.Option(False, "--version", "-V", help="Show version and exit")):
    if version:
        typer.echo(f"glyph {__version__}")
        raise typer.Exit()

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

# src/glyph/app.py — replace the whole dbv_ingest() with this version
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
        """
        Return mapping src_gid -> set of callee names by scanning each fn body.
        Uses byte spans from rewriter entities to slice precisely.
        """
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
            # parse & mark
            res = rewrite_snippet(code, filename=name, extra_args=shlex.split(cflags))
            ents = list(res.entities)
            name2gid = {e.name: e.gid for e in ents if e.kind in ("fn", "prototype")}
            fn_ents = [e for e in ents if e.kind == "fn"]

            # callgraph (AST-based)
            cg = callgraph_snippet(code, filename=name, extra_args=shlex.split(cflags))
            edges: list[tuple[str, str | None, str | None]] = []

            # accumulate edges from callgraph
            added: dict[str, set[str]] = {}
            for src in cg.roots:
                src_name = cg.names.get(src)
                src_gid = name2gid.get(src_name)  # only functions defined in this file
                if not src_gid:
                    continue
                dsts = set()
                for dst in cg.edges.get(src, set()):
                    dst_name = cg.names.get(dst)
                    if not dst_name:
                        continue
                    dst_gid = name2gid.get(dst_name)  # local resolution only
                    edges.append((src_gid, dst_gid, dst_name))
                    dsts.add(dst_name)
                if dsts:
                    added[src_gid] = dsts

            # fallback: textual scan per fn to ensure inter-file calls are recorded
            fb = _fallback_calls(code, fn_ents)
            for src_gid, names in fb.items():
                already = added.get(src_gid, set())
                for dst_name in names - already:
                    dst_gid = name2gid.get(dst_name)
                    edges.append((src_gid, dst_gid, dst_name))

            # ingest
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
        # first line contains name and decl_sig for grep-friendly tests
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
        # 1) exact name hits first (if identifier-like)
        if ident:
            for ent in gdb.lookup_by_name(q):
                if ent.gid in printed: 
                    continue
                printed.add(ent.gid)
                typer.echo(f"{ent.gid}\t{ent.name}\t{ent.decl_sig or ''}")
                if len(printed) >= limit:
                    return
        # 2) FTS fallback, dedupe
        for gid, name, decl in gdb.fts_search(q, limit=limit):
            if gid in printed:
                continue
            printed.add(gid)
            typer.echo(f"{gid}\t{name}\t{decl or ''}")
            if len(printed) >= limit:
                break

# src/glyph/app.py — add a repo scanner that understands Makefiles
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

    # harvest per-file args
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
