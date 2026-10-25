"""Explicit deep validation; never called by ordinary queries."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from .configuration import config, contained
from .bundles import digest, manifest, ensure_db
from .database import open_db_required
from .scanner import DEFAULT_IGNORES

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
    db = open_db_required(str(db_path))
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
            ignored = set(DEFAULT_IGNORES) | set(config(root).get("ignore", ["out", "__pycache__"]))
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
                        from . import implementations as implementation_index
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
