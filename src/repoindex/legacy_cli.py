"""Compatibility parser for existing single-tree commands."""
import argparse
import sys
import sqlite3
from ._version import VERSION
from .scanner import build_index
from .lookup import cmd_lookup
from .reports import cmd_headers, cmd_symbols
from .guides import cmd_agentsmd
from .legacy_queries import cmd_dupes, cmd_search, cmd_stats, cmd_status, cmd_tree


def cmd_index(args):
    return build_index(args.root, db_path=getattr(args, "db", None),
                       jobs=getattr(args, "jobs", 0), ignores=args.ignore,
                       with_symbols=not getattr(args, "no_symbols", False),
                       no_ignore=args.no_ignore)


def main(argv=None):
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
    p.add_argument("--path-prefix", help="Restrict to one directory and descendants")
    output = p.add_mutually_exclusive_group()
    output.add_argument("--paths-only", action="store_true", help="One path per line")
    output.add_argument("--json", action="store_true", help="Stream JSON Lines records")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("dupes", help="Find duplicate files (SHA-1 verified)")
    p.add_argument("--db")
    p.add_argument("--root", default=".")
    p.set_defaults(fn=cmd_dupes)

    sub.add_parser("workspace", help="Portable per-tree bundles and multi-tree queries; use workspace --help")
    args = ap.parse_args(argv)
    if args.cmd == "agentsmd" and args.also and not args.out:
        ap.error("--also requires --out")
    if args.cmd == "agentsmd" and args.key_files and args.compact:
        ap.error("--key-files requires --full")
    try:
        args.fn(args)
    except (OSError, ValueError, sqlite3.Error) as error:
        print("repoindex: " + str(error), file=sys.stderr)
        return 1
    return 0
