# Development

## Ownership and layout

Keep an independent Git checkout such as `~/tools/repoindex` for development.
That path is a convention, not a runtime requirement. A consumer may vendor a
complete pinned copy as ordinary files; it must work without the development
checkout, submodules, a user installation or network access.

The package lives in `src/repoindex/`. Read only the module relevant to a change:

| Module | Responsibility |
| --- | --- |
| `database.py` | Schema, connection lifetime, read-only validation and path-query SQL |
| `headers.py`, `scanner.py` | Heuristic parsing, bounded traversal and batched ingestion |
| `implementations.py` | Complete implementation text, literal matching and line ranges |
| `configuration.py` | Workspace roots, safe destinations and named selection |
| `bundles.py` | Checksums, chunks, atomic manifests, locks and verified restoration |
| `workspace.py`, `verification.py` | Multi-tree services and explicit deep audits |
| `workspace_cli.py`, `cli.py` | Workspace arguments/output and application dispatch |
| `lookup.py`, `reports.py`, `guides.py` | Legacy lookup, exports and protected guides |
| `legacy_cli.py`, `legacy_queries.py` | Existing single-tree parser/read commands |
| `_compat.py` | Lazy pre-package exports; never an implementation dependency |

Core services do not import the CLI or compatibility files. Workspace builds call
`scanner.build_index` directly, not a script by filesystem path. CLI handling owns
exit codes. Database services raise `IndexReadError` and close failed connections;
successful callers own the returned connection. Use `contextlib.closing` or
`try/finally`; SQLite's transaction context manager does not close connections.
Query generators release readers in `finally`; close them when stopping early.

Keep SQL, normalization, selection, atomic publication and output contracts stable
through moves. Existing bundle format 1/schema 2 needs no rebuild for this package
refactor. Changes to parser/data semantics need their own migration decision.

## Execute and install

```bash
# Direct execution requires only runtime Python/SQLite:
python3 ~/tools/repoindex workspace status --workspace ~/reference-workspace
# Optional isolated installation, if pipx is available:
pipx install ~/tools/repoindex
repoindex workspace status --workspace ~/reference-workspace
python3 -m repoindex --version  # use the environment containing the installation
```

A pipx command uses its own environment; it does not install modules into system
Python. Keep user/environment paths out of runtime code. Do not replace a pinned
consumer copy with a symlink to a mutable development checkout. Refresh it from a
verified canonical commit, retaining its consumer-owned provenance file.

`pyproject.toml` declares packaging and the console entry point, with zero runtime
dependencies. `_version.py` owns the version; setuptools reads that attribute.
Setuptools/wheel/pip/venv are development/install tools, not query dependencies.
Do not publish to PyPI or alter shell startup files as part of ordinary updates.

## Checks

```bash
python3 scripts/check.py
python3 scripts/check_package.py
```

The first runs disposable fixture tests with resource warnings enabled, plus
links, whitespace and documentation budgets. Resource warnings fail the check.
The second needs locally installed setuptools >=61, wheel, pip and venv support.
It builds an sdist, rebuilds a wheel from that archive, installs it in a temporary
isolated environment with `--no-index --no-deps`, and checks console/module/API
entry points outside the checkout. Two synthetic source trees are indexed, then
hidden together with their caches before body/path/line queries. No real source,
model, device, network, global package installation or implicit data rebuild is used.

Preserve regression assertions. Patch the module that owns a dependency in tests,
not an old re-export. Test directory/file launchers and old import routes as well
as new package imports. A Python 3.8 syntax/metadata floor is not evidence of a
runtime test on every Python/SQLite version; record the actual tested host.

Use normal commits. Consumer roots/scopes/benchmarks/evidence remain outside the
tool. After code moves, test the actual consumer's saved bundles and output caps;
reuse unchanged data audits rather than rebuilding gigabytes to test imports.

## Design references

These explain choices; they are not required startup context:

- [PyPA: src versus flat layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)
- [PyPA: command-line packaging](https://packaging.python.org/en/latest/guides/creating-command-line-tools/)
- [Python: SQLite connection management](https://docs.python.org/3.13/library/sqlite3.html)
