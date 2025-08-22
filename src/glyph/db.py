# src/glyph/db.py
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence, Tuple

from .rewriter import Entity
import itertools
_sp_counter = itertools.count()


# --- schema/versioning -------------------------------------------------------

_SCHEMA_VERSION = 7
_ROW_CHUNK = 1000


def _connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-80000")
    conn.execute("PRAGMA recursive_triggers=ON")
    return conn


# Base schema (tables + indexes; FTS is (re)created here too)
_BASE_SQL = f"""
-- drop old FTS + triggers unconditionally (handles prior contentless installs)
DROP TRIGGER IF EXISTS trg_entities_fts_upsert;
DROP TRIGGER IF EXISTS trg_entities_fts_update;
DROP TRIGGER IF EXISTS trg_entities_fts_delete;
DROP TABLE   IF EXISTS entities_fts;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  path    TEXT NOT NULL UNIQUE,
  mtime   REAL,
  size    INTEGER,
  sha256  TEXT
);

CREATE TABLE IF NOT EXISTS entities (
  gid       TEXT PRIMARY KEY,
  kind      TEXT NOT NULL,
  name      TEXT NOT NULL,
  storage   TEXT NOT NULL,
  decl_sig  TEXT,
  eff_sig   TEXT,
  sig_id    TEXT,     -- v6
  linkage   TEXT,     -- v6
  file_id   INTEGER NOT NULL,
  start     INTEGER NOT NULL,
  "end"     INTEGER NOT NULL,
  FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
  DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS idx_entities_file ON entities(file_id, start);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);

-- calls: keep legacy shape; callsite_id is present in v6
CREATE TABLE IF NOT EXISTS calls (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  src_gid   TEXT NOT NULL,
  dst_gid   TEXT,
  dst_name  TEXT,
  callsite_id INTEGER,
  FOREIGN KEY(src_gid) REFERENCES entities(gid) ON DELETE CASCADE
  DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY(dst_gid) REFERENCES entities(gid) ON DELETE SET NULL
  DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS idx_calls_src ON calls(src_gid);
CREATE INDEX IF NOT EXISTS idx_calls_dst ON calls(dst_gid);
CREATE INDEX IF NOT EXISTS idx_calls_callsite ON calls(callsite_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_calls_norm
  ON calls(src_gid, IFNULL(dst_gid,''), IFNULL(dst_name,''));

-- callsites (multi-target call expression sites)
CREATE TABLE IF NOT EXISTS callsites (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  src_gid    TEXT NOT NULL,
  kind       TEXT NOT NULL,     -- 'direct' | 'fp' | 'unknown'
  name_hint  TEXT,
  expr       TEXT,
  sig_id     TEXT,
  FOREIGN KEY(src_gid) REFERENCES entities(gid) ON DELETE CASCADE
  DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS idx_callsites_src ON callsites(src_gid);
CREATE UNIQUE INDEX IF NOT EXISTS uq_callsites_src_name_kind
  ON callsites(src_gid, IFNULL(name_hint,''), kind);

-- candidates per callsite (multi-target)
CREATE TABLE IF NOT EXISTS call_candidates (
  callsite_id  INTEGER NOT NULL,
  dst_gid      TEXT NOT NULL,
  rank         REAL DEFAULT 0.0,
  PRIMARY KEY (callsite_id, dst_gid),
  FOREIGN KEY(callsite_id) REFERENCES callsites(id) ON DELETE CASCADE
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY(dst_gid) REFERENCES entities(gid) ON DELETE CASCADE
    DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS idx_call_candidates_dst ON call_candidates(dst_gid);

-- FTS5 (external-content) + correct 'delete' triggers
CREATE VIRTUAL TABLE entities_fts USING fts5(
  gid UNINDEXED, name, decl_sig, eff_sig,
  content='entities', content_rowid='rowid',
  tokenize='unicode61'
);

CREATE TRIGGER trg_entities_fts_upsert
AFTER INSERT ON entities BEGIN
  INSERT INTO entities_fts(rowid, gid, name, decl_sig, eff_sig)
  VALUES (new.rowid, new.gid, new.name, new.decl_sig, new.eff_sig);
END;

CREATE TRIGGER trg_entities_fts_update
AFTER UPDATE ON entities BEGIN
  INSERT INTO entities_fts(entities_fts, rowid, gid, name, decl_sig, eff_sig)
  VALUES('delete', old.rowid, old.gid, old.name, old.decl_sig, old.eff_sig);
  INSERT INTO entities_fts(rowid, gid, name, decl_sig, eff_sig)
  VALUES (new.rowid, new.gid, new.name, new.decl_sig, new.eff_sig);
END;

CREATE TRIGGER trg_entities_fts_delete
AFTER DELETE ON entities BEGIN
  INSERT INTO entities_fts(entities_fts, rowid, gid, name, decl_sig, eff_sig)
  VALUES('delete', old.rowid, old.gid, old.name, old.decl_sig, old.eff_sig);
END;

INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', CAST({_SCHEMA_VERSION} AS TEXT));
"""

