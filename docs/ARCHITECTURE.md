# Architecture

A five-minute orientation for future-you. For the detailed "why"
behind individual decisions, see [docs/adr/](adr/).

`ai-session-capture` is organized around the **adapter pattern**: a
shared pipeline (parse → redact → render → index → serve) with
source-specific parsers at the front. The package
(`src/ai_session_capture/`) ships two adapters today —
`parser.py` (Claude Code) and `codex_parser.py` (Codex) — both
producing the same `Record` type. Future adapters (OpenCode, …) are
sibling parser modules that reuse everything downstream of the
parser layer.

Sessions are partitioned by `(machine, source)` end-to-end: file
layout, FTS rows, MCP results. The machine identity comes from
`[machine].name` in config (or sanitized hostname); see
[ADR-0006](adr/0006-multi-machine-ingestion.md). This makes the
data repo safely shareable across multiple hosts — each machine
writes only under its own subtree.

## The three layers

The pipeline factors cleanly into three layers, each derivable
from the layer above. Knowing which layer a feature operates on
is the first question to ask when extending the tool:

```
┌──────────────────────────────────────────────────────────┐
│ LAYER 1 — sources (raw, read-only, owned by AI tools)    │
│   ~/.claude/projects/*/*.jsonl       (Claude Code)       │
│   ~/.codex/sessions/YYYY/MM/DD/*.jsonl  (Codex)          │
│                                                          │
│   We never write here. Path-traversal-guarded reads      │
│   only.                                                  │
└──────────────────────────────────────────────────────────┘
           │ parse + redact + render   (cli: backfill / daily)
           ▼
┌──────────────────────────────────────────────────────────┐
│ LAYER 2 — rendered MDs (derived; the canonical archive)  │
│   ~/.local/share/ai-session-capture/                     │
│     sessions/<machine>/<source>/<project>/*.md           │
│     daily/<machine>/<date>.md                            │
│                                                          │
│   Human-readable, Obsidian-friendly, redaction baked in. │
│   YAML frontmatter is the contract — every field needed  │
│   to reconstruct Layer 3 is there.                       │
│   This is the layer that travels between machines.       │
└──────────────────────────────────────────────────────────┘
           │ index from Layer 2     (cli: search --rebuild)
           │ index from records     (auto, during backfill/daily)
           ▼
┌──────────────────────────────────────────────────────────┐
│ LAYER 3 — FTS5 search index (derived; local-only cache)  │
│   ~/.local/state/ai-session-capture/index.db             │
│     sessions table   (one row per session per date)      │
│     sessions_fts     (FTS5 virtual table over content)   │
│                                                          │
│   Pure cache. Fully reconstructible from Layer 2 alone.  │
│   `search` and the four MCP tools query *only* this      │
│   layer — no MD scans, no JSONL re-parses at query time. │
└──────────────────────────────────────────────────────────┘
```

**Two ways to populate Layer 3:**
- During `backfill`/`daily` ingest, Layer 1 → Layer 2 + Layer 3 in
  one sweep (records flow into both writes simultaneously).
- Via `search --rebuild`, Layer 2 → Layer 3 only — walks
  `sessions/**/*.md`, parses YAML frontmatter via
  `parse_session_md`, builds `SessionIndexRow` per file. This is
  the path that lets a machine see another machine's captures
  without needing their JSONLs.

**Layer derivability is the design contract.** Every feature
should declare which layer it operates on. A feature that secretly
needs Layer 1 information to operate on Layer 2 breaks the story
(and breaks multi-machine sync, since other machines won't have
the JSONLs). The `parse_session_md` helper exists precisely to
keep this contract: anything Layer 3 needs must land in
frontmatter on Layer 2, not get re-derived from Layer 1.

## Multi-machine sync model

The three-layer split is what makes multi-machine workflows work
without special sync-aware code:

| Layer | Sync between machines? | Recipe |
|---|---|---|
| Layer 1 (JSONLs) | **No** — each machine has its own AI tools, its own JSONLs. Not shareable; the AI tools own them. |
| Layer 2 (MDs) | **Yes** — `rsync --exclude='.git/'`, or once #A ships, `git push/pull`. Per-machine subtrees never collide. |
| Layer 3 (FTS) | **No** — it's a cache. Each machine maintains its own; rebuild from Layer 2 after every sync. |

The canonical consumer-side flow on a machine that just received
fresh Layer-2 data from another host:

```sh
# 1. Pull Layer 2 (rsync today, git pull once #A ships)
asc-pull-pi    # alias for the rsync command

# 2. Reindex Layer 3 from the now-updated Layer 2
ai-session-capture search --rebuild

# 3. Now search sees the union of all machines whose data is here
ai-session-capture search "rate limit"                  # all machines
ai-session-capture search "design" --machine raspberry  # one machine
```

Folding step 2 into the alias is the right move — there's no
scenario where you'd want to pull Layer 2 but not refresh Layer 3.
Cost is sub-second for hundreds of MDs.

**When to rebuild Layer 3:**

| Situation | Rebuild needed? |
|---|---|
| `backfill` / `daily` / `migrate-machine` | **Auto** — the command updates Layer 3 in lockstep. |
| Rsync, `git pull`, manual MD edit | **Manual** — `search --rebuild` walks Layer 2 anew. |
| Redaction regex changed | **No** — backfill instead. Layer 2 itself needs re-rendering. |
| Schema migration / DB corruption | **Manual** — `search --rebuild` is also the disaster-recovery path. |

In one sentence: rebuild whenever Layer 2 changed in a way Layer 3
doesn't already reflect. The tool's own commands keep the layers
consistent automatically; anything that arrives from outside the
tool's pipeline triggers a manual rebuild.

