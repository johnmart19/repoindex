#!/usr/bin/env python3
"""
repoindex.py - full-coverage file + symbol indexing for huge source trees
(AOSP-scale Android and vendor source trees, WSL-friendly).

Zero third-party dependencies (Python 3.8+, stdlib only). Nothing is sampled
or truncated: every command emits complete results unless you explicitly pass
a limit option.

One multithreaded index pass records, for the ENTIRE tree:
  - every file (path, size, mtime)                         -> files table
  - heuristic class/struct/enum/union/macro/global matches -> symbols table
    exposes (comment-aware parsing, forward decls included)
  - every #include line of every header                    -> includes table

Storage is normalized (a shared dirs table + deduplicated include targets)
and VACUUMed after each build, so multi-million-file trees stay compact.
Query through the compatibility views: vfiles / vsymbols / vincludes.

Commands
--------
  index      Multithreaded tree scan into SQLite (--jobs N,
             default: min(32, 4 x cores)); rebuilds + compacts the index
  lookup     Log-digging helper: symbol -> definitions, implementation
             candidates, includers. Grouped per symbol, duplicates merged,
             forward declarations collapsed. `--from-log file` auto-extracts
             CamelCase/snake_case tokens (noise stoplist; --no-stoplist
             disables) and resolves all of them.
  symbols    Dump the symbol inventory: --out FILE (streamed, any size),
             --out-dir DIR (one markdown per top-level component),
             --kind / --pattern / --path-prefix filters
  headers    Generate a complete header inventory (CAMERA_HEADERS.md style)
  agentsmd   Generate an AGENTS.md guide for future AI agents
             (--compact variant for local models like LM Studio;
             --also CLAUDE.md,... writes platform-specific copies)
  stats      Index statistics (totals, per-extension table, largest files)
  tree       Directory map with recursive file counts and sizes
  search     Find files by glob/substring, optionally by extension
  dupes      Duplicate finder (size groups, then SHA-1 verification)

Typical WSL usage (both trees at once - index their common parent):
  python3 repoindex.py index ~/references_code --ignore out --ignore prebuilts
  python3 repoindex.py symbols --root ~/references_code --out-dir ~/references_code/SYMBOLS
  python3 repoindex.py lookup CameraMode --root ~/references_code
  python3 repoindex.py agentsmd ~/references_code --out ~/references_code/AGENTS.md

Tip: keep the tree inside the WSL filesystem (~/...) rather than /mnt/c/... -
indexing over the 9P mount is an order of magnitude slower.
"""

import argparse
import concurrent.futures
import fnmatch
import hashlib
import json
import stat
import tempfile
from pathlib import Path
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date

VERSION = "1.1.0"
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

DEFAULT_IGNORES = {".git", ".repo", ".svn", ".hg"}
HEADER_EXTS = {".h", ".hpp", ".hh", ".hxx"}
IMPL_EXTS = (".cpp", ".cc", ".cxx", ".c")

KEY_FILE_PATTERNS = [
    "README*", "LICENSE*", "COPYING*", "NOTICE*", "CHANGELOG*", "CONTRIBUTING*",
    "AGENTS.md", "CLAUDE.md", "Makefile", "*.mk", "Android.bp", "Android.mk",
    "CMakeLists.txt", "*.cmake", "Kconfig", "*.kconfig", "package.json",
    "tsconfig.json", "pyproject.toml", "setup.py", "setup.cfg",
    "requirements*.txt", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "settings.gradle", "*.sln", "*.csproj", "Dockerfile", "docker-compose*.yml",
    ".gitignore", ".clang-format", ".editorconfig", "*.settings.xml",
]
# One compiled regex instead of 40 fnmatch calls per file (matters at 3M+).
KEY_FILE_RE = re.compile("^(?:" + "|".join(
    fnmatch.translate(p).replace("\\Z", "")
    for p in KEY_FILE_PATTERNS) + ")$", re.IGNORECASE)

SKIP_NAME_TOKENS = {"class", "struct", "union", "enum", "final"}


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
    db.executescript(SCHEMA)
    return db


def schema_is_current(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(files)")]
    return (not cols) or ("dir_id" in cols)


def open_db_required(db_path):
    """Open a completed compatible snapshot without creating or mutating it."""
    try:
        uri = Path(db_path).absolute().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True)
        version = db.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if version != (SCHEMA_VERSION,):
            raise ValueError("missing or unsupported schema version")
        for view in ("vfiles", "vsymbols", "vincludes"):
            db.execute("SELECT * FROM %s LIMIT 0" % view)
        if not db.execute("SELECT value FROM meta WHERE key='updated'").fetchone():
            raise ValueError("no completed scan metadata")
    except (sqlite3.Error, ValueError) as exc:
        if "db" in locals():
            db.close()
        sys.exit("Cannot read index %s: %s. Run 'repoindex.py index ROOT' "
                 "explicitly to create/rebuild it." % (db_path, exc))
    errors = db.execute("SELECT value FROM meta WHERE key='error_count'").fetchone()
    if errors and int(errors[0]):
        print("Warning: snapshot has %s scan errors; results are incomplete."
              % errors[0], file=sys.stderr)
    return db


def resolve_db(args, root=None):
    if getattr(args, "db", None):
        return args.db
    root = root or getattr(args, "root", None) or "."
    return os.path.join(os.path.abspath(root), ".repoindex.db")


def datetime_iso():
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# C/C++ header parsing (comment aware)
# ---------------------------------------------------------------------------

BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"//[^\n]*")
DECL_RE = re.compile(r"^\s*(class|struct|enum|union)\b(.*)$")
INCLUDE_RE = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]')
DEFINE_RE = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)")
# Exposed globals: extern/static/const/constexpr/volatile/inline variables.
# Lines containing parentheses are functions or function pointers - skipped.
VAR_RE = re.compile(
    r"^\s*((?:extern|static|const|constexpr|volatile|inline)\b[^;(){}]*?)"
    r"\b([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*(=[^;]*)?;\s*$")
NAME_RE = re.compile(r"[A-Za-z_]\w*")


def strip_comments(src):
    def repl(m):
        return "\n" * m.group(0).count("\n")
    src = BLOCK_COMMENT.sub(repl, src)
    src = LINE_COMMENT.sub("", src)
    return src


def looks_like_macro(token):
    return (token.isupper() or token.endswith("_API") or
            token.startswith("__"))


def symbol_name(rest):
    """Declared identifier from the text after class/struct/enum/union.
    Skips 'enum class', macro decorations (CAMERA_API) and 'final'."""
    head = rest.split("{", 1)[0].split(";", 1)[0].split(":", 1)[0]
    for tok in NAME_RE.findall(head):
        if tok in SKIP_NAME_TOKENS or looks_like_macro(tok):
            continue
        return tok
    return ""


def parse_header(src):
    """-> (symbols, includes)

    symbols:  (name, kind, decl_line) - class/struct/enum/union (incl. forward
              declarations; lines cut at the opening brace, enums kept
              whole), plus #define macros and exposed global variables.
    includes: (target, system_flag) pairs, in source order.
    """
    symbols = []
    includes = []
    for line in strip_comments(src).splitlines():
        mi = INCLUDE_RE.match(line)
        if mi:
            includes.append((mi.group(2).strip(), 1 if mi.group(1) == "<" else 0))
            continue
        md = DEFINE_RE.match(line)
        if md:
            symbols.append((md.group(1), "define", line.strip()))
            continue
        m = DECL_RE.match(line)
        if m:
            kind, rest = m.group(1), m.group(2)
            text = (kind + rest).rstrip()
            if kind in ("class", "struct", "union"):
                text = text.split("{", 1)[0].rstrip()
            if not text or text == kind:
                continue
            name = symbol_name(rest)
            if name:
                symbols.append((name, kind, text))
            continue
        mv = VAR_RE.match(line)
        if mv:
            symbols.append((mv.group(2), "var", mv.group(0).strip()))
    return symbols, includes


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

def default_jobs():
    try:
        cpus = os.cpu_count() or 4
    except NotImplementedError:
        cpus = 4
    return max(2, min(32, cpus * 4))


