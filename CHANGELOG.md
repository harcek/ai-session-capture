# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
version numbers are semver-ish (0.x while the layout settles).

## [0.2.0] — 2026-04-30

Multi-source ingestion arrives: alongside the Claude Code adapter,
this release ships a **Codex adapter** for OpenAI's Codex CLI rollout
JSONL transcripts. To match the broader product framing, the binary
and Python package are renamed (was `claude-session-capture`).
See [`docs/adr/0005-multi-source-codex-adapter.md`](docs/adr/0005-multi-source-codex-adapter.md)
for the design.

### Added

- **Codex adapter** (`ai_session_capture.codex_parser`) — reads
  `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, normalizes into
  the same `Record` type used by Claude. Same redaction, render,
  search, and MCP layers — adapters share everything downstream of
  the parser.
- **`--source claude|codex|all`** CLI flag on `daily`, `backfill`,
  and `search`. Default is `all`; absent source dirs are silently
  skipped, so a Claude-only or Codex-only user sees nothing change
  by default.
- **`source` filter** on the MCP tools `search_sessions`,
  `list_projects`, `list_recent_sessions`, `get_session_text`.
- **`source` column** on the FTS index (`sessions` table +
  `sessions_fts` virtual table). Schema migration runs on first v0.2.0
  start; legacy `(id, date)`-keyed DBs are rebuilt with the new
  `(id, date, source)` PK preserving rows.

### Changed

- **Package + CLI rename:** `claude-session-capture` →
  `ai-session-capture` (Python package `claude_session_capture` →
  `ai_session_capture`). Forward-looking name for the
  multi-adapter shape that now exists in code, not just intent.
- **XDG paths rename**, with one-shot in-place migration on first
  v0.2.0 run:
  - Config: `~/.config/claude-session-capture/` →
    `~/.config/ai-session-capture/`
  - State: `~/.local/state/claude-session-capture/` →
    `~/.local/state/ai-session-capture/`
  - Data (default): `~/.local/share/claude-sessions/` →
    `~/.local/share/ai-sessions/` (only migrated when the user is
    on the default `output.dir`; custom dirs are left alone).
- **Output layout** is now `sessions/<source>/<project>/<file>.md`
  (was `sessions/<project>/<file>.md`). Project names that collide
  across sources (same `cwd` used with both agents) are
  disambiguated by the `source` segment.
- **Scheduling unit + label rename:** launchd label and systemd unit
  names are now `ai-session-capture.daily` /
  `ai-session-capture.{service,timer}`. Re-run
  `./scripts/install.sh` after upgrade.
- **MCP `mcpServers` entry:** the `command` in
  `~/.claude/settings.json` must point at `ai-session-capture`
  (one-line edit per machine).

### Fixed

- Session frontmatter now reflects the source: `source: <name>`
  field plus a `<source>-session` tag (was hardcoded
  `claude-session` for every adapter).
- Codex user records that contain only system-priming blocks
  (every `input_text` filtered as priming, no real content) are no
  longer emitted as empty `Q` turns.

### Security

- **Codex transcripts** receive the same structural-drop + regex
  redaction treatment as Claude. The shared `SENSITIVE_BASH` /
  `SENSITIVE_PATH` matchers gate Codex's `shell` calls and Codex's
  `Read`-equivalent file accesses identically.

## [0.1.0] — 2026-04-23

Initial public release of **ai-session-capture**; this release ships
the **Claude Code adapter** (`claude_session_capture` Python package).
Future Codex and OpenCode adapters will land as sibling packages —
see [`BACKLOG.md`](BACKLOG.md).

### Added

- Per-session Markdown output under `sessions/<project>/` plus a
  per-day index under `daily/` with Obsidian-style backlinks
  between the two.
- SQLite FTS5 search index with a `search` CLI subcommand (phrase,
  AND/OR/NOT, prefix queries; `--project`, `--since`, `--until`,
  `--format text|json`, `--rebuild`).
- MCP stdio server (`mcp-serve`) exposing four read-only tools:
  `search_sessions`, `list_projects`, `list_recent_sessions`,
  `get_session_text`.
- Cross-platform scheduling: `install.sh` auto-detects macOS
  (launchd) or Linux (systemd user timer); fires once daily with
  catch-up-on-wake semantics.
- TOML configuration with project aliases, granularity control
  (`session` / `session+daily`), sidechain filtering, tool-call
  display mode.
- `--projects-root PATH` CLI flag for one-off imports from archived
  transcripts.

### Security

- Aggressive redaction by default: AWS, GitHub (four PAT variants),
  Anthropic, OpenAI, Slack, Google, Stripe, JWT, SSH/PEM private
  keys, database URLs with embedded credentials, and `.env`-style
  assignments with sensitive key names. Matches replaced with
  `[REDACTED:LABEL:hash6]`.
- Structural drops at parse time for `env`, `.env` reads, and
  credential-file reads — sensitive content never leaves the
  parser.
- Content-hash idempotency, atomic writes, umask `0o077`,
  directories `0o700`, files `0o600`, `O_NOFOLLOW` on reads.
- Prompt-injection neutralization (zero-width + bidi-override chars
  stripped before rendering).
- Every user-facing surface (body, filename, frontmatter, title,
  cwd) passes through redaction before reaching disk.

### Licensing

MIT.
