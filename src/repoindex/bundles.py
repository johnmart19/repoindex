"""Immutable Git-sized bundles, atomic manifests and verified cache restoration."""
from contextlib import contextmanager, closing
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import zlib
from .configuration import contained
from .database import open_db_required, prepare_search

FORMAT = 1
MAX_PART = 50 * 1024 ** 2
MAX_RAW_PART = 32 * 1024 ** 2
MAX_DB = 16 * 1024 ** 3

def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 ** 2), b""):
            h.update(block)
    return h.hexdigest()


@contextmanager
def lock(root, name):
    cache = contained(root, ".cache/repoindex/" + name)
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "writer.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield cache


def atomic_text(path, text):
    """Replace a UTF-8 document atomically and clean staging on every failure."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def atomic_json(path, data):
    atomic_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


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
    if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("tree") != tree:
        raise ValueError("Incompatible bundle identity/format")
    if not re.fullmatch(r"[a-f0-9]{64}", data.get("database_sha256", "")):
        raise ValueError("Invalid database checksum")
    if not isinstance(data.get("database_bytes"), int) or not 0 < data["database_bytes"] <= MAX_DB:
        raise ValueError("Invalid database size")
    metadata = data.get("metadata")
    if not isinstance(metadata, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in metadata.items()):
        raise ValueError("Invalid manifest metadata")
    parts = data.get("parts")
    if not isinstance(parts, list) or not 0 < len(parts) <= 16384:
        raise ValueError("Invalid chunk list")
    total = 0
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("Invalid chunk entry")
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
                    try:
                        with gzip.open(path, "rb") as source:
                            # Bounded decompression, including a byte to detect oversized input.
                            raw = source.read(part["raw_bytes"] + 1)
                    except (OSError, EOFError, zlib.error) as error:
                        raise ValueError("Cannot decompress chunk: " + part["file"]) from error
                    if len(raw) != part["raw_bytes"]:
                        raise ValueError("Decompressed chunk size mismatch")
                    output.write(raw)
            if db.stat().st_size != data["database_bytes"] or digest(db) != data["database_sha256"]:
                raise ValueError("Restored database checksum mismatch")
            con = open_db_required(str(db))
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Restored SQLite integrity check failed")
            finally:
                con.close()
            os.replace(db, dest)
            atomic_json(receipt, {"size": dest.stat().st_size, "mtime_ns": dest.stat().st_mtime_ns})
        print("Restored %s in %.2fs (no source scan)" % (tree, time.monotonic() - started), file=sys.stderr)
        return dest