def scan_directory(dirpath, filenames, root, db_abs, with_symbols):
    """Worker for one directory: stat every file, parse every header.
    Returns (file_rows, symbol_rows, include_rows, error_count).
    Rows carry file NAMES only; the main thread attaches the directory id.
    Runs inside worker threads; touches no shared state."""
    file_rows, sym_rows, inc_rows = [], [], []
    errors = 0
    check_db = os.path.dirname(db_abs) == dirpath
    for fn in filenames:
        full = os.path.join(dirpath, fn)
        # Skip the index itself and its SQLite journals (-journal/-wal/-shm)
        if check_db and full in {db_abs, db_abs + "-journal",
                                 db_abs + "-wal", db_abs + "-shm"}:
            continue
        try:
            st = os.lstat(full)
        except OSError:
            errors += 1
            continue
        ext = os.path.splitext(fn)[1].lower()
        file_rows.append((fn, ext, st.st_size, int(st.st_mtime)))
        if with_symbols and ext in HEADER_EXTS and stat.S_ISREG(st.st_mode):
            try:
                with open(full, "r", encoding="utf-8",
                          errors="replace") as f:
                    src = f.read()
            except OSError:
                errors += 1
                continue
            syms, incs = parse_header(src)
            for name, kind, decl in syms:
                sym_rows.append((name, kind, decl, fn))
            for target, system in incs:
                inc_rows.append((fn, target, system))
    return file_rows, sym_rows, inc_rows, errors


def cmd_index(args):
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.exit("Not a directory: %s" % root)
    ignores = set() if args.no_ignore else set(DEFAULT_IGNORES)
    ignores.update(args.ignore or [])
    with_symbols = not getattr(args, "no_symbols", False)
    jobs = max(1, getattr(args, "jobs", 0) or default_jobs())
    db_path = resolve_db(args, root)
    db = open_db(db_path)
    if not schema_is_current(db):
        # Old (fatter) layout: drop it entirely and rebuild compact.
        db.executescript("""
            DROP VIEW IF EXISTS vfiles;
            DROP VIEW IF EXISTS vsymbols;
            DROP VIEW IF EXISTS vincludes;
            DROP TABLE IF EXISTS files;
            DROP TABLE IF EXISTS symbols;
            DROP TABLE IF EXISTS includes;
            DROP TABLE IF EXISTS dirs;
            DROP TABLE IF EXISTS inc_targets;
            DROP TABLE IF EXISTS meta;
        """)
        db.executescript(SCHEMA)
        print("Old index layout dropped; rebuilding compact.")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=OFF")
    db_abs = os.path.abspath(db_path)

    t0 = time.time()
    count = 0
    total_bytes = 0
    n_symbols = 0
    n_includes = 0
    errors = 0

    cur = db.cursor()
    cur.execute("BEGIN")
    for t in ("files", "symbols", "includes", "dirs", "inc_targets"):
        cur.execute("DELETE FROM %s" % t)
    file_batch, sym_batch, inc_batch = [], [], []

    dir_ids = {}       # rel dir path ('' = root) -> id
    dir_new = []       # pending (id, path) rows
    target_ids = {}    # (target, system) -> id
    target_new = []    # pending (id, target, system) rows
    seq = [0, 0]       # [dir_seq, target_seq]

    def dir_id_for(rel):
        did = dir_ids.get(rel)
        if did is not None:
            return did
        seq[0] += 1
        did = seq[0]
        dir_ids[rel] = did
        dir_new.append((did, rel))
        return did

    def target_id_for(target, system):
        key = (target, system)
        tid = target_ids.get(key)
        if tid is not None:
            return tid
        seq[1] += 1
        tid = seq[1]
        target_ids[key] = tid
        target_new.append((tid, target, system))
        return tid

    def flush():
        if dir_new:
            cur.executemany("INSERT INTO dirs VALUES (?,?)", dir_new)
            dir_new.clear()
        if target_new:
            cur.executemany("INSERT INTO inc_targets VALUES (?,?,?)",
                            target_new)
            target_new.clear()
        if file_batch:
            cur.executemany(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", file_batch)
            file_batch.clear()
        if sym_batch:
            cur.executemany("INSERT INTO symbols VALUES (?,?,?,?,?)",
                            sym_batch)
            sym_batch.clear()
        if inc_batch:
            cur.executemany("INSERT INTO includes VALUES (?,?,?)", inc_batch)
            inc_batch.clear()

    def onerror(err):
        nonlocal errors
        errors += 1

    last_report = [0]

    def consume(dir_rel, result):
        nonlocal count, total_bytes, n_symbols, n_includes, errors
        file_rows, sym_rows, inc_rows, errs = result
        errors += errs
        did = dir_id_for(dir_rel)
        for fn, ext, size, mt in file_rows:
            file_batch.append((did, fn, ext, size, mt))
        count += len(file_rows)
        total_bytes += sum(r[2] for r in file_rows)
        for name, kind, decl, fn in sym_rows:
            sym_batch.append((name, kind, decl, did, fn))
        for fn, target, system in inc_rows:
            inc_batch.append((did, fn, target_id_for(target, system)))
        n_symbols += len(sym_rows)
        n_includes += len(inc_rows)
        if len(file_batch) >= 10000:
            flush()
        if count - last_report[0] >= 100000:
            last_report[0] = count
            print("  ... %d files" % count, flush=True)

    def prune(dirpath, dirnames):
        dirnames[:] = [
            d for d in dirnames
            if d not in ignores
            and not os.path.islink(os.path.join(dirpath, d))
        ]

    print("Scanning with %d thread(s) ..." % jobs)
    if jobs == 1:
        for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
            prune(dirpath, dirnames)
            rel = "" if dirpath == root else posix_rel(dirpath, root)
            consume(rel, scan_directory(dirpath, filenames, root, db_abs,
                                        with_symbols))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
            pending = {}   # future -> rel dir
            for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
                prune(dirpath, dirnames)
                rel = "" if dirpath == root else posix_rel(dirpath, root)
                fut = ex.submit(scan_directory, dirpath, filenames, root,
                                db_abs, with_symbols)
                pending[fut] = rel
                # Bounded pending queue keeps memory flat on huge trees.
                if len(pending) >= jobs * 8:
                    done, _pending = concurrent.futures.wait(
                        pending,
                        return_when=concurrent.futures.FIRST_COMPLETED)
                    for f in done:
                        rel = pending.pop(f)
                        try:
                            result = f.result()
                        except Exception as e:
                            errors += 1
                            print("  warning: worker failed: %s" % e, file=sys.stderr)
                        else:
                            consume(rel, result)
            for f in concurrent.futures.as_completed(pending):
                try:
                    result = f.result()
                except Exception as e:
                    errors += 1
                    print("  warning: worker failed: %s" % e, file=sys.stderr)
                else:
                    consume(pending[f], result)
    flush()

    cur.execute("INSERT OR REPLACE INTO meta VALUES ('root', ?)", (root,))
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('updated', ?)",
                (datetime_iso(),))
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('file_count', ?)",
                (str(count),))
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('total_bytes', ?)",
                (str(total_bytes),))
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('symbol_count', ?)",
                (str(n_symbols),))
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,))
    cur.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
        ("error_count", str(errors)),
        ("ignore_directories", json.dumps(sorted(ignores))),
        ("symbols_enabled", str(with_symbols).lower()),
        ("symlink_policy", "record file links; do not parse or traverse links"),
    ])
    db.commit()

    t1 = time.time()
    print("Compacting (VACUUM) ...", flush=True)
    db.execute("VACUUM")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()

    dt = time.time() - t0
    rate = int(count / dt) if dt > 0 else count
    print("Indexed %d files (%s) in %.1fs (%d files/s, incl. %.0fs compact)"
          % (count, human_size(total_bytes), dt, rate, time.time() - t1))
    if with_symbols:
        print("Recorded %d exposed symbols and %d include edges from headers"
              % (n_symbols, n_includes))
    print("Index size: %s -> %s" %
          (human_size(os.path.getsize(db_path)), db_path))
    if errors:
        print("Warning: %d scan errors; snapshot is incomplete" % errors)


# ---------------------------------------------------------------------------
# lookup  (log digging: symbol -> header / declaration / includers)
# ---------------------------------------------------------------------------

LOG_TOKEN_RE = re.compile(r"[A-Za-z_]\w{2,}")


def plausible_symbol_token(tok):
    """CamelCase (CameraMode), Capitalized class-style words of
    length >= 5 (Timer, Session), or lower_snake_case (capture_hint).
    Plain lowercase words and short Capitalized words are skipped."""
    if any(c.isupper() for c in tok[1:]):
        return True
    if "_" in tok and not tok.isupper():
        return True
    if len(tok) >= 5 and tok[0].isupper() and tok[1:].islower():
        return True
    return False


JUNK_TOKEN_LEN = 40
HEXISH_RE = re.compile(r"^[xX][0-9A-Fa-f_]+$")
HEXSTR_RE = re.compile(r"(?:[0-9a-f]{2}){6,}")


