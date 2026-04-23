# Architecture

A five-minute orientation for future-you. For the detailed "why"
behind individual decisions, see [docs/adr/](adr/).

`ai-session-capture` is organized around the **adapter pattern**: a
shared pipeline (parse → redact → render → index → serve) with
source-specific parsers at the front. The 0.1.0 release ships the
Claude Code adapter (the `claude_session_capture` Python package).
Future Codex / OpenCode adapters are sibling packages that reuse
everything downstream of `parser.py`.

## Pipeline

```
~/.claude/projects/*/*.jsonl        (input — raw Claude Code transcripts)
            │
            ▼
      parser.py            ← structural drops run here (sensitive tool
            │                 results are replaced with placeholders
            │                 BEFORE the regex layer ever sees them)
            ▼
   list[Record]            ← normalized, schema-stable data class
            │
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
 ~/.local/share/claude-sessions/YYYY-MM-DD.md   (output, private git repo)
```

## Module map

| Module | Responsibility | Lines |
|---|---|---|
| `parser.py` | Stream-parse JSONL, classify message types, apply structural drops | ~250 |
| `redact.py` | Regex redaction + prompt-injection neutralization + RedactionReport | ~170 |
| `render.py` | Group by session, apply content filters, emit Markdown via Jinja2 | ~230 |
| `config.py` | TOML → dataclass mapping with sensible defaults | ~110 |
| `state.py` | Atomic writes, flock, cursor-based idempotency, rotating log, desktop notify | ~190 |
| `cli.py` | argparse subcommands (daily, backfill, search, mcp-serve), orchestrates the others | ~280 |
| `search.py` | SQLite FTS5 index: upsert, query, rebuild, list helpers | ~260 |
| `mcp_server.py` | MCP stdio server — four read-only tools over the FTS index | ~180 |
| `templates/daily.md.j2` | Jinja2 template — YAML frontmatter, warning banner, per-session sections | ~60 |

Roughly 1,600 lines of Python. Intentionally small.

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
2. `~/.config/claude-session-capture/config.toml` (default)
3. Dataclass defaults (no file needed)

Unknown fields in the TOML are silently ignored so a typo can't wedge
a headless 06:00 run.

## Scheduling

Two templates, one installer:

- **macOS:** `~/Library/LaunchAgents/<reverse-dns-label>.claude-session-capture.plist`
  (launchd's calendar-based catch-up handles missed fires; label is
  configurable via the installer)
- **Linux:** `~/.config/systemd/user/claude-session-capture.{service,timer}`
  with `Persistent=true` (same semantics, different mechanism)

Both fire at 06:00 local time. `install.sh` detects the platform via
`uname -s` and installs the right one. The service invokes
`claude-session-capture daily`, which defaults to rendering "yesterday
in local TZ."

## XDG paths

All paths are XDG-standard and identical across platforms:

- Config: `~/.config/claude-session-capture/`
- State: `~/.local/state/claude-session-capture/` (cursor, lock, run.log, last-error)
- Output (data repo): `~/.local/share/claude-sessions/`

This keeps muscle memory and documentation consistent — no
"where was that file again" between machines.

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
                    claude-session-capture search
                    claude-session-capture mcp-serve
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
