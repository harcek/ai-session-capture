# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
version numbers are semver-ish (0.x while the layout settles).

## [0.3.0] — 2026-04-30

Multi-machine ingestion. Sessions now carry a `machine` discriminator
alongside `source`, the output layout grows a per-machine segment,
and the FTS index can be rebuilt by walking session MDs on disk —
the foundation for "one archive across MBP + Mac mini + Ubuntu, with
search that sees every machine's captures from any one of them." See
[`docs/adr/0006-multi-machine-ingestion.md`](docs/adr/0006-multi-machine-ingestion.md)
for the design.

### Added

- **`[machine]` config section** with optional `name` field. Empty →
  sanitized `socket.gethostname()` (lowercase, strip trailing
  `.local`, normalize to `[a-z0-9_-]`). Resolved once at run start
  via `state.resolve_machine_name(cfg)`.
- **`Record.machine` field** stamped onto every parsed record by the
  CLI. Both adapters (Claude Code + Codex) inherit the value.
- **`machine` column on the FTS index** (`sessions` table +
  `sessions_fts` virtual table). Composite PK is now
  `(id, date, source, machine)`. Schema migration runs on first
  v0.3.0 start; legacy v0.2.0 DBs (PK includes source but not
  machine) are rebuilt preserving rows.
- **`--machine` CLI flag** on `daily`, `backfill`, and `search`.
  Passthrough on `search`; informational no-op on ingest paths
  (only the local machine's JSONL is on this filesystem) — passing
  a non-current value logs a warning.
- **`machine` parameter on all four MCP tools**
  (`search_sessions`, `list_projects`, `list_recent_sessions`,
  `get_session_text`). Result rows expose `machine` alongside
  `source` so the LLM can post-filter or attribute hits.
- **`search --rebuild` walks the data dir's session MDs** rather
  than re-running the JSONL pipeline. New helpers
  `parse_session_md(text)` and `index_row_from_md(path)` parse
  YAML frontmatter into `SessionIndexRow`. After `git pull` brings
  in another machine's MDs, `--rebuild` indexes them — no JSONL
  needed.
- **`migrate-machine OLD NEW` subcommand** for renaming a machine
  in place across paths, frontmatter, and FTS. Useful when source
  JSONLs may have been pruned (Claude Code / Codex retention,
  manual cleanup, disk pressure) — wipe-and-rebackfill would lose
  those sessions, so we rewrite frontmatter, move the per-machine
  subtree, and update FTS rows in lockstep instead. `--dry-run`
  reports what would change. Refuses to merge if the target
  subtree already exists.
- **Three-layer architecture documented** in `docs/ARCHITECTURE.md`
  with a multi-machine sync section explaining which layers
  travel between hosts (Layer 2 only) and when to rebuild Layer 3
  (after every external change to Layer 2). Naming the layers
  (sources / MDs / FTS) makes "which layer does this feature
  operate on?" the first question for future contributors.
- **Lesson-capture process rule** added to `AGENTS.md`. After
  every turn producing a real lesson, append to
  `untracked/LESSONS.md` using the existing
  What-happened / Root-cause / Prevention structure. Cross-linked
  from `untracked/LESSONS.md`'s own header.

### Changed

- **Output layout**: `sessions/<machine>/<source>/<project>/<file>.md`
  (was `sessions/<source>/<project>/`). Per-machine subtrees never
  collide on git merge.
- **Daily index path**: `daily/<machine>/<date>.md` (was
  `daily/<date>.md`). Cross-machine "what happened on day X" comes
  via FTS; the daily MD is per-machine.
- **Daily index wiki-links** now use `../../sessions/<machine>/...`
  (one extra `../` to escape the per-machine daily subtree).
- **Session frontmatter** gains `machine: <name>` and a
  `machine/<name>` tag.
- **First v0.3.0 run migrates a v0.2.0 archive in place**
  (`state.migrate_archive_to_per_machine`): legacy
  `sessions/<source>/` and flat `daily/<date>.md` files move under
  `sessions/<this-machine>/<source>/` and
  `daily/<this-machine>/`. Idempotent; custom `output.dir` values
  receive the same treatment.
- **Default data dir renamed for symmetry**: was
  `~/.local/share/ai-sessions/`, now
  `~/.local/share/ai-session-capture/` so the data dir lines up
  with `~/.config/ai-session-capture/` and
  `~/.local/state/ai-session-capture/`. The `migrate_data_dir`
  shim chains through legacy → new in one rename: a v0.1.x or
  v0.2.0 user with `claude-sessions` / `ai-sessions` lands
  directly at `ai-session-capture` on first v0.3.x start. Custom
  `output.dir` values are left alone.
- **Search CLI text output** now reads `<date> · <machine>/<source>
  · <project> · <id>` (was `<date> · <source> · <project> · <id>`).

### Out of scope for 0.3.0

- **Auto-push to a shared remote** (BACKLOG #C) — the multi-machine
  semantics are in place but each machine's archive stays local
  until #C wires git push/pull.
- **OpenCode adapter** (BACKLOG #A) — same v0.2.0 pattern; lands as
  v0.4.0.

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