def is_junk_token(tok):
    """Structural log junk: hex blobs, digit-heavy ids,超长 tracepoint
    names. Objective shapes, always filtered in --from-log mode."""
    if len(tok) > JUNK_TOKEN_LEN:
        return True
    if sum(ch.isdigit() for ch in tok) > len(tok) * 0.5:
        return True
    if HEXISH_RE.match(tok) or HEXSTR_RE.search(tok):
        return True
    return False


def is_forward_decl(kind, decl):
    return kind in ("class", "struct", "union") and decl.rstrip().endswith(";")


def impl_candidates(db, header_path):
    """Same-directory, same-basename implementation files (.cpp/.cc/...)."""
    dir_rel = header_path.rsplit("/", 1)[0] if "/" in header_path else ""
    base = os.path.splitext(header_path.rsplit("/", 1)[-1])[0]
    drow = db.execute("SELECT id FROM dirs WHERE path = ?",
                      (dir_rel,)).fetchone()
    if not drow:
        return []
    found = []
    for ext in IMPL_EXTS:
        if db.execute("SELECT 1 FROM files WHERE dir_id = ? AND name = ?",
                      (drow[0], base + ext)).fetchone():
            found.append((dir_rel + "/" if dir_rel else "") + base + ext)
    return found


def wrap_paths(prefix, paths, indent="    "):
    """Inline, wrapped, complete path list - for long header lists."""
    line = indent + prefix
    for p in paths:
        if len(line) + len(p) + 2 > 160:
            print(line.rstrip().rstrip(","))
            line = indent + "  " + p + ", "
        else:
            line += p + ", "
    print(line.rstrip().rstrip(","))


def lookup_symbol(db, name, substr=False):
    """-> {symbol_name: [(kind, decl, path), ...]} (empty dict = no match)."""
    if substr:
        rows = db.execute(
            "SELECT name, kind, decl, path FROM vsymbols WHERE name LIKE ? "
            "ORDER BY path", ("%" + name + "%",)).fetchall()
    else:
        rows = db.execute(
            "SELECT name, kind, decl, path FROM vsymbols WHERE name = ? "
            "ORDER BY path", (name,)).fetchall()
    by_name = {}
    for nm, kind, decl, path in rows:
        by_name.setdefault(nm, []).append((kind, decl, path))
    return by_name


def print_symbol(db, name, rows, verbose, log_count=0, max_defs=0,
                 hint_root=""):
    """One deduplicated block per symbol: each unique definition once (with
    all its headers + impl candidates), forward declarations collapsed.
    In from-log digests, symbols with many definitions collapse further."""
    kinds = sorted({k for k, _d, _p in rows})
    defs = {}
    fwd_paths = []
    for kind, decl, path in rows:
        if is_forward_decl(kind, decl):
            if path not in fwd_paths:
                fwd_paths.append(path)
        else:
            bucket = defs.setdefault((kind, decl), [])
            if path not in bucket:
                bucket.append(path)
    n_defs = sum(len(v) for v in defs.values())
    parts = []
    if n_defs:
        parts.append("%d definition%s" % (n_defs, "" if n_defs == 1 else "s"))
    if fwd_paths:
        parts.append("forward-declared in %d header%s"
                     % (len(fwd_paths), "" if len(fwd_paths) == 1 else "s"))
    if log_count:
        parts.append("seen %dx in log" % log_count)
    print("## %s (%s) - %s" % (name, "/".join(kinds),
                               "; ".join(parts) or "no data"))
    if max_defs and not verbose and n_defs > max_defs:
        kind_counts = {}
        def_headers = set()
        for (k, _d), paths in defs.items():
            kind_counts[k] = kind_counts.get(k, 0) + len(paths)
            def_headers.update(paths)
        print("  digest: %s, across %d headers"
              % (", ".join("%d %s%s" % (c, k, "" if c == 1 else "s")
                           for k, c in sorted(kind_counts.items())),
                 len(def_headers)))
        first = sorted(def_headers)[:3]
        for p in first:
            print("    @ %s" % p)
            impls = impl_candidates(db, p)
            if impls:
                print("      impl: %s" % ", ".join(impls))
        if len(def_headers) > 3:
            print("    ... and %d more headers" % (len(def_headers) - 3))
        print("  drill down: python3 repoindex.py lookup %s --root %s"
              % (name, hint_root or "<tree>"))
        print()
        return
    for (kind, decl), paths in defs.items():
        print("  def: %s" % decl)
        if len(paths) <= 10:
            for p in paths:
                print("    @ %s" % p)
                impls = impl_candidates(db, p)
                if impls:
                    print("      impl: %s" % ", ".join(impls))
        else:
            wrap_paths("defined in %d headers: " % len(paths), paths)
        if verbose:
            seen_bn = set()
            for p in paths:
                bn = p.rsplit("/", 1)[-1]
                if bn in seen_bn:
                    continue
                seen_bn.add(bn)
                inc = db.execute(
                    "SELECT DISTINCT path FROM vincludes WHERE target = ? "
                    "ORDER BY path", (bn,)).fetchall()
                if inc:
                    print("    included by (%d):" % len(inc))
                    for (ip,) in inc:
                        print("      %s" % ip)
    if fwd_paths:
        if verbose:
            wrap_paths("fwd (%d): " % len(fwd_paths), fwd_paths, indent="  ")
        else:
            print("  fwd: forward-declared in %d headers "
                  "(use --verbose to list)" % len(fwd_paths))
    print()


# Ubiquitous tokens that flood log-derived lookups but answer nothing.
LOG_STOPLIST = {
    "NULL", "TRUE", "FALSE", "OK", "SUCCESS", "FAILED", "FAILURE",
    "ERROR", "WARNING", "WARN", "INFO", "DEBUG", "VERBOSE", "TRACE",
    "FATAL", "TODO", "FIXME", "NOTE", "XXX", "YES", "NO",
    "INTERFACE", "CALLBACK", "APIENTRY", "WINAPI", "STDCALL", "EXTERN_C",
    "UNUSED", "ASSERT", "STATIC_ASSERT", "ALIGN", "PACKED", "EXPORT",
}


def print_symbol_summary(matched):
    """Ranked one-line-per-symbol table: the agent's entry point to the
    report."""
    print("=== Matched symbols, most frequent in log first ===\n")
    print("%7s  %-36s %-26s %5s  %s"
          % ("seen", "symbol", "kinds", "defs", "first definition"))
    for name, rows, lc in matched:
        kinds = "/".join(sorted({k for k, _d, _p in rows}))
        def_hdrs = {p for k, d, p in rows if not is_forward_decl(k, d)}
        first = min(def_hdrs) if def_hdrs else "(forward decls only)"
        print("%7d  %-36s %-26s %5d  %s"
              % (lc, name, kinds, len(def_hdrs), first))
    print()


def cmd_lookup(args):
    db = open_db_required(resolve_db(args))
    if db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 0:
        print("(no symbols in index - re-run: repoindex.py index <root>)",
              file=sys.stderr)
    root_row = db.execute("SELECT value FROM meta WHERE key='root'").fetchone()
    hint_root = root_row[0] if root_row else ""

    names = list(args.names or [])
    log_counts = {}
    if args.from_log:
        try:
            with open(args.from_log, "r", encoding="utf-8",
                      errors="replace") as f:
                text = f.read()
        except OSError as e:
            sys.exit("Cannot read log: %s" % e)
        counts = Counter()
        stopped = junk = 0
        for tok in LOG_TOKEN_RE.findall(text):
            if not plausible_symbol_token(tok):
                continue
            if not args.no_stoplist and tok.upper() in LOG_STOPLIST:
                stopped += 1
                continue
            if is_junk_token(tok):
                junk += 1
                continue
            counts[tok] += 1
        rare = 0
        for tok, c in counts.items():
            if c < args.min_count:
                rare += 1
            else:
                log_counts[tok] = c
        print("Extracted %d candidate tokens from %s"
              % (len(log_counts), args.from_log), file=sys.stderr)
        notes = []
        if stopped:
            notes.append("%d noise" % stopped)
        if junk:
            notes.append("%d junk-shaped" % junk)
        if rare:
            notes.append("%d below --min-count %d" % (rare, args.min_count))
        if notes:
            print("Filtered out: " + ", ".join(notes) +
                  " (widen with --no-stoplist / --min-count 1)",
                  file=sys.stderr)
        # most frequent in the log first
        names.extend(sorted(log_counts, key=lambda t: (-log_counts[t], t)))

    if not names:
        sys.exit("Give at least one symbol name, or --from-log <file>")

    explicit = bool(args.names) and not args.from_log
    verbose = args.verbose or explicit
    hits = misses = raw = 0
    matched = []   # (symbol_name, rows, log_count)
    for name in names:
        groups = lookup_symbol(db, name, substr=args.substr)
        if not groups:
            misses += 1
            if not args.from_log:
                print("## %s - no match in index\n" % name)
            continue
        hits += 1
        for sym in sorted(groups):
            rows = groups[sym]
            raw += len(rows)
            matched.append((sym, rows, log_counts.get(name, 0)))

    if args.from_log:
        matched.sort(key=lambda t: (-t[2], t[0]))
        if not args.no_summary or args.summary_only:
            print_symbol_summary(matched)
        if not args.summary_only:
            for name, rows, lc in matched:
                print_symbol(db, name, rows, verbose, log_count=lc,
                             max_defs=args.max_defs, hint_root=hint_root)
    else:
        for name, rows, _lc in matched:
            print_symbol(db, name, rows, verbose)
    if args.from_log:
        print("%d/%d tokens matched a header symbol (%d unmatched; %d raw "
              "rows collapsed into the report above)"
              % (hits, hits + misses, misses, raw), file=sys.stderr)


