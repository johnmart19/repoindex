"""SQLite schema, read-only connections and shared formatting."""
from pathlib import Path
import os
import re
import sqlite3
import sys
from datetime import date


SCHEMA_VERSION = "2"


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS dirs (
  id   INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
  dir_id INTEGER NOT NULL REFERENCES dirs(id),
  name   TEXT NOT NULL,
  ext    TEXT NOT NULL,
  size   INTEGER NOT NULL,
  mtime  INTEGER NOT NULL,
  PRIMARY KEY (dir_id, name)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);
CREATE TABLE IF NOT EXISTS symbols (
  name   TEXT NOT NULL,
  kind   TEXT NOT NULL,
  decl   TEXT NOT NULL,
  dir_id INTEGER NOT NULL REFERENCES dirs(id),
  file   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE TABLE IF NOT EXISTS inc_targets (
  id     INTEGER PRIMARY KEY,
  target TEXT NOT NULL,
  system INTEGER NOT NULL,
  UNIQUE (target, system)
);
CREATE TABLE IF NOT EXISTS includes (
  dir_id    INTEGER NOT NULL REFERENCES dirs(id),
  file      TEXT NOT NULL,
  target_id INTEGER NOT NULL REFERENCES inc_targets(id)
);
CREATE INDEX IF NOT EXISTS idx_includes_target ON includes(target_id);

CREATE VIEW IF NOT EXISTS vfiles AS
  SELECT CASE WHEN d.path = '' THEN f.name
              ELSE d.path || '/' || f.name END AS path,
         f.name AS name, f.ext AS ext, f.size AS size, f.mtime AS mtime
  FROM files f JOIN dirs d ON d.id = f.dir_id;
CREATE VIEW IF NOT EXISTS vsymbols AS
  SELECT s.name AS name, s.kind AS kind, s.decl AS decl,
         CASE WHEN d.path = '' THEN s.file
              ELSE d.path || '/' || s.file END AS path
  FROM symbols s JOIN dirs d ON d.id = s.dir_id;
CREATE VIEW IF NOT EXISTS vincludes AS
  SELECT CASE WHEN d.path = '' THEN i.file
              ELSE d.path || '/' || i.file END AS path,
         t.target AS target, t.system AS system
  FROM includes i
  JOIN dirs d ON d.id = i.dir_id
  JOIN inc_targets t ON t.id = i.target_id;
"""


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return "%d B" % n
            return "%.1f %s" % (n, unit)
        n /= 1024


def posix_rel(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def open_db(db_path):
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        return db
    except BaseException:
        db.close()
        raise


def schema_is_current(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(files)")]
    return (not cols) or ("dir_id" in cols)


class IndexReadError(ValueError):
    """A completed, compatible snapshot could not be opened read-only."""


def open_db_required(db_path):
    """Open a completed snapshot read-only; callers own and must close the connection."""
    db = None
    try:
        uri = Path(db_path).absolute().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True)
        version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if version != (SCHEMA_VERSION,):
            raise ValueError("missing or unsupported schema version")
        for view in ("vfiles", "vsymbols", "vincludes"):
            db.execute("SELECT * FROM %s LIMIT 0" % view)
        if not db.execute("SELECT value FROM meta WHERE key='updated'").fetchone():
            raise ValueError("no completed scan metadata")
        errors = db.execute("SELECT value FROM meta WHERE key='error_count'").fetchone()
        if errors and int(errors[0]):
            print("Warning: snapshot has %s scan errors; results are incomplete."
                  % errors[0], file=sys.stderr)
        return db
    except (sqlite3.Error, ValueError) as error:
        if db is not None:
            db.close()
        raise IndexReadError("Cannot read index %s: %s. Run 'repoindex index ROOT' "
                             "explicitly to create/rebuild it." % (db_path, error)) from error


def resolve_db(args, root=None):
    if getattr(args, "db", None):
        return args.db
    root = root or getattr(args, "root", None) or "."
    return os.path.join(os.path.abspath(root), ".repoindex.db")


def datetime_iso():
    return date.today().isoformat()

def prepare_search(con):
    """Materialize paths once; optionally accelerate substring/glob candidates."""
    version = con.execute("SELECT value FROM meta WHERE key='search_schema'").fetchone()
    if version and version[0] == "1":
        return
    con.execute("DROP TABLE IF EXISTS search_paths_fts")
    con.execute("DROP TABLE IF EXISTS search_files")
    con.execute("CREATE TABLE search_files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INTEGER)")
    con.execute("INSERT INTO search_files(path,name,ext,size) SELECT path,name,ext,size FROM vfiles ORDER BY path")
    backend = "paths"
    try:
        con.execute("CREATE VIRTUAL TABLE search_paths_fts USING fts5(path, content='search_files', content_rowid='id', tokenize='trigram', detail='none')")
    except sqlite3.OperationalError as error:
        if not any(term in str(error).lower() for term in ("no such module", "no such tokenizer", "error in tokenizer")):
            raise
    else:
        con.execute("INSERT INTO search_paths_fts(search_paths_fts) VALUES('rebuild')")
        con.execute("INSERT INTO search_paths_fts(search_paths_fts) VALUES('optimize')")
        backend = "trigram"
    con.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                    [("search_schema", "1"), ("search_backend", backend)])


def files_sql(db, pattern, prefixes, limit):
    """Use optional acceleration only for candidates; keep exact original filters."""
    backend = db.execute("SELECT value FROM meta WHERE key='search_backend'").fetchone()
    table = "search_files" if backend else "vfiles"
    conds, params = [], []
    for prefix in prefixes:
        if not prefix:
            conds.append("1")
        else:
            conds.append("(path=? OR (path>=? AND path<?))")
            params.extend([prefix, prefix + "/", prefix + "0"])
    where = ["(" + " OR ".join(conds) + ")"]
    glob = any(char in pattern for char in "*?")
    # Short/common patterns use ordered paths and can stop at LIMIT immediately.
    literal = max(re.split(r"[*?%_\[\]]", pattern), key=len)
    if backend and backend[0] == "trigram" and len(literal) >= 3:
        if glob:
            patterns = [pattern] if "/" in pattern or pattern.startswith("*") else [pattern, "*/" + pattern]
            where.append("id IN (" + " UNION ".join("SELECT rowid FROM search_paths_fts WHERE path GLOB ?" for _ in patterns) + ")")
            params.extend(patterns)
        else:
            # LIKE's %/_ may widen candidates; instr below enforces literal semantics.
            where.append("id IN (SELECT rowid FROM search_paths_fts WHERE path LIKE ?)")
            params.append("%" + pattern + "%")
    if glob:
        where.append("(path GLOB ? OR name GLOB ?)")
        params.extend([pattern, pattern])
    else:
        where.append("instr(lower(path),lower(?))>0")
        params.append(pattern)
    params.append(limit)
    return "SELECT path FROM " + table + " WHERE " + " AND ".join(where) + " ORDER BY path LIMIT ?", params
