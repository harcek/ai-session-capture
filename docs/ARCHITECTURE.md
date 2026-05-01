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
