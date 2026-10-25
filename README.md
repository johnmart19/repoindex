# repoindex

Standalone, local search for large source workspaces. It indexes paths, heuristic
C/C++ header declarations/includes, and complete `.cpp`/`.cc` implementation text.
Ready SQLite bundles restore automatically; queries never rebuild source indexes.

Run the complete directory with Python—no installation, service or model API:

```bash
cd /path/to/workspace
python3 /path/to/repoindex workspace status
python3 /path/to/repoindex workspace files '*Parser*'
python3 /path/to/repoindex workspace impl 'Parser::parse' --scope core --limit 10
python3 /path/to/repoindex workspace source platform/src/parser.cpp --line 120 --limit 20
```

The workspace's `repoindex.json` selects trees, scopes, optional benchmark cases
and a generated guide. All configured trees are searched by default. The tool
contains no project-specific paths or queries.

| Command | Purpose |
| --- | --- |
| `files`, `symbol`, `impl`, `source` | Indexed paths, header symbols, body text and line ranges |
| `text` | Bounded live ripgrep search; requires source files |
| `scopes`, `status` | Navigation and saved coverage/cache readiness |
| `restore` | Prewarm ready bundles without scanning sources |
| `index`, `implement`, `optimize` | Explicit source refresh, body ingestion or query-index upgrade |
| `verify`, `guide`, `benchmark` | Explicit audits, navigation generation and configured measurements |

Human-readable output is the default; use `--json` for JSON Lines. Queries default
to **20 results**, **400-character excerpts** and a **12,000-byte stdout budget**.
Narrow with `--tree` and either `--scope` or `--path-prefix` before raising limits.
The Python APIs retain full records. Bodies are complete in storage; display caps
never remove indexed content.

Requirements: Linux/WSL, Python 3.8+, SQLite 3.34+ with FTS5/trigram for ready
content bundles. Ripgrep is needed only for `text`. No third-party Python packages.
Keep the tool's Python files together; `python3 repoindex.py workspace …` is an
equivalent entry point.

Read only the documentation needed:

- [Usage and configuration](docs/USAGE.md)
- [Maintenance and recovery](docs/MAINTENANCE.md)
- [Storage and matching guarantees](docs/FORMAT.md)
- [Advanced single-tree commands](docs/LEGACY.md)

Run `python3 scripts/check.py` for standalone fixtures, documentation budgets and
link/whitespace checks. It does not access a network, model, device or real source
workspace. Ready Android bundles are maintained by
[references_code](https://github.com/PocoF3Releases/references_code).
