# Usage

## Configuration

Create `repoindex.json` at the workspace root. Source directories are ordinary
files; no submodules are required. Existing ready bundles need no initial scan.

```json
{
  "trees": ["platform", "vendor"],
  "ignore": ["out", "__pycache__"],
  "implementation_extensions": [".cpp", ".cc"],
  "scopes": {"core": ["platform/src"], "drivers": ["vendor/drivers"]},
  "guide": "docs/INDEX_GUIDE.md",
  "command": "python3 /path/to/repoindex workspace",
  "benchmarks": [{"mode": "impl", "query": "Parser::parse", "scope": "core"}]
}
```

`trees` is required: unique immediate source-directory names. Other fields are
optional. Exclusions are directory names. `scopes` map names to paths below the
configured trees. `guide` is a workspace-relative generated Markdown destination;
`command` is display text for that guide, never executed. Benchmarks are explicit
workspace data, not built-in assumptions. Legacy `config/search-scopes.json` is
read only when inline scopes are absent.

Run from the workspace or a descendant; configuration is discovered upward.
Alternatively pass `--workspace /path/to/workspace` from anywhere. The tool itself
can live anywhere and does not require a consumer repository.

## Find, then inspect

```bash
python3 /path/to/repoindex workspace scopes
python3 /path/to/repoindex workspace files '*Parser*' --scope core
python3 /path/to/repoindex workspace symbol ParserMode --tree platform
python3 /path/to/repoindex workspace impl 'Parser::parse' --limit 10
python3 /path/to/repoindex workspace source platform/src/parser.cpp --line 120 --limit 20
```

All trees are selected by default. `--tree NAME` is repeatable. Choose either a
named `--scope` or repeatable `--path-prefix tree/component`; they intersect with
selected trees. An empty intersection returns no matches. Results follow configured
tree order, then path order; implementation hits follow starting line order.
`search` remains an alias for `files`.

| Mode | Matching / result |
| --- | --- |
| `files` | Case-insensitive path substring, or case-sensitive `*`/`?` glob |
| `symbol` | Exact heuristic C/C++ header symbol name |
| `impl` | Exact case-sensitive literal anywhere in complete selected implementation text |
| `source` | A workspace-qualified implementation path and bounded indexed line range |
| `text` | Live fixed-string ripgrep; `--regex` explicitly enables regex |

Body search includes comments/strings and supports multiline literals. It is not
AST/overload analysis. Short queries work but lack trigram acceleration. A hit
contains path, starting/ending line, column and a clipped line excerpt. Use a small
`source` range after a hit, rather than returning an entire function/file.

`text` requires live files. Indexed modes work without source folders. First
indexed use verifies/restores selected bundles into `.cache/repoindex/<tree>/`;
this is restoration, not reindexing. `status` is cheap saved provenance and cache
readiness, not a live freshness test. A cold cache is normal.

## Output controls

The default is compact human text with 20 results and a 12,000-byte stdout budget.
`--limit` is global across selected trees; for `source` it controls lines (maximum
200). `--max-chars` defaults to 400 and bounds excerpts/line display (16–2000).
`impl --context 1` adds nearby text; context accepts 0–5 lines and is capped at
1600 characters per hit. Stored bodies and API records are not truncated.

Use `--json` for valid JSON Lines. A reached result/output cap is reported on
stderr; narrow selection before raising `--limit` or `--max-output-bytes`. If the
first JSON record cannot fit, exit 3 emits no partial record: request fewer source
lines or a larger explicit budget. Human text may end at a UTF-8-safe budget
boundary. Symbol declarations are width-bounded in either format.

The output budget stops read-only producers promptly. Explicit maintenance still
finishes every selected tree even if its summaries exceed the display budget.
Exit codes: 0 successful/bounded output, 1 benchmark budget failed, 2 invalid input
or operation failure, 3 first JSON record too large.

## Python API

```python
from pathlib import Path
import workspace_index as workspace

root = Path('/path/to/workspace')
for row in workspace.query(root, None, 'impl', 'Parser::parse', limit=10):
    print(row)
for block in workspace.read_source(root, ['platform'],
                                   'platform/src/parser.cpp', line=120, limit=20):
    print(block)
```

Reuse a process for related queries to amortize Python startup. No persistent
server is needed. API records retain complete declarations; callers should bound
what they display. `workspace_index.main` delegates to the same CLI.

## Migration from 1.x

The workspace CLI now defaults to text; request `--json` for machine callers.
Its default result cap is 20. `repoindex.py workspace …`, the directory entry point
and existing Python query APIs remain supported. Consumer compatibility scripts
can forward to `workspace_cli.main` instead of maintaining another parser.
Benchmark configuration uses `query`; comparison accepts historical `pattern`
records as well. Old file/header bundles remain queryable; missing body data
requires an explicit maintainer ingestion, never an implicit startup rebuild.
