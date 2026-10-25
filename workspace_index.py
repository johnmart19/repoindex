"""Portable, Git-sized per-tree SQLite bundles and bounded multi-tree retrieval."""
from contextlib import contextmanager, closing
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

FORMAT = 1
MAX_PART = 50 * 1024 ** 2
MAX_RAW_PART = 32 * 1024 ** 2
MAX_DB = 16 * 1024 ** 3
TOOL = Path(__file__).with_name("repoindex.py")

def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 ** 2), b""):
            h.update(block)
    return h.hexdigest()

def contained(root, relative):
    root = Path(root).resolve()
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Expected a relative path below " + str(root))
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError("Path escapes its owning directory")
    return path

def config(root):
    root = Path(root).resolve()
    data = json.loads((root / "repoindex.json").read_text())
    trees = data["trees"]
    if not isinstance(trees, list) or not trees or not all(isinstance(name, str) for name in trees):
        raise ValueError("trees must be a nonempty list of source directory names")
    if len(trees) != len(set(trees)):
        raise ValueError("trees must be unique")
    for name in trees:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ValueError("Invalid tree name")
        contained(root, name)
        contained(root, "index/" + name)
    return data

def selected(root, names=None):
    data = config(root)
    result = names or data["trees"]
    if len(result) != len(set(result)) or any(x not in data["trees"] for x in result):
        raise ValueError("Unknown or repeated tree selection")
    return result

@contextmanager
def lock(root, name):
    cache = contained(root, ".cache/repoindex/" + name)
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "writer.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield cache

def atomic_json(path, data):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")
    try:
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()

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


def pack(db, directory, tree, chunk_bytes=MAX_RAW_PART):
    """Publish immutable chunks first, then atomically switch the manifest."""
    if not 1 <= chunk_bytes <= MAX_RAW_PART:
        raise ValueError("Raw chunk size outside supported range")
    db, directory = Path(db), Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as con:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        meta = dict(con.execute("SELECT key,value FROM meta"))
        if meta.get("error_count") != "0" or meta.get("symbols_enabled") not in ("true", "false"):
            raise ValueError("Refusing to publish unknown or incomplete coverage")
        prepare_search(con)
        meta = dict(con.execute("SELECT key,value FROM meta"))
        # Published metadata contains no host-specific source path.
        con.execute("UPDATE meta SET value='.' WHERE key='root'")
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
    total = db.stat().st_size
    if total > MAX_DB:
        raise ValueError("Database exceeds supported restore size")
    parts = []
    with db.open("rb") as source:
        while True:
            raw = source.read(chunk_bytes)
            if not raw:
                break
            compressed = gzip.compress(raw, compresslevel=6, mtime=0)
            if len(compressed) > MAX_PART:
                raise ValueError("Compressed part exceeds publication cap")
            sha = hashlib.sha256(compressed).hexdigest()
            name = "part-" + sha + ".gz"
            target = contained(directory, name)
            if target.exists():
                if digest(target) != sha:
                    raise ValueError("Existing content-addressed chunk is corrupt")
            else:
                with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                    staging = Path(stream.name)
                    stream.write(compressed)
                os.replace(staging, target)
            parts.append({"file": name, "sha256": sha, "bytes": len(compressed),
                          "raw_bytes": len(raw)})
    metadata = {k: v for k, v in meta.items() if k != "root"}
    manifest = {"format": FORMAT, "tree": tree, "database_sha256": digest(db),
                "database_bytes": total, "parts": parts, "metadata": metadata,
                "live_tree_verified": False}
    atomic_json(directory / "manifest.json", manifest)
    return manifest