# ---------------------------------------------------------------------------
# symbols  (full inventory dump - streamed, splittable per component)
# ---------------------------------------------------------------------------

def like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def symbol_query(args, extra_conds=None, extra_params=None):
    sql = "SELECT name, kind, decl, path FROM vsymbols"
    conds, params = [], []
    if args.kind:
        conds.append("kind = ?")
        params.append(args.kind)
    if args.pattern:
        conds.append("name GLOB ?")
        params.append(args.pattern)
    if getattr(args, "path_prefix", None):
        conds.append("path LIKE ? ESCAPE '\\'")
        params.append(like_escape(args.path_prefix) + "%")
    if extra_conds:
        conds.extend(extra_conds)
        params.extend(extra_params or [])
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY name COLLATE NOCASE, path"
    return sql, params


GUARD_DECL_RE = re.compile(r"^#define ([A-Za-z_]\w*)\s*$")
GUARD_NAME_RE = re.compile(r"_(H|HPP|HH|HXX)_?$")


def is_include_guard(name, decl):
    """Heuristic: ALL_CAPS macro named like its file (FOO_H), no value."""
    m = GUARD_DECL_RE.match(decl)
    return bool(m and m.group(1) == name and name.isupper()
                and GUARD_NAME_RE.search(name))


def keep_row(args, name, kind, decl):
    """Dump-time token savers. The index always keeps everything; these
    filters only affect dumps."""
    if getattr(args, "no_guards", False) and kind == "define" \
            and is_include_guard(name, decl):
        return False
    if getattr(args, "defs_only", False) \
            and kind in ("class", "struct", "union") \
            and decl.rstrip().endswith(";"):
        return False
    return True


def write_symbols_md(f, title, cursor, note, args=None):
    f.write("# %s\n\n%s\n\n" % (title, note))
    f.write("| Symbol | Kind | Declaration | Header |\n"
            "|--------|------|-------------|--------|\n")
    n = 0
    for name, kind, decl, path in cursor:
        if args is not None and not keep_row(args, name, kind, decl):
            continue
        f.write("| `%s` | %s | `%s` | `%s` |\n" %
                (name, kind, decl.replace("|", "\\|"), path))
        n += 1
    return n


def write_symbols_dedup(f, cursor, args):
    """Merge identical (name, kind, decl) rows; every exposing header is
    listed on the merged row."""
    f.write("# Symbol index (deduplicated)\n\nIdentical declarations across "
            "headers are merged; every exposing header is listed.\n\n"
            "| Symbol | Kind | Declaration | Headers |\n"
            "|--------|------|-------------|---------|\n")
    n = 0
    cur_key, cur_paths = None, []

    def flush_row():
        nonlocal n
        if cur_key is None:
            return
        name, kind, decl = cur_key
        if len(cur_paths) == 1:
            where = "`%s`" % cur_paths[0]
        else:
            where = "%d headers: %s" % (len(cur_paths), "; ".join(cur_paths))
        f.write("| `%s` | %s | `%s` | %s |\n" %
                (name, kind, decl.replace("|", "\\|"), where))
        n += 1

    for name, kind, decl, path in cursor:
        if not keep_row(args, name, kind, decl):
            continue
        key = (name, kind, decl)
        if key != cur_key:
            flush_row()
            cur_key, cur_paths = key, []
        cur_paths.append(path)
    flush_row()
    return n