# FTS5 (external-content) + triggers (rebuilt idempotently)
_FTS_SQL = """
DROP TRIGGER IF EXISTS trg_entities_fts_upsert;
DROP TRIGGER IF EXISTS trg_entities_fts_update;
DROP TRIGGER IF EXISTS trg_entities_fts_delete;
DROP TABLE   IF EXISTS entities_fts;

CREATE VIRTUAL TABLE entities_fts USING fts5(
  gid UNINDEXED, name, decl_sig, eff_sig,
  content='entities', content_rowid='rowid',
  tokenize='unicode61'
);

CREATE TRIGGER trg_entities_fts_upsert
AFTER INSERT ON entities BEGIN
  INSERT INTO entities_fts(rowid, gid, name, decl_sig, eff_sig)
  VALUES (new.rowid, new.gid, new.name, new.decl_sig, new.eff_sig);
END;

CREATE TRIGGER trg_entities_fts_update
AFTER UPDATE ON entities BEGIN
  INSERT INTO entities_fts(entities_fts, rowid, gid, name, decl_sig, eff_sig)
  VALUES('delete', old.rowid, old.gid, old.name, old.decl_sig, old.eff_sig);
  INSERT INTO entities_fts(rowid, gid, name, decl_sig, eff_sig)
  VALUES (new.rowid, new.gid, new.name, new.decl_sig, new.eff_sig);
END;

CREATE TRIGGER trg_entities_fts_delete
AFTER DELETE ON entities BEGIN
  INSERT INTO entities_fts(entities_fts, rowid, gid, name, decl_sig, eff_sig)
  VALUES('delete', old.rowid, old.gid, old.name, old.decl_sig, old.eff_sig);
END;
"""


def _infer_linkage(storage: str) -> str:
    """Heuristic until rewriter starts emitting linkage explicitly."""
    s = (storage or "").lower()
    return "internal" if s.startswith("static") else "external"


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _ensure_base(conn: sqlite3.Connection) -> None:
    conn.executescript(_BASE_SQL)

def _ensure_fts(conn: sqlite3.Connection) -> None:
    conn.executescript(_FTS_SQL)

