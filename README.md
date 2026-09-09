# repoindex.py

Full-coverage file + symbol indexing for huge source trees, built for
AI-agent workflows on WSL. Designed for multi-million-file Android/AOSP-sized
repositories (Android/AOSP-scale trees, vendor source drops, SDK mirrors).

One multithreaded pass scans an entire tree and records **every file**, the
**every class/struct/enum/union/macro/global variable each header exposes**,
and the **complete `#include` graph** — into a compact SQLite database with
no sampling and no cutouts. All reports are generated from that index in
seconds.

- Zero dependencies: Python 3.8+, standard library only.
- No `ag`/`rg` PTY quirks when driven from scripts or Windows-to-WSL calls —
  it's pure Python.
- Multithreaded scan (I/O + parsing overlapped), single-writer SQLite
  (deterministic results, verified lossless vs single-threaded).
- Compact normalized storage: shared directory table, deduplicated include
  targets, `VACUUM` after every build.

---

## Why

LLM agents digging through a huge reference tree (e.g. chasing a symbol from
a logcat line back to the header that exposes it) need three things:

1. A fast, complete, queryable map of the tree (`rg` alone can't answer
   "which header exposes `CameraMode` and who includes it").
2. Small, focused reference documents — not 200 MB markdown dumps that blow
   up a context window (225k rows ≈ 12.9M tokens for a single component!).
3. A structural `AGENTS.md` so future agents know where things live.

Measured on real trees: ~89% of header symbols are `#define` macros, only
~7% are type declarations (the actual API surface agents want to read), and
~18% of rows are cross-header duplicates. The tool's output formats are
built around that reality.

## Requirements

- Python 3.8+ (WSL Ubuntu/Debian default is fine). No pip installs.
- Optional: `sqlite3` CLI if you want to poke the database by hand
  (not required — the tool covers common queries).

## Install

```bash
# put it anywhere; examples assume your home dir
cp repoindex.py ~/
chmod +x ~/repoindex.py   # optional; always run via python3
```

## Quick start

```bash
# 1. Index BOTH trees at once (point at their common parent).
#    This is the only step that scans folders; expect minutes on huge trees.
python3 ~/repoindex.py index ~/references_code --ignore out --ignore prebuilts

# 2. Everything else is instant — reads the index at
#    ~/references_code/.repoindex.db:

# which header exposes a class seen in a log?
python3 ~/repoindex.py lookup CameraMode --root ~/references_code

# auto-extract every candidate symbol from a log and resolve all of them
python3 ~/repoindex.py lookup --from-log ~/logs/camera-crash.txt --root ~/references_code

# per-component symbol docs, split by kind and adaptive in size
python3 ~/repoindex.py symbols --root ~/references_code --out-dir ~/references_code/SYMBOLS

# structural guide for future agents
python3 ~/repoindex.py agentsmd ~/references_code \
    --title "Reference source navigation" --out ~/references_code/AGENTS.md
```

Re-run `index` whenever the tree changes; it rebuilds from scratch and
compacts the database automatically.

> **WSL tip:** keep trees under the WSL filesystem (`~/...`), not
> `/mnt/c/...` — indexing over the 9P mount is an order of magnitude slower.

---

## Commands

### `index` — scan a tree into SQLite

```bash
python3 repoindex.py index ROOT [--db PATH] [--ignore NAME]...
                      [--no-ignore] [--no-symbols] [--jobs N]
```

| Option | Default | Meaning |
|---|---|---|
| `--db PATH` | `<ROOT>/.repoindex.db` | Index file location |
| `--ignore NAME` | — | Extra directory name to skip (repeatable), e.g. `--ignore out` |
| `--no-ignore` | off | Also index `.git`, `.repo`, `.svn`, `.hg` (skipped by default) |
| `--no-symbols` | off | Files only, skip header parsing (fastest pass) |
| `--jobs N` | `min(32, 4×cores)` | Worker threads; `--jobs 1` = single-threaded |

Records per file: relative path, size, mtime. Per header (`.h/.hpp/.hh/.hxx`):
every exposed symbol (classes incl. forward declarations, structs, enums,
unions, `#define` macros, exposed global variables) and every `#include`
target. Prints progress every 100k files and a final summary:

```
Indexed 3241088 files (61.2 GB) in 95.3s (34000 files/s, incl. 12s compact)
Recorded 3202519 exposed symbols and 4811207 include edges from headers
Index size: 187.4 MB -> /home/user/references_code/.repoindex.db
```

