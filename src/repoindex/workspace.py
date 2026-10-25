"""Importable multi-tree build/query operations; no CLI parsing or process exits."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from .configuration import config, selected, contained
from .bundles import FORMAT, MAX_PART, MAX_RAW_PART, MAX_DB, digest, lock, atomic_json, pack, manifest, cached_db, ensure_db
from .database import open_db_required, prepare_search, files_sql
from .scanner import build_index
from .verification import verify

def build(root, tree, jobs=8, chunk_bytes=MAX_RAW_PART):
    source = contained(root, tree)
    if not source.is_dir():
        raise ValueError("Missing source tree " + tree)
    started = time.monotonic()
    with lock(root, tree) as cache:
        with tempfile.TemporaryDirectory(prefix="build-", dir=cache) as temp:
            db = Path(temp) / "index.db"
            build_index(source, db_path=db, jobs=jobs,
                        ignores=config(root).get("ignore", ["out", "__pycache__"]))
            from . import implementations as implementation_index
            with closing(sqlite3.connect(db)) as writer:
                implementation_index.build(writer, source, config(root).get("implementation_extensions", implementation_index.EXTENSIONS))
            result = pack(db, contained(root, "index/" + tree), tree, chunk_bytes)
    return {"tree": tree, "elapsed_seconds": round(time.monotonic() - started, 3),
            "database_bytes": result["database_bytes"], "compressed_bytes": sum(x["bytes"] for x in result["parts"]),
            "chunks": len(result["parts"]), "metadata": result["metadata"]}


def query(root, names, mode, pattern, limit=40, prefixes=None, context=0, max_chars=400):
    """Global limit across selected trees; paths always relative to the workspace."""
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
        db = open_db_required(str(ensure_db(root, tree)))
        try:
            if mode == "impl":
                from . import implementations as implementation_index
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
            reader = open_db_required(str(source))
            try:
                with closing(sqlite3.connect(db)) as writer:
                    reader.backup(writer)
            finally:
                reader.close()
            if implementations:
                from . import implementations as implementation_index
                with closing(sqlite3.connect(db)) as writer:
                    implementation_index.build(writer, contained(root, tree), config(root).get("implementation_extensions", implementation_index.EXTENSIONS))
            result = pack(db, contained(root, "index/" + tree), tree)
    return {"tree": tree, "elapsed_seconds": round(time.monotonic() - started, 3),
            "database_bytes": result["database_bytes"], "compressed_bytes": sum(x["bytes"] for x in result["parts"]),
            "backend": result["metadata"].get("search_backend"), "source_rescanned": bool(implementations), "inventory_reused": True,
            "implementation_files_read": int(result["metadata"].get("implementation_count", "0")) if implementations else 0}


def read_source(root, names, path, line=1, limit=40, max_chars=400, prefixes=None):
    from . import implementations as implementation_index
    path = path.replace("\\", "/")
    if prefixes and not any(path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes):
        return
    for tree in selected(root, names):
        if not path.startswith(tree + "/"):
            continue
        relative = path[len(tree) + 1:]
        if ".." in Path(relative).parts or relative.startswith("/"):
            raise ValueError("Source path must be workspace-relative")
        db = open_db_required(str(ensure_db(root, tree)))
        try:
            result = implementation_index.read(db, relative, line, limit, max_chars)
            if result:
                result.update(tree=tree, path=path)
                yield result
        finally:
            db.close()


def main(argv=None):
    from .workspace_cli import main as cli_main
    return cli_main(argv)