def _maybe_add_column(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

def _exec_base(conn: sqlite3.Connection) -> None:
    conn.executescript(_BASE_SQL)

def _exec_schema(conn: sqlite3.Connection) -> None:
    """
    Create/upgrade the schema to _SCHEMA_VERSION.
    Also backfills new columns if upgrading from older DBs and rebuilds FTS if empty.
    """
    conn.executescript(_BASE_SQL)

    # --- Post-creation migrations for existing DBs (add missing columns) ----
    def _cols(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    ent_cols = _cols("entities")
    if "sig_id" not in ent_cols:
        conn.execute("ALTER TABLE entities ADD COLUMN sig_id TEXT")
    if "linkage" not in ent_cols:
        conn.execute("ALTER TABLE entities ADD COLUMN linkage TEXT")

    call_cols = _cols("calls")
    if "callsite_id" not in call_cols:
        conn.execute("ALTER TABLE calls ADD COLUMN callsite_id INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_callsite ON calls(callsite_id)")

    # If FTS exists but is empty, rebuild from content
    try:
        cnt = conn.execute("SELECT count(*) AS c FROM entities_fts").fetchone()[0]
        if not cnt:
            conn.execute("INSERT INTO entities_fts(entities_fts) VALUES('rebuild')")
    except sqlite3.Error:
        # table might not exist yet; ignore (next init will create it)
        pass


# ---------- small utils ------------------------------------------------------

def _fts_expr_from_text(q: str, max_terms: int = 6) -> str:
    """
    Convert natural language to a safe, high-recall FTS5 query.
    Keep identifier-like tokens; join with OR; prefix matches.
    """
    toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", q)
    if not toks:
        return ""
    banned = {"and", "or", "not", "near"}
    out = []
    seen = set()
    for t in toks:
        tl = t.lower()
        if tl in banned:
            continue
        if "_" in t or len(t) >= 4:
            if t not in seen:
                seen.add(t)
                out.append(t)
        if len(out) >= max_terms:
            break
    if not out:
        return ""
    return " OR ".join(f"{t}*" for t in out)


def _canon_path(p: str | os.PathLike[str]) -> str:
    try:
        return str(Path(p).resolve())
    except Exception:
        return str(Path(p))


def _file_stat(path: Path, data: Optional[bytes]) -> tuple[float | None, int | None, str | None]:
    try:
        st = path.stat()
        mtime = float(st.st_mtime)
        size = int(st.st_size)
    except FileNotFoundError:
        mtime = None
        size = None
    sha = None
    if data is not None:
        h = hashlib.sha256()
        h.update(data)
        sha = h.hexdigest()
    return mtime, size, sha


def _chunked(seq: Iterable[tuple], n: int = _ROW_CHUNK) -> Iterator[list[tuple]]:
    buf: list[tuple] = []
    for it in seq:
        buf.append(it)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


# ---------- types ------------------------------------------------------------

@dataclass(frozen=True)
class DbEntity:
    gid: str
    kind: str
    name: str
    storage: str
    linkage: str     # v6
    sig_id: str      # v6
    decl_sig: str
    eff_sig: str
    file_path: str
    start: int
    end: int


CallEdge = Tuple[str, Optional[str], Optional[str]]  # (src_gid, dst_gid|None, dst_name|None)


# ---------- main API ---------------------------------------------------------

class GlyphDB:
    """
    SQLite-backed index:
      - files: path metadata (mtime/size/sha256)
      - entities: GLYPH IDs + metadata (now includes sig_id, linkage)
      - calls: src_gid → dst_gid | dst_name
      - entities_fts: FTS5 search over names/signatures
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.path = str(db_path)
        self.conn = _connect(self.path)
        self._ensure_schema()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "GlyphDB":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        sp = f"glyph_tx_{next(_sp_counter)}"
        try:
            self.conn.execute(f"SAVEPOINT {sp}")
            yield self.conn
            # Release (completes the savepoint); tolerate if already gone
            try:
                self.conn.execute(f"RELEASE SAVEPOINT {sp}")
            except sqlite3.OperationalError:
                # The savepoint may already have been closed by an inner COMMIT/ROLLBACK.
                pass
        except Exception:
            # Best-effort rollback to the savepoint if it still exists
            try:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            except sqlite3.OperationalError:
                pass
            # Always try to release; ignore if already gone
            try:
                self.conn.execute(f"RELEASE SAVEPOINT {sp}")
            except sqlite3.OperationalError:
                pass
            raise

    # ----- schema / migrations -----
    def _ensure_schema(self) -> None:
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            current = int(row["value"]) if row else None
        except sqlite3.Error:
            current = None
        if current != _SCHEMA_VERSION:
            _exec_schema(self.conn)

    # ----- files -----

    def upsert_file(self, path: str | os.PathLike[str], data: Optional[bytes] = None) -> int:
        p = _canon_path(path)
        mtime, size, sha = _file_stat(Path(p), data)
        self.conn.execute(
            """
            INSERT INTO files(path, mtime, size, sha256)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
              mtime=excluded.mtime,
              size=excluded.size,
              sha256=COALESCE(excluded.sha256, files.sha256)
            """,
            (p, mtime, size, sha),
        )
        return int(self.conn.execute("SELECT id FROM files WHERE path=?", (p,)).fetchone()["id"])

    def ingest_file(
        self,
        file_path: str | os.PathLike[str],
        entities: Sequence[Entity],
        calls: Iterable[CallEdge] = (),
        file_bytes: Optional[bytes] = None,
        replace_file_entities: bool = True,
        includes: Optional[Iterable[str | tuple[str, str]]] = None,  # NEW
    ) -> None:
        """
        Upsert file metadata, (optionally) replace all entities for that file,
        and insert normalized call edges. Also maintains callsites/candidates:
          - clears callsites for caller functions in this file when replacing entities
          - creates callsites(kind='direct') for unresolved direct calls (by name)
          - populates candidates for those callsites
        """
        p = _canon_path(file_path)

        # normalize includes -> List[Tuple[str, str]]
        incl_pairs: list[tuple[str, str]] = []
        if includes:
            for it in includes:
                if isinstance(it, tuple):
                    dst, kind = it
                else:
                    dst, kind = str(it), ""     # empty kind by default
                incl_pairs.append((_canon_path(dst), kind))

        with self.tx():
            fid = self.upsert_file(p, data=file_bytes)

            old_rows = self.conn.execute("SELECT gid FROM entities WHERE file_id=?", (fid,)).fetchall()
            old_gids = [r["gid"] for r in old_rows] if old_rows else []

            if replace_file_entities:
                if old_gids:
                    self.clear_calls_from(old_gids)
                    # self.clear_callsites_from(old_gids)  # keep if you have callsites
                self.remove_entities_for_file(fid)

            self.upsert_entities(fid, entities)

            # persist include edges for this file (idempotent)
            if incl_pairs:
                self.set_includes_for_file(p, incl_pairs)

            new_rows = self.conn.execute("SELECT gid FROM entities WHERE file_id=?", (fid,)).fetchall()
            new_gids = [r["gid"] for r in new_rows] if new_rows else []

            if calls:
                self.insert_calls(calls)

            if new_gids:
                try:
                    self.link_calls_to_callsites(new_gids)
                    self.populate_candidates(only_src_gids=new_gids)
                except Exception:
                    pass

    def bulk_ingest(
        self,
        items: Iterable[tuple],  # allow 4-tuple legacy and 5-tuple with includes
    ) -> None:
        with self.tx():
            for item in items:
                # Legacy: (file_path, entities, calls, file_bytes)
                # New:    (file_path, entities, calls, file_bytes, includes)
                if len(item) == 4:
                    file_path, entities, calls, data = item
                    includes = None
                elif len(item) == 5:
                    file_path, entities, calls, data, includes = item
                else:
                    raise ValueError("bulk_ingest expects 4- or 5-tuple per item")
                # Use the single-file API so the logic stays in one place
                self.ingest_file(
                    file_path=file_path,
                    entities=entities,
                    calls=calls,
                    file_bytes=data,
                    replace_file_entities=True,
                    includes=includes,
                )

            
    def _file_id_by_path(self, p: str) -> Optional[int]:
        row = self.conn.execute("SELECT id FROM files WHERE path=?", (p,)).fetchone()
        return int(row["id"]) if row else None

    def _ensure_file_row(self, p: str) -> int:
        """
        Make sure 'files' has a row for p (absolute path). We create a row even if
        the file wasn't ingested yet, so include edges can land. Metadata (mtime/sha)
        will be null until real ingest.
        """
        canon = _canon_path(p)
        # INSERT OR IGNORE, then fetch id
        self.conn.execute(
            "INSERT OR IGNORE INTO files(path, mtime, size, sha256) VALUES(?, NULL, NULL, NULL)",
            (canon,),
        )
        return int(self.conn.execute("SELECT id FROM files WHERE path=?", (canon,)).fetchone()["id"])

    def clear_includes_for_file(self, src_file_path: str | os.PathLike[str]) -> None:
        fid = self._file_id_by_path(_canon_path(src_file_path))
        if fid is None:
            return
        self.conn.execute("DELETE FROM includes WHERE src_file_id=?", (fid,))

    def set_includes_for_file(self, src_file_path: str | os.PathLike[str],
                              includes: Iterable[tuple[str, str]]) -> None:
        """
        includes: iterable of (resolved_path, kind)
        - Clears previous include edges for this src file
        - Upserts rows into 'files' for destination paths (if missing)
        - Inserts edges into 'includes'
        """
        src = _canon_path(src_file_path)
        with self.tx():
            src_fid = self._ensure_file_row(src)
            # clear previous edges
            self.conn.execute("DELETE FROM includes WHERE src_file_id=?", (src_fid,))
            # insert new edges
            rows = []
            for dst_path, kind in includes:
                if not dst_path:
                    continue
                dst_fid = self._ensure_file_row(dst_path)
                rows.append((src_fid, dst_fid, kind or ""))
            if rows:
                for chunk in _chunked(rows):
                    self.conn.executemany(
                        "INSERT OR IGNORE INTO includes(src_file_id, dst_file_id, kind) VALUES(?,?,?)",
                        chunk,
                    )

    def affected_files(self, changed_paths: Iterable[str], *,
                       transitive: bool = True,
                       include_self: bool = True) -> list[str]:
        """
        Compute reverse-include closure: all files that (directly/transitively)
        include ANY of the changed paths.
        Returns canonical absolute paths (strings), unique, stable order by path.
        """
        seeds = { _canon_path(p) for p in changed_paths if p }
        if not seeds:
            return []

        # Map paths -> ids (only those we know about)
        id_of: dict[str, int] = {}
        for p in list(seeds):
            fid = self._file_id_by_path(p)
            if fid is not None:
                id_of[p] = fid

        if not id_of:
            # nothing in DB references these (yet)
            return list(sorted(seeds)) if include_self else []

        # BFS on reverse edges
        frontier = set(id_of.values())
        seen_ids = set(frontier) if include_self else set()
        while frontier:
            cur = tuple(frontier)
            frontier = set()
            qmarks = ",".join("?" for _ in cur)
            for row in self.conn.execute(
                f"SELECT DISTINCT src_file_id FROM includes WHERE dst_file_id IN ({qmarks})",
                cur,
            ):
                sid = int(row["src_file_id"])
                if sid not in seen_ids:
                    seen_ids.add(sid)
                    if transitive:
                        frontier.add(sid)

            if not transitive:
                break

        # Convert back to paths
        paths = []
        if seen_ids:
            qmarks = ",".join("?" for _ in seen_ids)
            for r in self.conn.execute(f"SELECT path FROM files WHERE id IN ({qmarks})", tuple(seen_ids)):
                paths.append(str(r["path"]))

        # Ensure changed_paths themselves are present if requested
        if include_self:
            for p in seeds:
                if p not in paths:
                    paths.append(p)

        # Stable
        return sorted(set(paths))

    # ----- entities -----

    def upsert_entities(self, file_id: int, entities: Sequence[Entity]) -> None:
        rows = []
        for e in entities:
            # tolerate older Entity without sig_id/linkage
            sig_id = getattr(e, "sig_id", None)
            linkage = getattr(e, "linkage", None) or _infer_linkage(getattr(e, "storage", "extern"))
            rows.append((
                e.gid, e.kind, e.name, e.storage,
                e.decl_sig, e.eff_sig, sig_id, linkage,
                file_id, int(e.start), int(e.end),
            ))
        for chunk in _chunked(rows):
            self.conn.executemany(
                """
                INSERT INTO entities(gid, kind, name, storage, decl_sig, eff_sig, sig_id, linkage, file_id, start, "end")
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(gid) DO UPDATE SET
                  kind=excluded.kind,
                  name=excluded.name,
                  storage=excluded.storage,
                  decl_sig=excluded.decl_sig,
                  eff_sig=excluded.eff_sig,
                  sig_id=excluded.sig_id,
                  linkage=excluded.linkage,
                  file_id=excluded.file_id,
                  start=excluded.start,
                  "end"=excluded."end"
                """,
                chunk,
            )

    def remove_entities_for_file(self, file_id: int) -> None:
        self.conn.execute("DELETE FROM entities WHERE file_id=?", (file_id,))

    # ----- callsites -----

    def clear_callsites_from(self, src_gids: Iterable[str]) -> None:
        """Remove callsites (and their candidates) for the given caller functions."""
        for chunk in _chunked([(g,) for g in src_gids]):
            self.conn.executemany("DELETE FROM callsites WHERE src_gid=?", chunk)

    def ensure_callsite(self, *, src_gid: str, kind: str, name_hint: Optional[str] = None,
                        expr: Optional[str] = None, sig_id: Optional[str] = None) -> int:
        """
        Get-or-create a callsite id for (src_gid, kind, name_hint).
        We keep a UNIQUE index on (src_gid, name_hint, kind) to avoid dupes.
        """
        row = self.conn.execute(
            "SELECT id FROM callsites WHERE src_gid=? AND IFNULL(name_hint,'')=IFNULL(?, '') AND kind=?",
            (src_gid, name_hint, kind),
        ).fetchone()
        if row:
            return int(row["id"])
        self.conn.execute(
            "INSERT INTO callsites(src_gid, kind, name_hint, expr, sig_id) VALUES(?,?,?,?,?)",
            (src_gid, kind, name_hint, expr, sig_id),
        )
        return int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def link_calls_to_callsites(self, src_gids: Iterable[str]) -> None:
        """
        For unresolved direct calls (dst_gid IS NULL AND dst_name NOT NULL) in the
        given caller set, create/attach callsites(kind='direct') and backfill calls.callsite_id.
        """
        src_gids = list(src_gids)
        if not src_gids:
            return

        # create missing callsites for direct unresolved
        self.conn.executemany(
            """
            INSERT INTO callsites(src_gid, kind, name_hint)
            SELECT DISTINCT c.src_gid, 'direct', c.dst_name
            FROM calls c
            WHERE c.src_gid=? AND c.dst_gid IS NULL AND c.dst_name IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM callsites s
                WHERE s.src_gid=c.src_gid AND s.kind='direct' AND IFNULL(s.name_hint,'')=IFNULL(c.dst_name,'')
              )
            """,
            [(g,) for g in src_gids]
        )

        # backfill calls.callsite_id
        self.conn.executemany(
            """
            UPDATE calls
            SET callsite_id = (
              SELECT s.id FROM callsites s
              WHERE s.src_gid=calls.src_gid AND s.kind='direct'
                AND IFNULL(s.name_hint,'')=IFNULL(calls.dst_name,'')
            )
            WHERE calls.src_gid=? AND calls.dst_gid IS NULL AND calls.dst_name IS NOT NULL
            """,
            [(g,) for g in src_gids]
        )

    def populate_candidates(self, *, only_src_gids: Optional[Iterable[str]] = None) -> int:
        """
        For 'direct' callsites, propose all function definitions that share the name.
        Returns number of candidate rows inserted.
        """
        where_src = ""
        params: list = []
        if only_src_gids:
            only_src_gids = list(only_src_gids)
            where_src = " AND s.src_gid IN (%s)" % ",".join("?" * len(only_src_gids))
            params.extend(only_src_gids)

        with self.tx():
            cur = self.conn.execute(
                f"""
                INSERT OR IGNORE INTO call_candidates(callsite_id, dst_gid, rank)
                SELECT s.id, e.gid, 0.0
                FROM callsites s
                JOIN entities e
                  ON e.kind='fn' AND e.name = s.name_hint
                WHERE s.kind='direct' {where_src}
                """,
                params,
            )
            return cur.rowcount or 0

    # ----- calls -----

    def insert_calls(self, edges: Iterable[CallEdge]) -> None:
        seq = list(edges)
        if not seq:
            return
        for chunk in _chunked(seq):
            self.conn.executemany(
                "INSERT OR IGNORE INTO calls(src_gid, dst_gid, dst_name) VALUES(?, ?, ?)",
                chunk,
            )

    def clear_calls_from(self, src_gids: Iterable[str]) -> None:
        self.conn.executemany("DELETE FROM calls WHERE src_gid=?", [(g,) for g in src_gids])

    def resolve_unlinked_calls(self) -> int:
        """
        (a) Populate candidates for ambiguous direct callsites by name.
        (b) Resolve calls uniquely when only one function definition exists by that name.
        Returns number of rows updated in (b).
        """
        # (a) ensure candidates exist globally (idempotent)
        self.populate_candidates()

        sql = """
        WITH defs AS (
          SELECT name, gid
          FROM entities
          WHERE kind='fn'
        ),
        uniq AS (
          SELECT name, gid
          FROM defs
          GROUP BY name
          HAVING COUNT(*) = 1
        )
        UPDATE calls
        SET dst_gid = (SELECT uniq.gid FROM uniq WHERE uniq.name = calls.dst_name)
        WHERE dst_gid IS NULL
          AND EXISTS (SELECT 1 FROM uniq WHERE uniq.name = calls.dst_name)
        """
        with self.tx():
            cur = self.conn.execute(sql)
            return cur.rowcount or 0

    # ----- fetch / lookup -----

    def get_entity(self, gid: str) -> Optional[DbEntity]:
        row = self.conn.execute(
            """
            SELECT e.gid,e.kind,e.name,e.storage,e.linkage,e.sig_id,e.decl_sig,e.eff_sig,
                   f.path AS file_path,e.start,e."end"
            FROM entities e JOIN files f ON e.file_id=f.id
            WHERE e.gid=?""",
            (gid,),
        ).fetchone()
        if not row:
            return None
        return DbEntity(
            gid=row["gid"],
            kind=row["kind"],
            name=row["name"],
            storage=row["storage"],
            linkage=row["linkage"],
            sig_id=row["sig_id"],
            decl_sig=row["decl_sig"],
            eff_sig=row["eff_sig"],
            file_path=row["file_path"],
            start=int(row["start"]),
            end=int(row["end"]),
        )

    def entities_in_file(self, file_path: str | os.PathLike[str]) -> list[DbEntity]:
        p = _canon_path(file_path)
        row = self.conn.execute("SELECT id FROM files WHERE path=?", (p,)).fetchone()
        if not row:
            return []
        fid = int(row["id"])
        cur = self.conn.execute(
            """
            SELECT e.gid,e.kind,e.name,e.storage,e.linkage,e.sig_id,e.decl_sig,e.eff_sig,
                   f.path AS file_path,e.start,e."end"
            FROM entities e JOIN files f ON e.file_id=f.id
            WHERE e.file_id=?
            ORDER BY e.start
            """,
            (fid,),
        )
        return [
            DbEntity(
                gid=r["gid"],
                kind=r["kind"],
                name=r["name"],
                storage=r["storage"],
                linkage=r["linkage"],
                sig_id=r["sig_id"],
                decl_sig=r["decl_sig"],
                eff_sig=r["eff_sig"],
                file_path=r["file_path"],
                start=int(r["start"]),
                end=int(r["end"]),
            )
            for r in cur
        ]

    def callers(self, gid: str) -> list[str]:
        return [r["src_gid"] for r in self.conn.execute("SELECT src_gid FROM calls WHERE dst_gid=?", (gid,))]

    def get_callers(self, gid: str) -> list[str]:
        # Convenience alias used by planner/tests
        return [r["src_gid"] for r in self.conn.execute("SELECT DISTINCT src_gid FROM calls WHERE dst_gid=?", (gid,))]

    def callees(self, gid: str) -> list[tuple[Optional[str], Optional[str]]]:
        return [
            (r["dst_gid"], r["dst_name"])
            for r in self.conn.execute("SELECT dst_gid, dst_name FROM calls WHERE src_gid=?", (gid,))
        ]

    def get_callees(self, gid: str) -> list[tuple[Optional[str], Optional[str]]]:
        # Convenience alias
        return self.callees(gid)

    def lookup_by_name(self, name: str) -> list[DbEntity]:
        cur = self.conn.execute(
            """
            SELECT e.gid,e.kind,e.name,e.storage,e.linkage,e.sig_id,e.decl_sig,e.eff_sig,
                   f.path,e.start,e."end"
            FROM entities e JOIN files f ON e.file_id=f.id
            WHERE e.name=?
            ORDER BY f.path, e.start
            """,
            (name,),
        )
        return [
            DbEntity(
                gid=r["gid"],
                kind=r["kind"],
                name=r["name"],
                storage=r["storage"],
                linkage=r["linkage"],
                sig_id=r["sig_id"],
                decl_sig=r["decl_sig"],
                eff_sig=r["eff_sig"],
                file_path=r["path"],
                start=int(r["start"]),
                end=int(r["end"]),
            )
            for r in cur
        ]

    def fts_search(self, query: str, *, limit: int = 50) -> list[tuple[str, str, str]]:
        """
        FTS query over name/decl_sig/eff_sig.
        Returns list of (gid, name, decl_sig). Robust to NL queries.
        """
        expr = _fts_expr_from_text(query)
        if not expr:
            return []
        try:
            cur = self.conn.execute(
                "SELECT gid, name, decl_sig FROM entities_fts WHERE entities_fts MATCH ? LIMIT ?",
                (expr, int(limit)),
            )
            return [(r["gid"], r["name"], r["decl_sig"]) for r in cur]
        except sqlite3.OperationalError:
            # conservative LIKE fallback
            like = f"%{query.strip()}%"
            cur = self.conn.execute(
                "SELECT gid, name, decl_sig FROM entities "
                "WHERE name LIKE ? OR decl_sig LIKE ? LIMIT ?",
                (like, like, int(limit)),
            )
            return [(r["gid"], r["name"], r["decl_sig"]) for r in cur]

    def lookup_span(self, file_path: str | os.PathLike[str], offset: int) -> Optional[DbEntity]:
        p = _canon_path(file_path)
        row = self.conn.execute("SELECT id FROM files WHERE path=?", (p,)).fetchone()
        if not row:
            return None
        fid = int(row["id"])
        r = self.conn.execute(
            """
            SELECT e.gid,e.kind,e.name,e.storage,e.linkage,e.sig_id,e.decl_sig,e.eff_sig,
                   f.path AS file_path,e.start,e."end"
            FROM entities e JOIN files f ON e.file_id=f.id
            WHERE e.file_id=? AND e.start<=? AND e."end">=?
            ORDER BY (e."end"-e.start) ASC LIMIT 1
            """,
            (fid, int(offset), int(offset)),
        ).fetchone()
        if not r:
            return None
        return DbEntity(
            gid=r["gid"],
            kind=r["kind"],
            name=r["name"],
            storage=r["storage"],
            linkage=r["linkage"],
            sig_id=r["sig_id"],
            decl_sig=r["decl_sig"],
            eff_sig=r["eff_sig"],
            file_path=r["file_path"],
            start=int(r["start"]),
            end=int(r["end"]),
        )

    # ----- maintenance -----

    def analyze(self) -> None:
        with self.tx():
            self.conn.execute("ANALYZE")

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")