### `lookup` — symbol → definitions / implementations / includers

```bash
python3 repoindex.py lookup NAME [NAME...] [--root DIR | --db PATH]
                       [--substr] [--from-log FILE] [--verbose] [--no-stoplist]
                       [--min-count N] [--max-defs N] [--summary-only]
```

Built for log digging. Output is **grouped per symbol and deduplicated**:
each unique definition prints once with every header containing it plus
same-basename implementation candidates (`Timer.h` → `Timer.cpp`); forward
declarations collapse to a count. `--verbose` additionally lists every
forward-declaring header and the includers of each definition header
(default on for explicit names).

- `--from-log FILE` extracts CamelCase / Capitalized / `snake_case` tokens
  from a log, skips ubiquitous noise (`NULL`, `ERROR`, `INFO`, ... — disable
  with `--no-stoplist`) and structural junk (hex blobs, digit-heavy ids,
  40+ char tracepoint names), resolves every remaining token, and ends with
  a match summary.
- From-log reports open with a **ranked summary table** (most frequent log
  tokens first; `--summary-only` prints just it, `--no-summary` skips it),
  and each block header shows how often the symbol appeared in the log.
- `--min-count N` ignores tokens seen fewer than N times in the log (unique
  one-off tokens are usually a third of a log's vocabulary).
- Symbols with more than `--max-defs N` definitions (default 25) collapse
  to a compact digest - per-kind counts, first few locations with impl
  candidates, and a drill-down command. Explicit name lookups always print
  the full listing.

```
## Timer (class) - 1 definition; forward-declared in 2 headers
  def: class Timer final : public std::enable_shared_from_this<Timer>
    @ vendor/xiaomi/.../hal/Timer.h
      impl: vendor/xiaomi/.../hal/Timer.cpp
  fwd: forward-declared in 2 headers (use --verbose to list)
```

### `symbols` — dump the symbol inventory

```bash
python3 repoindex.py symbols [--root DIR | --db PATH]
                      [--kind K] [--pattern GLOB] [--path-prefix P]
                      [--out FILE | --out-dir DIR] [--max-per-file N]
                      [--no-guards] [--defs-only] [--dedup]
```

- No output option → prints everything to stdout (pipe-friendly).
- `--kind` one of `class struct enum union define var`.
- `--pattern '*Photo*'` glob on symbol names.
- `--path-prefix vendor/xiaomi/camera` one subtree only
  (combine with `--out`, not `--out-dir`).
- `--out FILE` one complete markdown file, **streamed** (flat memory at any
  size).
- `--out-dir DIR` **recommended**: writes per-component files and splits
  adaptively so no subtree exceeds `--max-per-file` symbols (default
  100,000). Each component is further split by kind:
  - `*.types.md` — class/struct/enum/union only; the small, LLM-readable API
    surface (~7% of tokens on AOSP-like trees),
  - `*.defines.md` — `#define` macros; grep-only reference,
  - `*.vars.md` — exposed globals,
  - `00-INDEX.md` — the map: every file with its symbol count.

Token-saving dump filters (the index itself always keeps everything):

- `--no-guards` — skip include-guard macros (`FOO_H` style, no value).
- `--defs-only` — skip forward declarations (`class Foo;`), keep definitions.
- `--dedup` — merge identical `name+kind+decl` rows into one row listing
  every exposing header (`--out`/console only; not with `--out-dir`).

### `headers` — CAMERA_HEADERS.md-style inventory

```bash
python3 repoindex.py headers ROOT [--subdir DIR]... [--pattern GLOB]...
                       [--recursive] [--title T] [--out FILE]
```

Reads files directly (no index needed). Per matched header, in alphabetical
order: a linked section with **Declarations (including forward
declarations)** and **Includes** code blocks. Sections with no content are
collapsed to a bare heading. Non-recursive by default (like the original
example); `--recursive` for whole subtrees.

```bash
python3 repoindex.py headers ~/references_code \
    --subdir vendor/xiaomi/camera/hal \
    --title "Xiaomi camera HAL header index" --out ~/references_code/CAMERA_HEADERS.md
```

### `agentsmd` — structural AGENTS.md scaffold

```bash
python3 repoindex.py agentsmd ROOT [--out FILE] [--title T]
                        [--depth N] [--reindex] [--ignore NAME]...
                        [--key-files] [--compact] [--also NAMES]
```

Generates from the index (auto-indexes if missing): full directory map with
recursive counts/sizes to `--depth` (default 2), complete file-type census,
header/symbol counts per top-level component, a **Which tool for which
case** decision table mapping situations to commands, ready-to-run search
recipes, SQL examples, and a **Token-efficient usage** section teaching
agents to grep/query instead of loading dumps. Human-knowledge sections (workspace
roles, verified observations) are marked with `<!-- TODO(curate) -->` slots.

`--key-files` additionally appends a Key files section (every `*.mk`,
`Android.bp`, `Makefile`, build config, ...). It is **off by default**:
build files matter to people who want to *build* the tree, while the
reference-search workflow targets variables, locations and definitions that
get matched against logs.

`--compact` writes a minimal variant for context-limited hosts (LM Studio
and other local models): index facts, the which-tool table and the token
rules only. `--also CLAUDE.md,GEMINI.md` writes the same guide to extra
filenames so platforms with their own instruction-file conventions pick it
up (paths resolve next to `--out`; subdirectories are created).

## Agent platform support

The generated guide follows the AGENTS.md convention that most coding
agents read natively; `--also` covers hosts with their own filenames, and
`--compact` covers small-context local models:

| Platform | How to use |
|---|---|
| ChatGPT Codex | `AGENTS.md` (default output) is read natively |
| Cursor, Aider, other AGENTS.md-compatible agents | same default `AGENTS.md` |
| Claude Code | `python3 repoindex.py agentsmd ROOT --out AGENTS.md --also CLAUDE.md` |
| Gemini CLI | `--also GEMINI.md` |
| GitHub Copilot | `--also .github/copilot-instructions.md` |
| LM Studio and other local/small-context models | generate with `--compact` and use it as the system prompt / preset instructions; keep the full AGENTS.md on disk for reference |
| Any agent | point it at `.repoindex.db` - one `lookup` call answers what a multi-megabyte file read would |

`--also` takes a comma-separated list, so one run can emit `AGENTS.md`,
`CLAUDE.md` and `.github/copilot-instructions.md` together.

### `search` — find files in the index

```bash
python3 repoindex.py search PATTERN [--root DIR | --db PATH]
                      [--ext .h] [--limit N]     # default: print all matches
```

`PATTERN` with `*`/`?` is a glob on the path (or file name); otherwise it's a
case-insensitive substring on the path.

### `tree` / `stats` / `dupes`

```bash
python3 repoindex.py tree  [--depth 2]           # dir map: recursive counts + sizes
python3 repoindex.py stats [--limit 20]          # totals, per-extension census, largest files
python3 repoindex.py dupes                       # size groups, then SHA-1 verified
```

---

## The database

Single file: `<ROOT>/.repoindex.db` (SQLite, WAL mode, compacted after each
build). Storage is normalized — paths live once in `dirs`, include targets
once in `inc_targets`:

| Table | Columns | Contents |
|---|---|---|
| `dirs` | `id, path` | every directory (`''` = root) |
| `files` | `dir_id, name, ext, size, mtime` | every file; `PRIMARY KEY (dir_id, name)`, `WITHOUT ROWID` |
| `symbols` | `name, kind, decl, dir_id, file` | every exposed symbol; `kind ∈ class struct enum union define var` |
| `inc_targets` | `id, target, system` | unique include targets |
| `includes` | `dir_id, file, target_id` | full include graph |
| `meta` | `key, value` | root path, build date, counts, schema version |

Query through the compatibility **views**, which reconstruct full paths:
`vfiles(path, name, ext, size, mtime)`, `vsymbols(name, kind, decl, path)`,
`vincludes(path, target, system)`.

```sql
-- which headers expose a symbol
SELECT name, kind, path FROM vsymbols WHERE name = 'CameraMode';

-- who includes a header
SELECT DISTINCT path FROM vincludes WHERE target = 'CameraMode.h';

-- files changed most recently
SELECT path, datetime(mtime,'unixepoch') AS modified
FROM vfiles ORDER BY mtime DESC LIMIT 50;

-- biggest headers in one component
SELECT path, size FROM vfiles
WHERE ext = '.h' AND path LIKE 'vendor/xiaomi/camera/%'
ORDER BY size DESC LIMIT 30;
```

Indexes exist on `symbols(name)`, `includes(target_id)`, `files(size)`,
`dirs(path)`. Rebuilding with a newer repoindex.py auto-detects an outdated
layout and replaces it.

## Token-efficient workflow for AI agents

The golden rules (also embedded in generated `AGENTS.md`):

1. **Never load whole `SYMBOLS/*.md` files into context.** Read
   `SYMBOLS/00-INDEX.md` (the tiny map), then grep exactly one relevant file,
   or query the index with `lookup` / SQL for just the rows you need.
2. **Read `.types.md` files** (the API surface); treat `.defines.md` and
   `.vars.md` as grep-only reference — on AOSP-like trees ~89% of symbols
   are macros.
3. If a dump must be LLM-read, generate it with `--no-guards --defs-only`
   or `--dedup` to cut token load further (18%+ on macro-heavy trees).
4. The SQLite index is the cheapest interface of all: one `lookup` call
   answers what a 12M-token file read would.

## Performance

Measured on this tool's test runs:

- 23,083 files / 1.5 GB indexed in **0.8 s** (8 threads), incl. parsing
  655 headers into 8,197 symbols + 3,709 include edges.
- Parallel scan verified **bit-identical** to single-threaded (exact set
  comparison of all tables).
- Compact layout: 8 MB → **2.7 MB** index for the same tree vs the original
  flat schema (bigger win on include-heavy trees).
- All dumps are streamed: memory stays flat regardless of tree size.

Expectations for multi-million-file trees: first run is disk-bound (threads
overlap the latency — biggest win on NVMe, warm page cache afterwards);
watch the `... N files` progress rate. Ignoring build outputs
(`--ignore out --ignore prebuilts`) saves more time than any thread count.

## What the header parser captures

Comment-aware (both `/* */` and `//` stripped before matching, so
documentation examples never pollute the index):

| Captured | Example row | Notes |
|---|---|---|
| classes | `class BokehPhotographer final : public Photographer` | cut at `{`; `final`, `CAMERA_API`-style macros skipped when finding the name |
| structs / unions | `struct HdrStatus` | same rules |
| enums | `enum HDRsupportedCameraMode { NORMAL_MODE, SDK_MODE };` | line kept whole |
| forward declarations | `class AlgoSession;` | kept (filterable via `--defs-only`) |
| macros | `#define VENDOR_FEATURE_FLAG 1` | first line of multi-line macros |
| exposed globals | `extern const char* const kSessionKeys[4];` | `extern/static/const/constexpr/volatile/inline` |

Deliberately skipped: function declarations, function pointers, typedefs,
local variables, anonymous enums/unions, and anything inside comments.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Wrote ... (0 symbols)` | `symbols`/`lookup` read the index; they don't scan. Run `index` first (see its final `Recorded N exposed symbols` line — N should be large). |
| `No index found at ...` | Same as above; read commands refuse to silently create an empty DB. |
| `old (fatter) layout` | Index was built by an earlier repoindex.py. Re-run `index` once with the current script — it rebuilds compact. |
| `Warning: N paths unreadable` | Permission-denied files were skipped; everything else was indexed. |
| Scan feels slow | Check the tree isn't under `/mnt/c`; add `--ignore out`/`--jobs`; first cold run is disk-bound by nature. |
| Out of disk during build | `VACUUM` temporarily needs ~1× the DB size of free space. |

## Limitations

- The parser is line-oriented, not a full C++ grammar: multi-line
  declarations are represented by their first line; `typedef`/`using`
  aliases and function signatures are out of scope (use `rg` for those).
- `lookup`'s "included by" matches include targets by file name, so
  same-named headers in different directories are grouped together.
- File contents are never hashed during indexing (that's what makes it
  fast); `dupes` hashes on demand instead.
- mtimes are stored at 1-second resolution.

## File layout produced on your tree

```
~/references_code/
├── .repoindex.db          # the index (single SQLite file)
├── AGENTS.md              # generated structural guide + token rules
├── CAMERA_HEADERS.md      # optional per-directory header inventory
└── SYMBOLS/
    ├── 00-INDEX.md        # the map: every dump file + symbol count
    ├── platform.types.md
    ├── platform.defines.md
    ├── vendor__xiaomi__camera.types.md
    └── ...                # adaptive split, <= --max-per-file symbols each
```