def manifest(root, tree):
    directory = contained(root, "index/" + tree)
    path = contained(directory, "manifest.json")
    if path.stat().st_size > 1024 ** 2:
        raise ValueError("Manifest too large")
    data = json.loads(path.read_text())
    if data.get("format") != FORMAT or data.get("tree") != tree:
        raise ValueError("Incompatible bundle identity/format")
    if not re.fullmatch(r"[a-f0-9]{64}", data.get("database_sha256", "")):
        raise ValueError("Invalid database checksum")
    if not isinstance(data.get("database_bytes"), int) or not 0 < data["database_bytes"] <= MAX_DB:
        raise ValueError("Invalid database size")
    parts = data.get("parts")
    if not isinstance(parts, list) or not 0 < len(parts) <= 16384:
        raise ValueError("Invalid chunk list")
    total = 0
    for part in parts:
        sha = part.get("sha256", "")
        if not re.fullmatch(r"[a-f0-9]{64}", sha) or part.get("file") != "part-" + sha + ".gz":
            raise ValueError("Invalid chunk name/checksum")
        contained(directory, part["file"])
        if not isinstance(part.get("bytes"), int) or not 0 < part["bytes"] <= MAX_PART:
            raise ValueError("Invalid compressed chunk size")
        if not isinstance(part.get("raw_bytes"), int) or not 0 < part["raw_bytes"] <= MAX_RAW_PART:
            raise ValueError("Invalid raw chunk size")
        total += part["raw_bytes"]
    if total != data["database_bytes"]:
        raise ValueError("Chunk sizes do not match database size")
    return data

def cached_db(root, tree, data):
    cache = contained(root, ".cache/repoindex/" + tree)
    dest = contained(cache, data["database_sha256"] + ".db")
    receipt = dest.with_suffix(".verified.json")
    if dest.is_file() and receipt.is_file():
        try:
            saved = json.loads(receipt.read_text())
            info = dest.stat()
            if saved == {"size": info.st_size, "mtime_ns": info.st_mtime_ns} and info.st_size == data["database_bytes"]:
                return dest
        except (ValueError, OSError):
            pass
    return None


def ensure_db(root, tree):
    """Restore once per bundle hash; no source scan, network or model calls."""
    data = manifest(root, tree)
    # Immutable verified generations remain readable during a potentially long rebuild.
    ready = cached_db(root, tree, data)
    if ready:
        return ready
    with lock(root, tree) as cache:
        data = manifest(root, tree)
        ready = cached_db(root, tree, data)
        if ready:
            return ready
        dest = contained(cache, data["database_sha256"] + ".db")
        receipt = dest.with_suffix(".verified.json")
        started = time.monotonic()
        directory = contained(root, "index/" + tree)
        with tempfile.TemporaryDirectory(prefix="restore-", dir=cache) as temp:
            db = Path(temp) / "index.db"
            with db.open("wb") as output:
                for part in data["parts"]:
                    path = contained(directory, part["file"])
                    if path.stat().st_size != part["bytes"] or digest(path) != part["sha256"]:
                        raise ValueError("Chunk integrity failure: " + part["file"])
                    with gzip.open(path, "rb") as source:
                        # Bounded decompression, including a byte to detect oversized input.
                        raw = source.read(part["raw_bytes"] + 1)
                    if len(raw) != part["raw_bytes"]:
                        raise ValueError("Decompressed chunk size mismatch")
                    output.write(raw)
            if db.stat().st_size != data["database_bytes"] or digest(db) != data["database_sha256"]:
                raise ValueError("Restored database checksum mismatch")
            import repoindex
            con = repoindex.open_db_required(str(db))
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Restored SQLite integrity check failed")
            finally:
                con.close()
            os.replace(db, dest)
            atomic_json(receipt, {"size": dest.stat().st_size, "mtime_ns": dest.stat().st_mtime_ns})
        print("Restored %s in %.2fs (no source scan)" % (tree, time.monotonic() - started), file=sys.stderr)
        return dest

