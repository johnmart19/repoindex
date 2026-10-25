"""Protected generated guides and legacy guide reports."""
from contextlib import closing
import stat
import tempfile
from pathlib import Path
import os
import sys
from collections import defaultdict
from .database import human_size, open_db_required, resolve_db
from .headers import HEADER_EXTS
from .scanner import KEY_FILE_RE, build_index
from .reports import dir_aggregates


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
            raise ValueError("Refusing symlink output: %s" % dest)
        existing = Path(dest).read_text(encoding="utf-8") if os.path.exists(dest) else ""
        try:
            merged = merge_guide(existing, text, getattr(args, "replace", False))
        except ValueError as exc:
            raise ValueError("Cannot write %s: %s" % (dest, exc))
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
        build_index(root, db_path=db_path, ignores=args.ignore)
    with closing(open_db_required(db_path)) as db:
        meta = dict(db.execute("SELECT key, value FROM meta"))
        if os.path.realpath(meta.get("root", "")) != os.path.realpath(root):
            raise ValueError("Index root does not match requested root; select the correct "
                     "--db or explicitly --reindex.")
        name = os.path.basename(root.rstrip(os.sep)) or root
        title = args.title or "%s - agent guide" % name
        if getattr(args, "compact", False):
            totals = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files").fetchone()
            ns = db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            nh = db.execute("SELECT COUNT(*) FROM (SELECT dir_id,file FROM symbols "
                            "GROUP BY dir_id,file)").fetchone()[0]
            text = compact_agentsmd(root, title, db_path, *totals, ns, nh)
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
        emit_guide(args, snapshot_note(meta) + text)
