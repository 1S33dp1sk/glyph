# src/glyph/db.py
from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence, Tuple
import re
from .rewriter import Entity

# --- bump schema ---
_SCHEMA_VERSION = 5
_ROW_CHUNK = 1000

# ---------- connection & schema ----------

def _connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-80000")  # ~80MB
    return conn



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

_SCHEMA_SQL = f"""
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
  file_id   INTEGER NOT NULL,
  start     INTEGER NOT NULL,
  "end"     INTEGER NOT NULL,
  FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
  DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS idx_entities_file ON entities(file_id, start);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);

CREATE TABLE IF NOT EXISTS calls (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  src_gid   TEXT NOT NULL,
  dst_gid   TEXT,
  dst_name  TEXT,
  FOREIGN KEY(src_gid) REFERENCES entities(gid) ON DELETE CASCADE
  DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY(dst_gid) REFERENCES entities(gid) ON DELETE SET NULL
  DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS idx_calls_src ON calls(src_gid);
CREATE INDEX IF NOT EXISTS idx_calls_dst ON calls(dst_gid);
CREATE UNIQUE INDEX IF NOT EXISTS uq_calls_norm
  ON calls(src_gid, IFNULL(dst_gid,''), IFNULL(dst_name,''));

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

def _exec_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_SQL)

# ---------- small utils ----------

def _fts_expr_from_text(q: str, max_terms: int = 6) -> str:
    """
    Convert natural language to a safe, high-recall FTS5 query.
    - keep identifier-ish tokens (prefer ones with '_' or length>=4)
    - drop obvious operators
    - join with OR (not AND) and use prefix matches
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
        # prefer identifier-y tokens
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
        h = hashlib.sha256(); h.update(data); sha = h.hexdigest()
    return mtime, size, sha

def _chunked(seq: Iterable[tuple], n: int = _ROW_CHUNK) -> Iterator[list[tuple]]:
    buf: list[tuple] = []
    for it in seq:
        buf.append(it)
        if len(buf) >= n:
            yield buf; buf = []
    if buf:
        yield buf

# ---------- types ----------

@dataclass(frozen=True)
class DbEntity:
    gid: str
    kind: str
    name: str
    storage: str
    decl_sig: str
    eff_sig: str
    file_path: str
    start: int
    end: int

CallEdge = Tuple[str, Optional[str], Optional[str]]  # (src_gid, dst_gid|None, dst_name|None)

# ---------- main API ----------