## Pipeline

```
~/.claude/projects/*/*.jsonl        (Claude Code transcripts)
~/.codex/sessions/YYYY/MM/DD/*.jsonl (Codex rollouts)
            │
            ▼
  parser.py / codex_parser.py
                              ← structural drops run here (sensitive tool
            │                 results are replaced with placeholders
            │                 BEFORE the regex layer ever sees them)
            ▼
   list[Record]            ← normalized, schema-stable data class with
            │                 a `source` discriminator (claude|codex|…)
            ▼
      render.py            ← redact.py is invoked inline on every piece
            │                 of user-facing text; a shared RedactionReport
            │                 threads through so the top-of-doc warning
            │                 banner reflects the full day
            ▼
  markdown string
            │
            ▼
      state.py             ← content-hash gate against cursor.json
            │                 (skip write if unchanged), atomic
            │                 tmp+rename, 0o600 mode, flock-guarded
            ▼
 ~/.local/share/ai-session-capture/sessions/<machine>/<source>/<project>/<file>.md
                                       (output, private git repo)
```

## Module map

| Module | Responsibility |
|---|---|
| `parser.py` | Claude Code: stream-parse JSONL, classify message types, apply structural drops |
| `codex_parser.py` | Codex: stream-parse rollout JSONL, dispatch on `(rtype, ptype)`, apply same structural drops |
| `redact.py` | Regex redaction + prompt-injection neutralization + RedactionReport |
| `render.py` | Group by session, apply content filters, emit Markdown via Jinja2 |
| `config.py` | TOML → dataclass mapping with sensible defaults |
| `state.py` | Atomic writes, flock, cursor-based idempotency, rotating log, desktop notify, XDG-path migration |
| `cli.py` | argparse subcommands (daily, backfill, search, mcp-serve) with `--source`, orchestrates the others |
| `search.py` | SQLite FTS5 index (source-aware): upsert, query, rebuild, list helpers |
| `mcp_server.py` | MCP stdio server — four read-only tools over the FTS index, with `source` filter |
| `templates/*.j2` | Jinja2 templates — session.md.j2 + daily_index.md.j2 |

Intentionally small. Adapters share everything to the right of the
parser column.

## Data flow invariants

Invariants that hold across the whole pipeline — if you break one,
something downstream breaks:

1. **Records are chronologically sortable** by `(timestamp, uuid)`.
   The render layer relies on this for deterministic output.
2. **`RedactionReport` is write-once per day.** Same day rendered twice
   with identical input produces identical reports.
3. **No plaintext secrets in `RedactionReport`.** The counts are labels
   + integers. The data structure never holds what was matched — that
   would defeat the whole point.
4. **The renderer is pure.** Given the same `(records, date, config, tz)`
   it produces identical bytes. This is what `state.py`'s content-hash
   idempotency gate depends on.
5. **Sensitive content never leaves `parser.py`.** Structural drops
   blank `tool_result.content` at the source. Downstream code only ever
   sees `""` where the secret was, even if someone adds a new debug log.

## Config resolution order

1. `--config <path>` CLI flag (explicit)
2. `~/.config/ai-session-capture/config.toml` (default)
3. Dataclass defaults (no file needed)

Unknown fields in the TOML are silently ignored so a typo can't wedge
a headless 06:00 run.

## Scheduling

Two templates, one installer:

- **macOS:** `~/Library/LaunchAgents/ai-session-capture.daily.plist`
  (launchd's calendar-based catch-up handles missed fires; label is
  configurable via `LABEL=...` in the installer environment)
- **Linux:** `~/.config/systemd/user/ai-session-capture.{service,timer}`
  with `Persistent=true` (same semantics, different mechanism)

Both fire at 06:00 local time. `install.sh` detects the platform via
`uname -s` and installs the right one. The service invokes
`ai-session-capture daily`, which defaults to rendering "yesterday
in local TZ."

## XDG paths

All paths are XDG-standard and identical across platforms:

- Config: `~/.config/ai-session-capture/`
- State: `~/.local/state/ai-session-capture/` (cursor, lock, run.log, last-error, index.db)
- Output (data repo): `~/.local/share/ai-session-capture/`

These rename from `claude-session-capture` / `claude-sessions` in
v0.2.0; first run after upgrade migrates the existing dirs in place
(see `state.state_dir` and `state.migrate_data_dir`). This keeps
muscle memory and documentation consistent — no "where was that file
again" between machines.

## Search + MCP sit on top of the Phase 1 pipeline

```
                     daily / backfill
                           │
                  ┌────────┴────────┐
                  ▼                 ▼
           MD on disk        sessions_fts
           (data repo)       (state DB)
                                   ▲
                                   │
                    ai-session-capture search
                    ai-session-capture mcp-serve
                                   ▲
                                   │
                           Claude Code sessions
                           (via MCP stdio)
```

`search.py` consumes the same `Record` list that the renderer produces,
applies the same redaction + content filters, and indexes one row per
`(session_id, local-date)`. The MCP server calls exclusively into
`search.py` helpers — it has no parser/JSONL dependency — so it boots
instantly and is read-only by construction.

## What's intentionally NOT here

- **No embeddings.** Phase 3, if at all. FTS5 covers keyword/phrase
  search; embeddings add semantic search (and a much bigger dep
  footprint). Defer until usage proves it's needed.
- **No streaming / incremental updates of the FTS index.** A full
  refresh is cheap: unchanged sessions skip via content-hash, and
  re-parsing every JSONL takes < 1 second per year of data.
- **No web UI.** `grep` → FTS CLI → MCP server covers every use case
  I've wanted so far.
