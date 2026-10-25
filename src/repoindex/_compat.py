"""Lazy pre-package exports; internal modules never import this compatibility layer."""
from importlib import import_module
from functools import wraps

EXPORTS = {
    "VERSION": "_version",
    "SCHEMA_VERSION": "database",
    "SCHEMA": "database",
    "human_size": "database",
    "posix_rel": "database",
    "open_db": "database",
    "schema_is_current": "database",
    "open_db_required": "database",
    "resolve_db": "database",
    "datetime_iso": "database",
    "HEADER_EXTS": "headers",
    "SKIP_NAME_TOKENS": "headers",
    "BLOCK_COMMENT": "headers",
    "LINE_COMMENT": "headers",
    "DECL_RE": "headers",
    "INCLUDE_RE": "headers",
    "DEFINE_RE": "headers",
    "VAR_RE": "headers",
    "NAME_RE": "headers",
    "strip_comments": "headers",
    "looks_like_macro": "headers",
    "symbol_name": "headers",
    "parse_header": "headers",
    "DEFAULT_IGNORES": "scanner",
    "IMPL_EXTS": "scanner",
    "KEY_FILE_PATTERNS": "scanner",
    "KEY_FILE_RE": "scanner",
    "default_jobs": "scanner",
    "scan_directory": "scanner",
    "cmd_index": "legacy_cli",
    "LOG_TOKEN_RE": "lookup",
    "JUNK_TOKEN_LEN": "lookup",
    "HEXISH_RE": "lookup",
    "HEXSTR_RE": "lookup",
    "LOG_STOPLIST": "lookup",
    "plausible_symbol_token": "lookup",
    "is_junk_token": "lookup",
    "is_forward_decl": "lookup",
    "impl_candidates": "lookup",
    "wrap_paths": "lookup",
    "lookup_symbol": "lookup",
    "print_symbol": "lookup",
    "print_symbol_summary": "lookup",
    "cmd_lookup": "lookup",
    "like_escape": "reports",
    "symbol_query": "reports",
    "GUARD_DECL_RE": "reports",
    "GUARD_NAME_RE": "reports",
    "is_include_guard": "reports",
    "keep_row": "reports",
    "write_symbols_md": "reports",
    "write_symbols_dedup": "reports",
    "symbols_out_dir": "reports",
    "cmd_symbols": "reports",
    "cmd_headers": "reports",
    "dir_aggregates": "reports",
    "case_table_lines": "guides",
    "token_rules_lines": "guides",
    "GUIDE_BEGIN": "guides",
    "GUIDE_END": "guides",
    "snapshot_note": "guides",
    "merge_guide": "guides",
    "emit_guide": "guides",
    "compact_agentsmd": "guides",
    "cmd_agentsmd": "guides",
    "cmd_status": "legacy_queries",
    "cmd_stats": "legacy_queries",
    "cmd_tree": "legacy_queries",
    "cmd_search": "legacy_queries",
    "cmd_dupes": "legacy_queries",
    "main": "cli"
}


def resolve(name):
    module = EXPORTS.get(name)
    if module is None:
        raise AttributeError(name)
    value = getattr(import_module("." + module, __package__), name)
    if name == "open_db_required" or name.startswith("cmd_"):
        @wraps(value)
        def legacy(*args, **kwargs):
            try:
                return value(*args, **kwargs)
            except ValueError as error:
                raise SystemExit(str(error)) from error
        return legacy
    return value