class GlyphDB:
    """
    SQLite-backed index:
      - files: path metadata (mtime/size/sha256)
      - entities: GLYPH IDs and metadata
      - calls: src_gid → dst_gid | dst_name
      - entities_fts: FTS5 search over names/signatures
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.path = str(db_path)
        self.conn = _connect(self.path)
        self._ensure_schema()

    def close(self) -> None:
        try: self.conn.close()
        except Exception: pass

    def __enter__(self) -> "GlyphDB":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
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

    # ----- entities -----

    def upsert_entities(self, file_id: int, entities: Sequence[Entity]) -> None:
        rows = [
            (e.gid, e.kind, e.name, e.storage, e.decl_sig, e.eff_sig, file_id, int(e.start), int(e.end))
            for e in entities
        ]
        for chunk in _chunked(rows):
            self.conn.executemany(
                """
                INSERT INTO entities(gid, kind, name, storage, decl_sig, eff_sig, file_id, start, "end")
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(gid) DO UPDATE SET
                  kind=excluded.kind,
                  name=excluded.name,
                  storage=excluded.storage,
                  decl_sig=excluded.decl_sig,
                  eff_sig=excluded.eff_sig,
                  file_id=excluded.file_id,
                  start=excluded.start,
                  "end"=excluded."end"
                """,
                chunk,
            )

    def remove_entities_for_file(self, file_id: int) -> None:
        self.conn.execute("DELETE FROM entities WHERE file_id=?", (file_id,))

    # ----- calls -----

    def insert_calls(self, edges: Iterable[CallEdge]) -> None:
        for chunk in _chunked(list(edges)):
            self.conn.executemany(
                "INSERT OR IGNORE INTO calls(src_gid, dst_gid, dst_name) VALUES(?, ?, ?)",
                chunk,
            )

    def clear_calls_from(self, src_gids: Iterable[str]) -> None:
        self.conn.executemany("DELETE FROM calls WHERE src_gid=?", [(g,) for g in src_gids])

    def resolve_unlinked_calls(self) -> int:
        """
        Link calls where (dst_gid IS NULL) and a UNIQUE function definition by name exists.
        Prototypes are ignored; ambiguity keeps call unresolved.
        """
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
        cur = self.conn.execute(sql)
        self.conn.commit()
        return cur.rowcount or 0

    # ----- fetch / lookup -----

    def get_entity(self, gid: str) -> Optional[DbEntity]:
        row = self.conn.execute(
            """
            SELECT e.gid,e.kind,e.name,e.storage,e.decl_sig,e.eff_sig,f.path AS file_path,e.start,e."end"
            FROM entities e JOIN files f ON e.file_id=f.id
            WHERE e.gid=?""",
            (gid,),
        ).fetchone()
        if not row:
            return None
        return DbEntity(
            gid=row["gid"], kind=row["kind"], name=row["name"], storage=row["storage"],
            decl_sig=row["decl_sig"], eff_sig=row["eff_sig"], file_path=row["file_path"],
            start=int(row["start"]), end=int(row["end"]),
        )

    def entities_in_file(self, file_path: str | os.PathLike[str]) -> list[DbEntity]:
        p = _canon_path(file_path)
        row = self.conn.execute("SELECT id FROM files WHERE path=?", (p,)).fetchone()
        if not row:
            return []
        fid = int(row["id"])
        cur = self.conn.execute(
            """
            SELECT e.gid,e.kind,e.name,e.storage,e.decl_sig,e.eff_sig,f.path AS file_path,e.start,e."end"
            FROM entities e JOIN files f ON e.file_id=f.id
            WHERE e.file_id=?
            ORDER BY e.start
            """,
            (fid,),
        )
        return [
            DbEntity(
                gid=r["gid"], kind=r["kind"], name=r["name"], storage=r["storage"],
                decl_sig=r["decl_sig"], eff_sig=r["eff_sig"], file_path=r["file_path"],
                start=int(r["start"]), end=int(r["end"]),
            )
            for r in cur
        ]

    def callers(self, gid: str) -> list[str]:
        return [r["src_gid"] for r in self.conn.execute("SELECT src_gid FROM calls WHERE dst_gid=?", (gid,))]

    def callees(self, gid: str) -> list[tuple[Optional[str], Optional[str]]]:
        return [(r["dst_gid"], r["dst_name"])
                for r in self.conn.execute("SELECT dst_gid, dst_name FROM calls WHERE src_gid=?", (gid,))]

    def lookup_by_name(self, name: str) -> list[DbEntity]:
        cur = self.conn.execute(
            """
            SELECT e.gid,e.kind,e.name,e.storage,e.decl_sig,e.eff_sig,f.path,e.start,e."end"
            FROM entities e JOIN files f ON e.file_id=f.id
            WHERE e.name=?
            ORDER BY f.path, e.start
            """,
            (name,),
        )
        return [
            DbEntity(
                gid=r["gid"], kind=r["kind"], name=r["name"], storage=r["storage"],
                decl_sig=r["decl_sig"], eff_sig=r["eff_sig"], file_path=r["path"],
                start=int(r["start"]), end=int(r["end"]),
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
                "SELECT gid, name, decl_sig FROM entities_fts "
                "WHERE entities_fts MATCH ? LIMIT ?",
                (expr, int(limit)),
            )
            return [(r["gid"], r["name"], r["decl_sig"]) for r in cur]
        except sqlite3.OperationalError:
            # very conservative fallback
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
            SELECT e.gid,e.kind,e.name,e.storage,e.decl_sig,e.eff_sig,f.path AS file_path,e.start,e."end"
            FROM entities e JOIN files f ON e.file_id=f.id
            WHERE e.file_id=? AND e.start<=? AND e."end">=?
            ORDER BY (e."end"-e.start) ASC LIMIT 1
            """,
            (fid, int(offset), int(offset)),
        ).fetchone()
        if not r:
            return None
        return DbEntity(
            gid=r["gid"], kind=r["kind"], name=r["name"], storage=r["storage"],
            decl_sig=r["decl_sig"], eff_sig=r["eff_sig"], file_path=r["file_path"],
            start=int(r["start"]), end=int(r["end"]),
        )

    # ----- convenience ingest -----

    def ingest_file(
        self,
        file_path: str | os.PathLike[str],
        entities: Sequence[Entity],
        calls: Iterable[CallEdge] = (),
        file_bytes: Optional[bytes] = None,
        replace_file_entities: bool = True,
    ) -> None:
        p = _canon_path(file_path)
        with self.tx():
            fid = self.upsert_file(p, data=file_bytes)
            if replace_file_entities:
                rows = self.conn.execute("SELECT gid FROM entities WHERE file_id=?", (fid,)).fetchall()
                if rows:
                    self.clear_calls_from([r["gid"] for r in rows])
                self.remove_entities_for_file(fid)
            self.upsert_entities(fid, entities)
            if calls:
                self.insert_calls(calls)

    def bulk_ingest(self, items: Iterable[tuple[str, Sequence[Entity], Iterable[CallEdge], Optional[bytes]]]) -> None:
        """
        items: iterable of (file_path, entities, calls, file_bytes)
        """
        with self.tx():
            for file_path, entities, calls, data in items:
                self.ingest_file(file_path, entities, calls, data)

    # ----- maintenance -----

    def analyze(self) -> None:
        with self.tx():
            self.conn.execute("ANALYZE")

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")
