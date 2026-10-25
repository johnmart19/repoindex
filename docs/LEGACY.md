# Advanced single-tree interface

The original `repoindex.py` commands and Python helpers remain available for
existing single-tree databases. Prefer the bounded workspace CLI for new agent
searches. Legacy exports can be large: write them to files, not chat context.

| Command | Role |
| --- | --- |
| `index ROOT` | Explicit file/header scan into `<ROOT>/.repoindex.db` or `--db` |
| `status --root ROOT --json` | Saved single-tree provenance; no scan |
| `search PATTERN --db DB --limit 20 --paths-only` | File lookup |
| `lookup NAME --db DB` | Header definitions, implementation candidates and includers |
| `symbols --db DB --out FILE` | Stream a symbol export to a file |
| `headers ROOT --subdir DIR --out FILE` | Live header inventory |
| `agentsmd ROOT --out INDEX_GUIDE.md` | Compact generated guide from an existing index |
| `stats`, `tree`, `dupes` | Inventory reports and on-demand duplicate hashing |

Use each command's `--help` for its exact flags. Query connections are read-only
and do not silently create missing databases. Guides never scan unless explicitly
requested with `--reindex`; marker ownership protects curated output. `--full`
reports and symbol dumps are optional exports, not startup instructions.

The legacy scanner builds only file/header data. Workspace builds add complete
implementation text and portable per-tree chunks. Updating a tool copy is separate
from refreshing a database. A copied DB supports indexed queries; live headers,
reindexing and duplicate hashing still require source access.

Same-basename implementation/include candidates are heuristic and may group
unrelated same-named headers. Header parser limitations still apply. Consult
[format guarantees](FORMAT.md) before interpreting a saved index as complete
language or runtime evidence.
