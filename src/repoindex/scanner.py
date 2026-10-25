"""Bounded parallel source traversal and batched SQLite ingestion."""
import concurrent.futures
from contextlib import closing
import fnmatch
import json
import stat
import os
import re
import sys
import time
from .database import SCHEMA, SCHEMA_VERSION, datetime_iso, human_size, open_db, posix_rel, resolve_db, schema_is_current
from .headers import HEADER_EXTS, parse_header


DEFAULT_IGNORES = {".git", ".repo", ".svn", ".hg"}


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


KEY_FILE_RE = re.compile("^(?:" + "|".join(
    fnmatch.translate(p).replace("\\Z", "")
    for p in KEY_FILE_PATTERNS) + ")$", re.IGNORECASE)


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


def build_index(root, db_path=None, jobs=0, ignores=None, with_symbols=True, no_ignore=False):
    """Build one file/header snapshot; the caller owns atomic publication."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise ValueError("Not a directory: %s" % root)
    excluded = set() if no_ignore else set(DEFAULT_IGNORES)
    excluded.update(ignores or [])
    ignores = excluded
    jobs = max(1, jobs or default_jobs())
    db_path = db_path or os.path.join(root, ".repoindex.db")
    with closing(open_db(db_path)) as db:
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