def build(root, tree, jobs=8, chunk_bytes=MAX_RAW_PART):
    source = contained(root, tree)
    if not source.is_dir():
        raise ValueError("Missing source tree " + tree)
    started = time.monotonic()
    with lock(root, tree) as cache:
        with tempfile.TemporaryDirectory(prefix="build-", dir=cache) as temp:
            db = Path(temp) / "index.db"
            command = [sys.executable, str(TOOL), "index", str(source), "--db", str(db), "--jobs", str(jobs)]
            for item in config(root).get("ignore", ["out", "__pycache__"]):
                command += ["--ignore", item]
            subprocess.run(command, check=True)
            import implementation_index
            with closing(sqlite3.connect(db)) as writer:
                implementation_index.build(writer, source, config(root).get("implementation_extensions", implementation_index.EXTENSIONS))
            result = pack(db, contained(root, "index/" + tree), tree, chunk_bytes)
    return {"tree": tree, "elapsed_seconds": round(time.monotonic() - started, 3),
            "database_bytes": result["database_bytes"], "compressed_bytes": sum(x["bytes"] for x in result["parts"]),
            "chunks": len(result["parts"]), "metadata": result["metadata"]}

def query(root, names, mode, pattern, limit=40, prefixes=None, context=0, max_chars=400):
    """Global limit across selected trees; paths always relative to the workspace."""
    import repoindex
    if limit < 1 or mode not in ("files", "symbol", "impl"):
        raise ValueError("Positive limit and files/symbol/impl mode required")
    count = 0
    for tree in selected(root, names):
        local = []
        for prefix in prefixes or [tree]:
            prefix = prefix.replace("\\", "/").strip("/")
            if prefix == tree:
                local.append("")
            elif prefix.startswith(tree + "/"):
                local.append(prefix[len(tree) + 1:])
        if not local:
            continue
        db = repoindex.open_db_required(str(ensure_db(root, tree)))
        try:
            if mode == "impl":
                import implementation_index
                for row in implementation_index.search(db, pattern, local, context, max_chars):
                    if count == limit:
                        print("More matches exist; narrow the tree/scope or raise --limit.", file=sys.stderr)
                        return
                    count += 1
                    row = dict(row)
                    row.update(tree=tree, path=tree + "/" + row["path"])
                    yield row
                continue
            if mode == "files":
                sql, params = files_sql(db, pattern, local, limit - count + 1)
            else:
                conds, params = [], []
                for prefix in local:
                    if not prefix:
                        conds.append("1")
                    else:
                        conds.append("(path=? OR (path>=? AND path<?))")
                        params += [prefix, prefix + "/", prefix + "0"]
                where = "(" + " OR ".join(conds) + ") AND name=?"
                params += [pattern, limit - count + 1]
                sql = "SELECT DISTINCT path,kind,decl FROM vsymbols WHERE " + where + " ORDER BY path,kind,decl LIMIT ?"
            for row in db.execute(sql, params):
                if count == limit:
                    print("More matches exist; narrow the tree/scope or raise --limit.", file=sys.stderr)
                    return
                result = {"tree": tree, "path": tree + "/" + row[0]}
                if mode == "symbol":
                    result.update(kind=row[1], declaration=row[2])
                count += 1
                yield result
        finally:
            db.close()

def optimize(root, tree, implementations=False):
    """Upgrade auxiliary indexes, optionally ingesting complete implementation text."""
    original = manifest(root, tree)
    source = ensure_db(root, tree)
    if digest(source) != original["database_sha256"]:
        raise ValueError("Existing snapshot checksum mismatch")
    started = time.monotonic()
    with lock(root, tree) as cache:
        if manifest(root, tree)["database_sha256"] != original["database_sha256"]:
            raise ValueError("Snapshot changed during optimize; retry against the new snapshot")
        with tempfile.TemporaryDirectory(prefix="optimize-", dir=cache) as temp:
            db = Path(temp) / "index.db"
            import repoindex
            reader = repoindex.open_db_required(str(source))
            try:
                with closing(sqlite3.connect(db)) as writer:
                    reader.backup(writer)
            finally:
                reader.close()
            if implementations:
                import implementation_index
                with closing(sqlite3.connect(db)) as writer:
                    implementation_index.build(writer, contained(root, tree), config(root).get("implementation_extensions", implementation_index.EXTENSIONS))
            result = pack(db, contained(root, "index/" + tree), tree)
    return {"tree": tree, "elapsed_seconds": round(time.monotonic() - started, 3),
            "database_bytes": result["database_bytes"], "compressed_bytes": sum(x["bytes"] for x in result["parts"]),
            "backend": result["metadata"].get("search_backend"), "source_rescanned": bool(implementations), "inventory_reused": True,
            "implementation_files_read": int(result["metadata"].get("implementation_count", "0")) if implementations else 0}


