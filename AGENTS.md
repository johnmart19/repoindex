# repoindex development

Keep this tool standalone. Workspace paths, scopes and benchmark queries belong
in `repoindex.json`, never in Python code. Read README first and only the relevant
document under `docs/`.

- Queries are read-only and never ingest sources. Preserve immutable cache generations,
  atomic per-tree manifests and failed-build recovery.
- Default CLI output must stay bounded and readable; JSON records must remain valid.
  API records and indexed bodies remain complete. Do not load inventories into context.
- Preserve the file/header APIs, literal/glob semantics, tree/scope boundaries and
  source-free restoration. Distinguish stored evidence from live freshness.
- Use standard-library Python and SQLite; no runtime services or pip dependencies.
  Keep SQL parameterized and validate filesystem destinations before writes.
- Run `python3 scripts/check.py` after changes. Keep regression assertions enabled;
  test standalone operation and output limits rather than importing a consumer repo.
- Keep README <=4000 bytes, these rules <=1500 and the usage guide <=10000.
  Do not expand startup documentation to hold detailed reports or examples.
