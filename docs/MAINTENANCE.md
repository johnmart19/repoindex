# Maintenance

Normal queries never scan or refresh source trees. Use ready manifests/chunks and
keep verified caches. Only invoke the operations below for a relevant change,
explicit audit or performance concern.

## Refresh and upgrade

```bash
python3 /path/to/repoindex workspace index --tree platform
python3 /path/to/repoindex workspace implement --tree platform
python3 /path/to/repoindex workspace optimize --tree platform
```

`index` rebuilds the selected file/header inventory and complete implementation
contents. Default implementation suffixes are `.cpp`/`.cc`; configuration can
select others. Use it after actual source changes. `implement` reads selected
implementation files once while reusing an unchanged file/header snapshot; it
fails if source metadata no longer matches. `optimize` upgrades auxiliary path
search structures without refreshing source or adding missing body contents.

Builds take a per-tree writer lock, work in a temporary cache directory and publish
immutable compressed chunks before atomically replacing the manifest. Failure
before replacement retains the previous snapshot. Trees refresh separately, not
as one transaction. Already-verified readers bypass the build lock and keep using
their immutable databases. Cold restores serialize per tree.

If `guide` is configured, CLI maintenance regenerates it after successful selected
operations. Missing trees are marked unbuilt, not scanned automatically.

## Guide ownership

```bash
python3 /path/to/repoindex workspace guide --out docs/INDEX_GUIDE.md
```

Only the `repoindex:begin/end` block is replaced; surrounding curated prose is
preserved. Symlinks and existing unmarked documents are refused. `--replace` is an
explicit migration option after preserving curated content. A guide failure after
manifest publication does not invalidate the database; regenerate the guide only.
Never generate a census over curated `AGENTS.md`.

## Verify and measure explicitly

```bash
python3 /path/to/repoindex workspace verify --live --hash-implementations
python3 /path/to/repoindex workspace benchmark --samples 5 --budget-ms 100 --out measurements.json
```

Deep verification checks chunks/database hashes, SQLite/FTS consistency, logical
coverage and stored text hashes. `--live` compares paths/sizes/integer-second
mtimes. `--hash-implementations` additionally compares raw and normalized hashes
for every indexed implementation; it requires live source files. It does not
claim content hashes for all non-implementation files. Clone/copy operations may
change mtimes: inspect source identity before treating metadata drift as content
change. Saved verification never guarantees future freshness or runtime behavior.

Benchmarks use the workspace's configured cases. They warm selected bundles, take
at least three samples and optionally enforce a median budget. No network/model
or source scan is performed. `--compare` accepts previous result JSON; changed
result counts must be reviewed, not silently accepted. No benchmarks/audits run
on query startup.

## Generations, recovery and updates

Current manifests own their chunk list. To prune, stop old readers, preserve a
recovery copy, take the tree's writer lock and remove only unreferenced,
checksum-identified chunks. Keep current verified databases and receipts.
Databases are navigation artifacts, not source backups. Never copy a live WAL
piecemeal; use SQLite backup or preserve a quiescent directory consistently.

Use normal commits for updates. A requested history rewrite needs a recovery
bundle, captured expected remote head and explicit `--force-with-lease`. It does
not authorize future rewrites or changes to unrelated branches.

Keep the complete tool directory when copying/vendoring. Runtime needs no pip
packages: Linux/WSL, Python 3.8+ and SQLite with FTS5/trigram support suffice;
ripgrep is optional except for live `text`. Validate changes with
`python3 scripts/check.py` from the tool directory before publication. Preserve assertions and test the
standalone package rather than depending on a particular source workspace.

Workspace maintainers may reuse `repoindex.checks.documentation_errors` for
local Markdown/link/budget checks. It never restores databases or scans sources.