def read_source(root, names, path, line=1, limit=40, max_chars=400, prefixes=None):
    import implementation_index
    import repoindex
    path = path.replace("\\", "/")
    if prefixes and not any(path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes):
        return
    for tree in selected(root, names):
        if not path.startswith(tree + "/"):
            continue
        relative = path[len(tree) + 1:]
        if ".." in Path(relative).parts or relative.startswith("/"):
            raise ValueError("Source path must be workspace-relative")
        db = repoindex.open_db_required(str(ensure_db(root, tree)))
        try:
            result = implementation_index.read(db, relative, line, limit, max_chars)
            if result:
                result.update(tree=tree, path=path)
                yield result
        finally:
            db.close()


def verify(root, tree, live=False, hash_implementations=False):
    """Explicit deep verification, kept off the normal query path."""
    data = manifest(root, tree)
    if data["metadata"].get("error_count") != "0":
        raise ValueError("Cannot certify unknown or incomplete scan coverage")
    directory = contained(root, "index/" + tree)
    for part in data["parts"]:
        path = contained(directory, part["file"])
        if path.stat().st_size != part["bytes"] or digest(path) != part["sha256"]:
            raise ValueError("Chunk integrity failure: " + part["file"])
    db_path = ensure_db(root, tree)
    if digest(db_path) != data["database_sha256"]:
        raise ValueError("Database checksum mismatch")
    import repoindex
    db = repoindex.open_db_required(str(db_path))
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity failure")
        files = db.execute("SELECT count(*) FROM files").fetchone()[0]
        symbols = db.execute("SELECT count(*) FROM symbols").fetchone()[0]
        includes = db.execute("SELECT count(*) FROM includes").fetchone()[0]
        total = db.execute("SELECT sum(size) FROM files").fetchone()[0] or 0
        if (files, symbols, total) != tuple(int(data["metadata"][key]) for key in ("file_count", "symbol_count", "total_bytes")):
            raise ValueError("Database counts differ from manifest")
        for table, name in [("symbols", "file"), ("includes", "file")]:
            sql = "SELECT count(*) FROM " + table + " s LEFT JOIN files f ON f.dir_id=s.dir_id AND f.name=s." + name + " WHERE f.name IS NULL"
            if db.execute(sql).fetchone()[0]:
                raise ValueError("Orphaned " + table + " rows")
        paths = dict((path, (size, int(mtime))) for path, size, mtime in db.execute("SELECT path,size,mtime FROM vfiles"))
        if len(paths) != files:
            raise ValueError("Missing/duplicate file-directory links")
        backend = data["metadata"].get("search_backend", "legacy")
        if backend != "legacy":
            if db.execute("SELECT count(*) FROM search_files").fetchone()[0] != files:
                raise ValueError("Search path coverage mismatch")
            difference = db.execute("SELECT path,name,ext,size FROM vfiles EXCEPT SELECT path,name,ext,size FROM search_files LIMIT 1").fetchone()
            if difference:
                raise ValueError("Materialized paths differ from file inventory")
            if backend == "trigram":
                # External-content consistency check is write-shaped SQL on a disposable copy.
                with tempfile.TemporaryDirectory(prefix="fts-check-") as temp:
                    with closing(sqlite3.connect(Path(temp) / "check.db")) as check:
                        db.backup(check)
                        check.execute("INSERT INTO search_paths_fts(search_paths_fts,rank) VALUES('integrity-check',1)")
        implementation_meta = data["metadata"].get("implementation_schema")
        implementation_hashes = {}
        if implementation_meta:
            ext = json.loads(data["metadata"]["implementation_extensions"])
            placeholders = ",".join("?" for _ in ext)
            expected = db.execute("SELECT count(*),coalesce(sum(size),0) FROM vfiles WHERE ext IN (" + placeholders + ")", ext).fetchone()
            actual = db.execute("SELECT count(*),coalesce(sum(size),0) FROM implementation_files").fetchone()
            if actual != expected or actual != (int(data["metadata"]["implementation_count"]), int(data["metadata"]["implementation_bytes"])):
                raise ValueError("Implementation coverage mismatch")
            missing = db.execute("SELECT path,size,mtime FROM vfiles WHERE ext IN (" + placeholders + ") EXCEPT SELECT path,size,mtime FROM implementation_files LIMIT 1", ext).fetchone()
            if missing:
                raise ValueError("Implementation paths/metadata differ from file inventory")
            with tempfile.TemporaryDirectory(prefix="implementation-fts-check-") as temp:
                with closing(sqlite3.connect(Path(temp) / "check.db")) as check:
                    db.backup(check)
                    check.execute("INSERT INTO implementation_fts(implementation_fts,rank) VALUES('integrity-check',1)")
            for path, body, sha in db.execute("SELECT path,body,body_sha256 FROM implementation_files"):
                if hashlib.sha256(body.encode("utf-8")).hexdigest() != sha:
                    raise ValueError("Stored implementation text hash mismatch: " + path)
            if hash_implementations:
                implementation_hashes = {path: (raw_sha, body_sha) for path, raw_sha, body_sha in db.execute("SELECT path,source_sha256,body_sha256 FROM implementation_files")}
        elif hash_implementations:
            raise ValueError("No implementation index to verify")
        result = {"tree": tree, "chunks_verified": len(data["parts"]), "database_integrity": "ok",
                  "files": files, "header_symbols": symbols, "includes": includes, "search_backend": backend,
                  "live_metadata_verified": False, "live_contents_hashed": False,
                  "implementation_files": int(data["metadata"].get("implementation_count", "0")),
                  "implementation_bytes": int(data["metadata"].get("implementation_bytes", "0")),
                  "implementation_hashes_verified": False}
        if hash_implementations and not live:
            raise ValueError("Implementation hash verification requires --live")
        if live:
            ignored = set(repoindex.DEFAULT_IGNORES) | set(config(root).get("ignore", ["out", "__pycache__"]))
            source = contained(root, tree)
            if not source.is_dir():
                raise ValueError("Source tree unavailable for live verification")
            changed, extra, errors, seen = [], [], [], set()
            for base, dirs, names in os.walk(source, onerror=lambda error: errors.append(str(error))):
                dirs[:] = [name for name in dirs if name not in ignored]
                for name in names:
                    path = Path(base) / name
                    rel = path.relative_to(source).as_posix()
                    try:
                        info = path.lstat()
                    except OSError as error:
                        errors.append(str(error))
                        continue
                    seen.add(rel)
                    if rel not in paths:
                        extra.append(rel)
                    elif paths[rel] != (info.st_size, int(info.st_mtime)):
                        changed.append(rel)
                    elif rel in implementation_hashes:
                        import implementation_index
                        raw = path.read_bytes()
                        raw_sha, text_sha = implementation_hashes[rel]
                        body = implementation_index.normalize(raw.decode("utf-8", errors="replace"))
                        if hashlib.sha256(raw).hexdigest() != raw_sha or hashlib.sha256(body.encode("utf-8")).hexdigest() != text_sha:
                            changed.append(rel + " (implementation SHA-256)")
            missing = sorted(set(paths) - seen)
            if changed or extra or missing or errors:
                raise ValueError("Live metadata mismatch: " + json.dumps({"changed": changed[:10], "extra": extra[:10], "missing": missing[:10], "errors": errors[:10]}))
            result["live_metadata_verified"] = True
            result["implementation_hashes_verified"] = bool(implementation_meta and hash_implementations)
        return result
    finally:
        db.close()


def main(argv=None):
    """Compatibility entry point; the shared frontend owns all CLI behavior."""
    from workspace_cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