def symbols_out_dir(db, args):
    """Adaptive per-component dump: split recursively until no file holds
    more than --max-per-file symbols. Single streamed pass over the symbol
    table; memory stays flat regardless of tree size."""
    if getattr(args, "path_prefix", None):
        sys.exit("--path-prefix does not combine with --out-dir; use "
                 "--out with --path-prefix instead.")
    if getattr(args, "dedup", False):
        sys.exit("--dedup does not combine with --out-dir (merging spans "
                 "components); use --out.")
    max_per = max(1, args.max_per_file)
    os.makedirs(args.out_dir, exist_ok=True)

    w, p = [], []
    if args.kind:
        w.append("kind = ?")
        p.append(args.kind)
    if args.pattern:
        w.append("name GLOB ?")
        p.append(args.pattern)
    where = (" WHERE " + " AND ".join(w)) if w else ""

    dirpaths = dict(db.execute("SELECT id, path FROM dirs"))
    direct = {}
    for did, c in db.execute("SELECT dir_id, COUNT(*) FROM symbols" + where +
                             " GROUP BY dir_id", p):
        direct[dirpaths.get(did, "")] = c
    subtree = defaultdict(int)
    children = defaultdict(list)
    for dpath in dirpaths.values():
        parent = dpath.rsplit("/", 1)[0] if "/" in dpath else ""
        children[parent].append(dpath)
    for dpath, c in direct.items():
        d = dpath
        while True:
            subtree[d] += c
            if d == "":
                break
            d = d.rsplit("/", 1)[0] if "/" in d else ""

    # Leaves get one file covering their whole subtree; split nodes with
    # headers of their own additionally get a '*__direct.md' file.
    leaf_set = set()
    direct_nodes = set()

    def decide(prefix, depth):
        total = subtree.get(prefix, 0)
        if total == 0:
            return
        kids = [k for k in children.get(prefix, []) if subtree.get(k, 0)]
        if total <= max_per or not kids or depth >= 5:
            leaf_set.add(prefix)
            return
        if direct.get(prefix, 0):
            direct_nodes.add(prefix)
        for k in kids:
            decide(k, depth + 1)

    decide("", 0)

    def fname(prefix, is_direct):
        if prefix == "":
            return "_root.md" if is_direct else "_all.md"
        base = prefix.replace("/", "__")
        return base + ("__direct.md" if is_direct else ".md")

    # Split each component file by kind group: types (small, LLM-readable),
    # defines and vars (usually huge; grep material, not context material).
    KIND_GROUP = {"class": "types", "struct": "types", "enum": "types",
                  "union": "types", "define": "defines", "var": "vars"}
    GROUP_TITLE = {
        "types": "type declarations (class/struct/enum/union)",
        "defines": "macros (#define)",
        "vars": "global variables",
    }
    handles, counts = {}, {}

    def handle_for(key):   # key = (prefix, is_direct, group); lazy open
        h = handles.get(key)
        if h is not None:
            return h
        prefix, is_direct, group = key
        path = os.path.join(
            args.out_dir,
            fname(prefix, is_direct).replace(".md", ".%s.md" % group))
        h = open(path, "w", encoding="utf-8")
        where = prefix or "entire tree"
        if is_direct:
            where += " (direct headers)"
        h.write("# Symbols: %s - %s\n\nComplete inventory, no cutouts. "
                "Grep this file; do not load it whole into an LLM context.\n\n"
                "| Symbol | Kind | Declaration | Header |\n"
                "|--------|------|-------------|--------|\n"
                % (where, GROUP_TITLE[group]))
        handles[key] = h
        counts[key] = 0
        return h

    route_cache = {}

    def route(d):
        t = route_cache.get(d)
        if t is not None:
            return t
        cur = d
        while True:
            if cur in leaf_set:
                t = (cur, False)
                break
            if cur == d and cur in direct_nodes:
                t = (cur, True)
                break
            if cur == "":
                t = ("", "" not in leaf_set)
                break
            cur = cur.rsplit("/", 1)[0] if "/" in cur else ""
        route_cache[d] = t
        return t

    total = 0
    sql = ("SELECT d.path, s.file, s.name, s.kind, s.decl "
           "FROM symbols s JOIN dirs d ON d.id = s.dir_id" + where +
           " ORDER BY d.path, s.file")
    for dpath, file, name, kind, decl in db.execute(sql, p):
        if not keep_row(args, name, kind, decl):
            continue
        prefix, is_direct = route(dpath)
        key = (prefix, is_direct, KIND_GROUP.get(kind, "types"))
        h = handle_for(key)
        full = (dpath + "/" if dpath else "") + file
        h.write("| `%s` | %s | `%s` | `%s` |\n" %
                (name, kind, decl.replace("|", "\\|"), full))
        counts[key] += 1
        total += 1
    for h in handles.values():
        h.close()

    index_lines = ["# Symbol index by component", "",
                   "Adaptive split: no subtree exceeds %d symbols "
                   "(--max-per-file). `.types.md` files hold only "
                   "class/struct/enum/union (small - the API surface agents "
                   "read); `.defines.md` / `.vars.md` hold macros / globals "
                   "(grep them; never load them whole)." % max_per, ""]
    for key in sorted(counts,
                      key=lambda k: (k[0].count("/"), k[0], k[1], k[2])):
        prefix, is_direct, group = key
        if counts[key] == 0:
            continue
        label = prefix or "(entire tree)"
        if is_direct:
            label += " (direct headers)"
        label += " - " + group
        index_lines.append("%s- [%s](%s) - %d symbols" % (
            "  " * prefix.count("/"), label,
            fname(prefix, is_direct).replace(".md", ".%s.md" % group),
            counts[key]))
    with open(os.path.join(args.out_dir, "00-INDEX.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(index_lines) + "\n")
    print("Wrote %d files -> %s (%d symbols total, max %d per file)"
          % (len(handles), args.out_dir, total, max_per))


def cmd_symbols(args):
    db = open_db_required(resolve_db(args))

    if args.out_dir:
        symbols_out_dir(db, args)
        return

    sql, params = symbol_query(args)
    if getattr(args, "dedup", False):
        sql = sql.rsplit("ORDER BY", 1)[0] + \
            "ORDER BY name COLLATE NOCASE, kind, decl, path"
    cur = db.execute(sql, params)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            if getattr(args, "dedup", False):
                n = write_symbols_dedup(f, cur, args)
            else:
                n = write_symbols_md(f, "Symbol index", cur,
                                     "Generated %s. Complete inventory of "
                                     "exposed classes, structs, enums, "
                                     "unions, macros and global variables - "
                                     "no cutouts." % date.today().isoformat(),
                                     args)
        print("Wrote %s (%d symbols)" % (args.out, n))
    elif getattr(args, "dedup", False):
        n = 0
        cur_key, cur_paths = None, []
        for name, kind, decl, path in cur:
            if not keep_row(args, name, kind, decl):
                continue
            key = (name, kind, decl)
            if key != cur_key:
                if cur_key is not None:
                    print("%-40s %-7s %d header(s): %s" % (
                        cur_key[0], cur_key[1], len(cur_paths),
                        "; ".join(cur_paths)))
                    n += 1
                cur_key, cur_paths = key, []
            cur_paths.append(path)
        if cur_key is not None:
            print("%-40s %-7s %d header(s): %s" % (
                cur_key[0], cur_key[1], len(cur_paths), "; ".join(cur_paths)))
            n += 1
        print("%d unique symbols" % n, file=sys.stderr)
    else:
        n = 0
        for name, kind, decl, path in cur:
            if not keep_row(args, name, kind, decl):
                continue
            print("%-40s %-7s %s" % (name, kind, path))
            n += 1
        print("%d symbols" % n, file=sys.stderr)
    if n == 0 and not (args.kind or args.pattern or
                       getattr(args, "path_prefix", None)):
        print("(0 symbols in the index - it was not built yet or predates\n"
              " symbol support; run: python3 repoindex.py index <root>)",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# headers  (CAMERA_HEADERS.md format, full coverage; reads files, not index)
# ---------------------------------------------------------------------------

def cmd_headers(args):
    root = os.path.abspath(args.root)
    patterns = args.pattern or ["*.h"]
    subdirs = args.subdir or ["."]

    matched = []
    for sub in subdirs:
        base = os.path.normpath(os.path.join(root, sub))
        if not os.path.isdir(base):
            sys.exit("Not a directory: %s" % base)
        if args.recursive:
            walker = os.walk(base)
        else:
            walker = [(base, [], os.listdir(base))]
        for dirpath, _dirnames, filenames in walker:
            for fn in filenames:
                if any(fnmatch.fnmatch(fn, p) for p in patterns):
                    full = os.path.join(dirpath, fn)
                    if not os.path.islink(full) and os.path.isfile(full):
                        matched.append((full, fn))
    matched.sort(key=lambda t: (t[1].lower(), t[0].lower()))

    out_path = os.path.abspath(args.out) if args.out else None
    link_base = os.path.dirname(out_path) if out_path else os.getcwd()

    title = args.title or "Header index: %s" % ", ".join(subdirs)
    if args.recursive:
        scope = "all %s files under %s (recursive)" % (
            "/".join(patterns), ", ".join(subdirs))
    else:
        scope = "all direct %s files in %s" % (
            "/".join(patterns), ", ".join(subdirs))

    lines = []
    lines.append("# %s" % title)
    lines.append("")
    lines.append("Generated from %s on %s. This is an interface/include "
                 "inventory, not an exhaustive correctness audit."
                 % (scope, date.today().isoformat()))
    lines.append("")
    lines.append("%d files." % len(matched))
    lines.append("")

    unreadable = 0
    for full, fn in matched:
        rel = posix_rel(full, link_base)
        lines.append("## [%s](%s)" % (fn, rel))
        lines.append("")
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            unreadable += 1
            continue
        syms, incs = parse_header(src)
        # CAMERA_HEADERS.md format lists type declarations only; macros and
        # variables live in the symbol index (symbols table / SYMBOLS dir).
        decls = [d for (_n, k, d) in syms
                 if k in ("class", "struct", "enum", "union")]
        if decls:
            lines.append("Declarations (including forward declarations):")
            lines.append("```cpp")
            lines.extend(decls)
            lines.append("```")
        if incs:
            lines.append("Includes:")
            lines.append("```cpp")
            lines.extend('#include %s%s%s' % ("<" if s else '"', t,
                                              ">" if s else '"')
                         for t, s in incs)
            lines.append("```")
        lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print("Wrote %s (%d files, %s)" %
              (out_path, len(matched), human_size(len(text))))
    else:
        sys.stdout.write(text)
    if unreadable:
        print("Warning: %d files unreadable" % unreadable, file=sys.stderr)


# ---------------------------------------------------------------------------
# agentsmd
# ---------------------------------------------------------------------------

def dir_aggregates(rows):
    counts = defaultdict(int)
    sizes = defaultdict(int)
    for path, size in rows:
        parts = path.split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            counts[d] += 1
            sizes[d] += size
    return counts, sizes


def case_table_lines(db_path):
    """The 'Which tool for which case' section, shared by the full and
    compact agent guides."""
    L = []
    L.append("## Which tool for which case")
    L.append("")
    L.append("Indexed queries read the snapshot (`%s`). `headers` scans source; `dupes` reads candidate files. `python3 repoindex.py <cmd> -h` lists every flag."
             % db_path)
    L.append("")
    L.append("| Situation | Command |")
    L.append("|---|---|")
    L.append("| Class/symbol seen in a log: definitions, locations, impl "
             "candidates, includers | `lookup NAME --root <tree>` (full "
             "detail by default) |")
    L.append("| Cross-check a whole logcat | `lookup --from-log FILE --root "
             "<tree>` - ranked summary table first, blocks sorted by log "
             "frequency; add `--summary-only` for just the table, "
             "`--min-count 3` to skip one-off tokens |")
    L.append("| Where is a macro / global defined | same `lookup` - "
             "`define` and `var` symbols are indexed too |")
    L.append("| Mega-symbols flood the report (`Status`, `Result`...) | "
             "they auto-collapse to digests over `--max-defs` (default 25); "
             "`--max-defs 0` disables |")
    L.append("| Noise words in from-log mode | stoplist + junk filters are "
             "automatic; `--no-stoplist` widens |")
    L.append("| Everything one header exposes | SQL: `SELECT name, kind, "
             "decl FROM vsymbols WHERE path = '<path>'` |")
    L.append("| All symbols of one component | `symbols --path-prefix <comp>"
             " --out <comp>.md` |")
    L.append("| Bulk symbol docs, agent-sized | `symbols --out-dir SYMBOLS/`"
             " - adaptive split, kind-grouped (types/defines/vars), see "
             "`SYMBOLS/00-INDEX.md` |")
    L.append("| Find files by name/glob | `search '*CameraMode*' --root "
             "<tree>` |")
    L.append("| Tree layout, sizes, type census | `tree --depth 3` / `stats`"
             " |")
    L.append("| Duplicate files | `dupes` (SHA-1 verified) |")
    L.append("| Per-header declarations + includes document | `headers "
             "<tree> --subdir <dir> [--recursive] --out FILE` |")
    L.append("| Raw SQL over everything | sqlite3 views: `vfiles`, "
             "`vsymbols`, `vincludes` |")
    L.append("")
    return L


def token_rules_lines():
    """The token-efficiency rules paragraph, shared by both guide variants."""
    L = []
    L.append("**Token-efficient usage (important for agents):** never load "
             "whole SYMBOLS files into context. Start from "
             "`SYMBOLS/00-INDEX.md` (the map), then grep the one relevant "
             "file, or query the index (`lookup` / SQL) for just what you "
             "need. Read `.types.md` files (the API surface); treat "
             "`.defines.md` / `.vars.md` as grep-only reference. Dump-time "
             "filters: `--no-guards`, `--defs-only`, `--dedup`. The index "
             "itself always keeps everything.")
    return L


GUIDE_BEGIN = "<!-- repoindex:begin -->"
GUIDE_END = "<!-- repoindex:end -->"


def snapshot_note(meta):
    return ("Index coverage: snapshot %s; symbols=%s; scan errors=%s; "
            "excluded directory names=%s. This is not a live tree check. "
            "Header parsing is heuristic, not a compiler-complete symbol inventory.\n\n" %
            (meta.get("updated", "unknown"), meta.get("symbols_enabled", "unknown"),
             meta.get("error_count", "unknown"), meta.get("ignore_directories", "unknown")))


def merge_guide(existing, text, replace=False):
    """Replace only our delimited block; protect hand-written instructions."""
    block = GUIDE_BEGIN + "\n" + text.rstrip() + "\n" + GUIDE_END + "\n"
    if replace or not existing:
        return block
    if existing.count(GUIDE_BEGIN) == 1 and existing.count(GUIDE_END) == 1:
        start = existing.index(GUIDE_BEGIN)
        end = existing.index(GUIDE_END)
        if end < start:
            raise ValueError("reversed repoindex markers")
        return existing[:start] + block.rstrip("\n") + existing[end + len(GUIDE_END):]
    raise ValueError("existing file has no unique repoindex block; use a new "
                     "output path or --replace only after preserving curated text")


def emit_guide(args, text):
    if not args.out:
        sys.stdout.write(text)
        return
    destinations = [os.path.abspath(args.out)]
    for alias in (getattr(args, "also", None) or "").split(","):
        if alias.strip():
            destinations.append(os.path.abspath(os.path.join(
                os.path.dirname(destinations[0]), alias.strip())))
    prepared = []
    # Preflight every alias before changing any file.
    for dest in dict.fromkeys(destinations):
        if os.path.islink(dest):
            sys.exit("Refusing symlink output: %s" % dest)
        existing = Path(dest).read_text(encoding="utf-8") if os.path.exists(dest) else ""
        try:
            merged = merge_guide(existing, text, getattr(args, "replace", False))
        except ValueError as exc:
            sys.exit("Cannot write %s: %s" % (dest, exc))
        prepared.append((dest, merged))
    for dest, merged in prepared:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".repoindex-guide-", dir=os.path.dirname(dest))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(merged)
            if os.path.exists(dest):
                os.chmod(tmp, stat.S_IMODE(os.stat(dest).st_mode))
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        print("Wrote %s (%s)" % (dest, human_size(len(merged))))


def cmd_status(args):
    db = open_db_required(resolve_db(args))
    meta = dict(db.execute("SELECT key,value FROM meta"))
    db.close()
    report = {"metadata": meta, "live_tree_verified": False,
              "coverage_known": all(k in meta for k in
                  ("error_count", "ignore_directories", "symbols_enabled"))}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("Stored snapshot (not a live freshness check):")
        for key, value in sorted(meta.items()):
            print("  %s: %s" % (key, value))
        if not report["coverage_known"]:
            print("Coverage metadata unavailable in this older index; rebuild explicitly if needed.")


def compact_agentsmd(root, title, db_path, total_files, total_bytes,
                     n_symbols, n_sym_headers):
    """Minimal agent guide for context-limited hosts (LM Studio and other
    local models): index facts, the which-tool table, and token rules only."""
    L = []
    L.append("# %s (compact)" % title)
    L.append("")
    L.append("Navigation guide for agents. Generate a separate report with --full "
             "only when the complete directory map and census are needed; "
             "this guide keeps only what an agent needs to *find* things.")
    L.append("")
    L.append("## The index")
    L.append("")
    L.append("`%s`: %d files (%s), %d exposed header symbols from %d "
             "headers, plus extracted header includes. SQLite; query via views "
             "`vfiles` / `vsymbols` / `vincludes`. Indexed queries below read a snapshot; headers and dupes also access source files."
             % (db_path, total_files, human_size(total_bytes), n_symbols,
                n_sym_headers))
    L.append("")
    L.extend(case_table_lines(db_path))
    L.append("## Token rules")
    L.append("")
    L.extend(token_rules_lines())
    L.append("")
    L.append("Regenerate: `python3 repoindex.py agentsmd %s --compact "
             "--out <this file>`" % root)
    L.append("")
    return "\n".join(L).rstrip() + "\n"


def cmd_agentsmd(args):
    root = os.path.abspath(args.root)
    db_path = resolve_db(args, root)
    if args.reindex:
        print("Building index first -> %s" % db_path)
        cmd_index(argparse.Namespace(
            root=root, db=db_path, no_ignore=False,
            ignore=args.ignore, no_symbols=False, jobs=0))
    db = open_db_required(db_path)
    meta = dict(db.execute("SELECT key, value FROM meta"))
    if os.path.realpath(meta.get("root", "")) != os.path.realpath(root):
        db.close()
        sys.exit("Index root does not match requested root; select the correct "
                 "--db or explicitly --reindex.")
    name = os.path.basename(root.rstrip(os.sep)) or root
    title = args.title or "%s - agent guide" % name
    if getattr(args, "compact", False):
        totals = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files").fetchone()
        ns = db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        nh = db.execute("SELECT COUNT(*) FROM (SELECT dir_id,file FROM symbols "
                        "GROUP BY dir_id,file)").fetchone()[0]
        text = compact_agentsmd(root, title, db_path, *totals, ns, nh)
        db.close()
        emit_guide(args, snapshot_note(meta) + text)
        return
    rows = db.execute("SELECT path, size FROM vfiles").fetchall()
    ext_rows = db.execute(
        "SELECT ext, COUNT(*), SUM(size) FROM vfiles GROUP BY ext "
        "ORDER BY SUM(size) DESC").fetchall()
    n_symbols = db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    n_sym_headers = db.execute(
        "SELECT COUNT(DISTINCT dir_id || ':' || file) "
        "FROM symbols").fetchone()[0]
    total_files = len(rows)
    total_bytes = sum(r[1] for r in rows)
    counts, sizes = dir_aggregates(rows)

    name = os.path.basename(root.rstrip(os.sep)) or root
    title = args.title or "%s - agent guide" % name
    today = meta.get("updated", "unknown")
    depth = args.depth

    L = []
    L.append("# %s" % title)
    L.append("")
    L.append("Index snapshot: %s. Structural sections (trees, census, header/symbol "
             "map) are generated in full from the file index - no sampling, "
             "no cutouts." % today)
    L.append("")
    L.append("Regenerate:")
    L.append("```sh")
    L.append("python3 repoindex.py index %s" % root)
    L.append("python3 repoindex.py agentsmd %s --out %s"
             % (root, args.out or "AGENTS.md"))
    L.append("```")
    L.append("")

    L.append("## Workspace roles")
    L.append("")
    L.append("<!-- TODO(curate): one bullet per tree: buildable vs read-only "
             "reference, device/codename targets, tooling paths. -->")
    L.append("")
    L.append("- `%s`: %d files, %s total." %
             (root, total_files, human_size(total_bytes)))
    L.append("")

    L.append("## Directory map")
    L.append("")
    L.append("Recursive file counts and sizes, every directory to depth %d."
             % depth)
    L.append("")
    L.append("```")
    dirs_sorted = sorted(d for d in counts if d.count("/") < depth)
    for d in dirs_sorted:
        indent = "  " * d.count("/")
        L.append("%s%-40s %10d files  %12s" % (
            indent, os.path.basename(d) + "/", counts[d],
            human_size(sizes[d])))
    L.append("```")
    L.append("")

    L.append("## File-type census")
    L.append("")
    L.append("Every extension present in the tree, sorted by total bytes.")
    L.append("")
    L.append("| Extension | Files | Bytes |")
    L.append("|-----------|------:|------:|")
    for ext, cnt, byt in ext_rows:
        L.append("| `%s` | %d | %s |" % (ext or "(none)", cnt,
                                          human_size(byt or 0)))
    L.append("")

    if getattr(args, "key_files", False):
        L.append("## Key files")
        L.append("")
        L.append("Every known build/config/doc file in the tree, grouped by "
                 "file name. Complete lists, no cutouts.")
        L.append("")
        by_name = defaultdict(list)
        for (p, _s) in rows:
            base = p.rsplit("/", 1)[-1]
            if KEY_FILE_RE.match(base):
                by_name[base].append(p)
        if not by_name:
            L.append("No known build/doc/config files found.")
            L.append("")
        else:
            for base in sorted(by_name, key=str.lower):
                paths = sorted(by_name[base])
                plural = "" if len(paths) == 1 else "s"
                L.append("**`%s`** - %d file%s:" % (base, len(paths), plural))
                L.append("")
                L.append("```")
                L.extend(paths)
                L.append("```")
                L.append("")

    L.append("## Header and symbol index")
    L.append("")
    hdr_counts = defaultdict(int)
    for (p, _s) in rows:
        ext = os.path.splitext(p)[1].lower()
        if ext in HEADER_EXTS:
            hdr_counts[p.split("/", 1)[0]] += 1
    total_hdr = sum(hdr_counts.values())
    L.append("%d C/C++ headers (%s) in the tree; the SQLite index maps **%d "
             "exposed symbols** (classes, structs, enums, unions, macros and "
             "global variables) across %d headers, plus the full include "
             "graph." %
             (total_hdr, ", ".join(sorted(HEADER_EXTS)), n_symbols,
              n_sym_headers))
    L.append("")
    for top in sorted(hdr_counts):
        L.append("- `%s/`: %d headers" % (top, hdr_counts[top]))
    L.append("")
    L.append("The index lives at `%s` (compact, normalized). Query it "
             "directly, or dump readable markdown per component:" % db_path)
    L.append("```sh")
    L.append("# find which header exposes a class seen in logs:")
    L.append("python3 repoindex.py lookup CameraMode Timer")
    L.append("# cross-check a whole logcat against the index (ranked")
    L.append("# summary table first; mega-symbols collapse to digests):")
    L.append("python3 repoindex.py lookup --from-log /path/to/logcat.txt \\")
    L.append("    --root <tree> --min-count 3")
    L.append("# split into per-component markdown, adaptive file sizes:")
    L.append("python3 repoindex.py symbols --out-dir SYMBOLS/   "
             "# splits any file over --max-per-file (default 100000)")
    L.append("# or a single component only:")
    L.append("python3 repoindex.py symbols --path-prefix <component> \\")
    L.append("    --out <component>-symbols.md")
    L.append("```")
    L.append("")
    L.extend(token_rules_lines())
    L.append("")
    L.append("Generate a complete per-header inventory (declarations + "
             "includes, no cutouts):")
    L.append("```sh")
    L.append("python3 repoindex.py headers %s --subdir <dir> --recursive \\"
             % root)
    L.append("    --title \"Header index\" --out HEADERS.md")
    L.append("```")
    L.append("")

    L.extend(case_table_lines(db_path))
    L.append("## Efficient searches")
    L.append("")
    tops = sorted(d for d in counts if "/" not in d)
    L.append("Narrow to a component first; search from `%s`:" % root)
    L.append("")
    L.append("```sh")
    L.append("rg -n 'pattern' %s" % (" ".join(tops[:4]) if tops else "."))
    L.append("# include hidden/ignored files and cross-check empty results:")
    L.append("rg -uuu -n 'pattern' %s" % (tops[0] if tops else "."))
    L.append("```")
    L.append("")
    L.append("Query the file index directly:")
    L.append("")
    L.append("```sh")
    L.append("python3 repoindex.py search '*.bp' --db %s" % db_path)
    L.append("python3 repoindex.py tree --db %s --depth 3" % db_path)
    L.append("```")
    L.append("")
    L.append("```sql")
    L.append("-- which headers expose a symbol")
    L.append("SELECT name, kind, path FROM vsymbols WHERE name = 'CameraMode';")
    L.append("-- who includes a header")
    L.append("SELECT DISTINCT path FROM vincludes WHERE target = 'CameraMode.h';")
    L.append("-- files changed most recently")
    L.append("SELECT path, datetime(mtime,'unixepoch') AS modified")
    L.append("FROM vfiles ORDER BY mtime DESC LIMIT 50;")
    L.append("```")
    L.append("")

    L.append("## Verified observations and open leads")
    L.append("")
    L.append("<!-- TODO(curate): hand-maintained findings. Generated sections "
             "above are rebuilt from the index. Put curated instructions "
             "outside the repoindex:begin/end markers so regeneration "
             "preserves them. -->")
    L.append("")

    if getattr(args, "compact", False):
        text = compact_agentsmd(root, title, db_path, total_files,
                                total_bytes, n_symbols, n_sym_headers)
    else:
        text = "\n".join(L).rstrip() + "\n"
    db.close()
    emit_guide(args, snapshot_note(meta) + text)


# ---------------------------------------------------------------------------
# stats / tree / search / dupes
# ---------------------------------------------------------------------------

def cmd_stats(args):
    db = open_db_required(resolve_db(args))
    total_files, total_bytes = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM vfiles").fetchone()
    n_symbols = db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    n_includes = db.execute("SELECT COUNT(*) FROM includes").fetchone()[0]
    n_dirs = db.execute("SELECT COUNT(*) FROM dirs").fetchone()[0]
    print("Files: %d   Bytes: %s   Symbols: %d   Include edges: %d   Dirs: %d"
          % (total_files, human_size(total_bytes), n_symbols, n_includes,
             n_dirs))
    print("\nPer-extension (all):")
    for ext, cnt, byt in db.execute(
            "SELECT ext, COUNT(*), SUM(size) FROM vfiles GROUP BY ext "
            "ORDER BY SUM(size) DESC"):
        print("  %-12s %10d  %12s" % (ext or "(none)", cnt,
                                      human_size(byt or 0)))
    limit = args.limit if args.limit and args.limit > 0 else None
    q = "SELECT path, size FROM vfiles ORDER BY size DESC"
    if limit:
        q += " LIMIT %d" % limit
    print("\nLargest files%s:" % ("" if not limit else " (top %d)" % limit))
    for path, size in db.execute(q):
        print("  %12s  %s" % (human_size(size), path))


def cmd_tree(args):
    db = open_db_required(resolve_db(args))
    rows = db.execute("SELECT path, size FROM vfiles").fetchall()
    counts, sizes = dir_aggregates(rows)
    dirs_sorted = sorted(d for d in counts if d.count("/") < args.depth)
    for d in dirs_sorted:
        indent = "  " * d.count("/")
        print("%s%-40s %10d files  %12s" % (
            indent, os.path.basename(d) + "/", counts[d],
            human_size(sizes[d])))


def cmd_search(args):
    db = open_db_required(resolve_db(args))
    pat = args.pattern
    sql = "SELECT path, size FROM vfiles WHERE "
    conds, params = [], []
    if any(c in pat for c in "*?"):
        if "/" in pat or pat.startswith("*"):
            conds.append("path GLOB ?")
            params.append(pat)
        else:
            conds.append("(path GLOB ? OR path GLOB ?)")
            params += ["*/" + pat, pat]
    else:
        conds.append("path LIKE ?")
        params.append("%" + pat + "%")
    if args.ext:
        conds.append("ext = ?")
        params.append(args.ext if args.ext.startswith(".")
                      else "." + args.ext)
    sql += " AND ".join(conds) + " ORDER BY path"
    if args.limit and args.limit > 0:
        sql += " LIMIT %d" % args.limit
    rows = db.execute(sql, params).fetchall()
    for path, size in rows:
        print("%12s  %s" % (human_size(size), path))
    print("%d matches" % len(rows), file=sys.stderr)


def cmd_dupes(args):
    db = open_db_required(resolve_db(args))
    root_row = db.execute("SELECT value FROM meta WHERE key='root'").fetchone()
    root = root_row[0] if root_row else "."
    groups = db.execute(
        "SELECT size, COUNT(*) c FROM vfiles WHERE size > 0 "
        "GROUP BY size HAVING c > 1 ORDER BY size*c DESC").fetchall()
    print("%d same-size groups; hashing to verify..." % len(groups))
    shown = 0
    for size, _c in groups:
        paths = [p for (p,) in db.execute(
            "SELECT path FROM vfiles WHERE size = ?", (size,))]
        hashes = defaultdict(list)
        for p in paths:
            try:
                h = hashlib.sha1()
                candidate = os.path.join(root, p)
                if not stat.S_ISREG(os.lstat(candidate).st_mode):
                    continue
                with open(candidate, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                hashes[h.hexdigest()].append(p)
            except OSError:
                continue
        for members in hashes.values():
            if len(members) > 1:
                shown += 1
                print("\n%s x %d:" % (human_size(size), len(members)))
                for m in sorted(members):
                    print("  %s" % m)
    print("\n%d duplicate sets" % shown)


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Full-coverage file + symbol indexing for huge source "
                    "trees.")
    ap.add_argument("--version", action="version", version="repoindex " + VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="Index a tree into SQLite (multithreaded)")
    p.add_argument("root")
    p.add_argument("--db", help="Index path (default: <root>/.repoindex.db)")
    p.add_argument("--ignore", action="append",
                   help="Extra directory name to skip (repeatable)")
    p.add_argument("--no-ignore", action="store_true",
                   help="Index everything, including VCS metadata")
    p.add_argument("--no-symbols", action="store_true",
                   help="Skip header parsing (faster, files only)")
    p.add_argument("--jobs", type=int, default=0,
                   help="Worker threads for the scan (default: min(32, "
                        "4 x CPU cores); --jobs 1 = single-threaded)")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("lookup", help="Symbol -> header/decl/includers "
                                      "(built for log digging)")
    p.add_argument("names", nargs="*", help="Symbol names, e.g. CameraMode")
    p.add_argument("--db")
    p.add_argument("--root", default=".")
    p.add_argument("--from-log", metavar="FILE",
                   help="Extract candidate symbols from a log file and "
                        "look up every one")
    p.add_argument("--substr", action="store_true",
                   help="Substring match instead of exact")
    p.add_argument("--verbose", action="store_true",
                   help="Also list includers and every forward-declaration "
                        "header (default on for explicit names)")
    p.add_argument("--no-stoplist", action="store_true",
                   help="With --from-log: also look up ubiquitous noise "
                        "tokens (NULL, ERROR, INFO, ...)")
    p.add_argument("--min-count", type=int, default=1,
                   help="With --from-log: ignore tokens seen fewer than N "
                        "times in the log (default: 1)")
    p.add_argument("--max-defs", type=int, default=25,
                   help="With --from-log: symbols with more than N "
                        "definitions get a compact digest block (default: "
                        "25; 0 = never collapse)")
    p.add_argument("--no-summary", action="store_true",
                   help="With --from-log: skip the ranked summary table")
    p.add_argument("--summary-only", action="store_true",
                   help="With --from-log: print only the ranked summary "
                        "table")
    p.set_defaults(fn=cmd_lookup)

    p = sub.add_parser("symbols", help="Dump the symbol inventory (streamed)")
    p.add_argument("--db")
    p.add_argument("--root", default=".")
    p.add_argument("--kind", choices=["class", "struct", "enum", "union",
                                      "define", "var"])
    p.add_argument("--pattern", help="Glob on symbol name, e.g. '*Photo*'")
    p.add_argument("--path-prefix",
                   help="Only symbols under this path, e.g. "
                        "vendor/xiaomi/camera")
    p.add_argument("--out", help="Write one complete markdown file (streamed)")
    p.add_argument("--out-dir",
                   help="Write per-component markdown into this directory, "
                        "split adaptively so no file exceeds --max-per-file "
                        "symbols (recommended for huge trees)")
    p.add_argument("--max-per-file", type=int, default=100000,
                   help="Max symbols per file for --out-dir (default: "
                        "100000)")
    p.add_argument("--no-guards", action="store_true",
                   help="Skip include-guard macros in dumps (they stay in "
                        "the index; small token saver)")
    p.add_argument("--defs-only", action="store_true",
                   help="Skip forward declarations in dumps; keep "
                        "definitions, macros and variables only")
    p.add_argument("--dedup", action="store_true",
                   help="Merge identical name+kind+decl rows, listing every "
                        "header on the merged row (console/--out only)")
    p.set_defaults(fn=cmd_symbols)

    p = sub.add_parser("headers", help="Full header inventory markdown")
    p.add_argument("root")
    p.add_argument("--subdir", action="append",
                   help="Directory under root to scan (repeatable; "
                        "default: root itself)")
    p.add_argument("--pattern", action="append",
                   help="Filename glob (repeatable; default: *.h)")
    p.add_argument("--recursive", action="store_true",
                   help="Recurse into subdirectories (default: direct files "
                        "only, like the CAMERA_HEADERS.md example)")
    p.add_argument("--title", help="Document title")
    p.add_argument("--out", help="Output file (default: stdout)")
    p.set_defaults(fn=cmd_headers)

    p = sub.add_parser("agentsmd", help="Generate structural AGENTS.md")
    p.add_argument("root")
    p.add_argument("--db", help="Index path (default: <root>/.repoindex.db)")
    p.add_argument("--out", help="Output file (default: stdout)")
    p.add_argument("--title", help="Document title")
    p.add_argument("--depth", type=int, default=2,
                   help="Directory map depth (default: 2)")
    p.add_argument("--replace", action="store_true",
                   help="Explicitly replace an unmarked guide; default preserves curated text")
    p.add_argument("--reindex", action="store_true",
                   help="Rebuild the index before generating")
    p.add_argument("--ignore", action="append",
                   help="Extra directory name to skip with --reindex")
    p.add_argument("--key-files", action="store_true",
                   help="Also include a Key files section (build/config "
                        "files: *.mk, Android.bp, Makefiles, ...). Off by "
                        "default - those matter for building the tree, not "
                        "for searching reference code.")
    guide_mode = p.add_mutually_exclusive_group()
    guide_mode.add_argument("--compact", dest="compact", action="store_true",
                            help="Minimal navigation guide (default)")
    guide_mode.add_argument("--full", dest="compact", action="store_false",
                            help="Explicitly include the full structural inventory")
    p.set_defaults(compact=True)
    p.add_argument("--also",
                   help="Comma-separated extra filenames to write the same "
                        "guide to, next to --out (e.g. CLAUDE.md,GEMINI.md,"
                        ".github/copilot-instructions.md)")
    p.set_defaults(fn=cmd_agentsmd)

    p = sub.add_parser("status", help="Stored snapshot provenance and coverage (no scan)")
    p.add_argument("--db")
    p.add_argument("--root", default=".")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("stats", help="Index statistics")
    p.add_argument("--db")
    p.add_argument("--root", default=".")
    p.add_argument("--limit", type=int, default=20,
                   help="Cap 'largest files' list (0 = no limit)")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("tree", help="Directory map from the index")
    p.add_argument("--db")
    p.add_argument("--root", default=".")
    p.add_argument("--depth", type=int, default=2)
    p.set_defaults(fn=cmd_tree)

    p = sub.add_parser("search", help="Find files by glob or substring")
    p.add_argument("pattern")
    p.add_argument("--db")
    p.add_argument("--root", default=".")
    p.add_argument("--ext", help="Filter by extension, e.g. .h or h")
    p.add_argument("--limit", type=int, default=0,
                   help="Cap results (default: 0 = print every match)")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("dupes", help="Find duplicate files (SHA-1 verified)")
    p.add_argument("--db")
    p.add_argument("--root", default=".")
    p.set_defaults(fn=cmd_dupes)

    args = ap.parse_args()
    if args.cmd == "agentsmd" and args.also and not args.out:
        ap.error("--also requires --out")
    if args.cmd == "agentsmd" and args.key_files and args.compact:
        ap.error("--key-files requires --full")
    args.fn(args)


if __name__ == "__main__":
    main()
