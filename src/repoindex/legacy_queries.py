"""Legacy single-tree read commands."""
from contextlib import closing
import hashlib
import json
import stat
import os
import sys
from collections import defaultdict
from .database import human_size, open_db_required, resolve_db
from .reports import dir_aggregates


def cmd_status(args):
    with closing(open_db_required(resolve_db(args))) as db:
        meta = dict(db.execute("SELECT key,value FROM meta"))
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


def cmd_stats(args):
    with closing(open_db_required(resolve_db(args))) as db:
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
    with closing(open_db_required(resolve_db(args))) as db:
        rows = db.execute("SELECT path, size FROM vfiles").fetchall()
        counts, sizes = dir_aggregates(rows)
        dirs_sorted = sorted(d for d in counts if d.count("/") < args.depth)
        for d in dirs_sorted:
            indent = "  " * d.count("/")
            print("%s%-40s %10d files  %12s" % (
                indent, os.path.basename(d) + "/", counts[d],
                human_size(sizes[d])))


def cmd_search(args):
    with closing(open_db_required(resolve_db(args))) as db:
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
        prefix = getattr(args, "path_prefix", None)
        if prefix:
            # Bound a directory, not similarly named siblings; avoid LIKE wildcards.
            prefix = prefix.replace("\\", "/").strip("/")
            conds.append("(path = ? OR substr(path, 1, ?) = ?)")
            params.extend([prefix, len(prefix) + 1, prefix + "/"])
        sql += " AND ".join(conds) + " ORDER BY path"
        if args.limit and args.limit > 0:
            sql += " LIMIT %d" % args.limit
        count = 0
        for path, size in db.execute(sql, params):
            count += 1
            if getattr(args, "json", False):
                print(json.dumps({"path": path, "size": size}))
            elif getattr(args, "paths_only", False):
                print(path)
            else:
                print("%12s  %s" % (human_size(size), path))
        print("%d matches%s" % (count, " (limit reached; more may exist)"
              if args.limit and count == args.limit else ""), file=sys.stderr)


def cmd_dupes(args):
    with closing(open_db_required(resolve_db(args))) as db:
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
