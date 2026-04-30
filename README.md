# ai-session-capture

> A private, redaction-first archive for AI coding-agent sessions
> (Claude Code, Codex, …), producing Markdown files and a local
> search/MCP index.
>
> Human-readable, git-friendly, and local-first by default.

A scheduled tool that parses transcripts from your AI coding agents —
Claude Code (`~/.claude/projects/*/*.jsonl`) and Codex
(`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`) today — redacts
anything that looks like a credential, and writes a human-readable
Markdown archive plus a local SQLite FTS5 search index. Future Claude
Code sessions can query the archive via an MCP server, turning the
backfill into long-term memory across sessions.

No network calls at runtime. No database server. No web surface. The
output is plain Markdown in a git repo you own; the index is a single
local SQLite file. Everything runs as the invoking user under XDG
paths.

## Who this is for

Anyone who uses Claude Code or Codex and wants a searchable,
shareable, secret-scrubbed record of their sessions — for reference,
for standup notes, for onboarding teammates to past decisions, or for
querying from future agent sessions via MCP.

## What it does

- **Captures** — parses every JSONL under `~/.claude/projects/` and
  `~/.codex/sessions/` once a day (or on demand) into normalized
  session records. Absent agent dirs are skipped silently — install
  one agent, both, or neither.
- **Redacts** — aggressive by default. Structural drops at parse time
  kill sensitive tool outputs (`env`, `cat .env`, reads of
  `.aws/`/`.ssh/`) before they leave the parser. Regex pass scrubs
  AWS / GitHub / OpenAI / Anthropic / Slack / Stripe / JWT / SSH key
  tokens, plus `.env`-style assignments with sensitive key names.
- **Renders** — one Markdown file per session under
  `sessions/<source>/<project>/<YYYY-MM-DD>_<HH-MM>_<id>[_<slug>].md`,
  where `<source>` is `claude` or `codex`. Plus a thin per-day index
  file under `daily/<YYYY-MM-DD>.md` with Obsidian-style wiki-links
  to the sessions that touched that day, across all sources.
- **Indexes** — per-session rows in a local SQLite FTS5 table for
  millisecond-latency phrase / AND / OR / prefix search across years
  of archive. The `source` column lets you scope to a single agent.
- **Serves (optional)** — an MCP server exposes four read-only query
  tools (`search_sessions`, `list_projects`, `list_recent_sessions`,
  `get_session_text`) that Claude Code sessions can call directly,
  with optional `source` filter.

## Paths (XDG-standard)

| What | Default path |
|---|---|
| Config | `~/.config/ai-session-capture/config.toml` |
| State (cursor, lockfile, run log, FTS DB) | `~/.local/state/ai-session-capture/` |
| Output archive (your data, a git repo) | `~/.local/share/ai-sessions/` |
| Input (read-only) — Claude | `~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR/projects`) |
| Input (read-only) — Codex | `~/.codex/sessions/` (or `$CODEX_SESSIONS_ROOT`) |

All paths can be overridden — see [`config.toml.example`](config.toml.example)
and [`docs/adr/0004-derive-dont-configure-claude-root.md`](docs/adr/0004-derive-dont-configure-claude-root.md).

## Install

```sh
git clone <this repo>
cd ai-session-capture
python3 -m venv .venv
.venv/bin/pip install -e '.[mcp]'          # or omit [mcp] if you don't need the MCP server
.venv/bin/pytest tests/                    # expect all green
./scripts/install.sh                       # register launchd (macOS) or systemd-timer (Linux)
```

`install.sh` auto-detects macOS vs Linux and installs the appropriate
scheduling units. On Linux it attempts `loginctl enable-linger` so the
timer fires even when you're not logged in.

The scheduler fires once a day, early morning local time (default
06:00), and catches up exactly once on wake / boot if the machine was
asleep at the trigger.

### Upgrading from 0.1.x

The 0.2.0 rename moves XDG dirs in place on first run (config / state /
default data dir). Two things still need a manual touch:

1. `./scripts/install.sh` to register the new-named scheduling units
   (the old `claude-session-capture.daily.*` units can be removed via
   `./scripts/uninstall.sh` once you've confirmed the new ones run).
2. If you wired the MCP server into Claude Code, update the
   `mcpServers.<name>.command` field in `~/.claude/settings.json` to
   point at `ai-session-capture` (see the snippet below).

## Usage

```sh
# One-off runs
ai-session-capture backfill                   # process all historical transcripts (every source)
ai-session-capture daily                      # render yesterday's sessions (what the scheduler does)
ai-session-capture backfill --source codex    # only Codex transcripts
ai-session-capture --dry-run backfill         # render to stdout, touch nothing
ai-session-capture --show-redactions daily    # just the redaction count, no body

# Search the archive (SQLite FTS5 — phrase, AND/OR/NOT, prefix)
ai-session-capture search "rate limit"
ai-session-capture search "scanner" --project <your-project> --since 2026-04-01
ai-session-capture search "redact*" --source codex --limit 5 --format json
ai-session-capture search --rebuild           # drop and re-index everything

# One-off import from an archive / another machine
ai-session-capture --projects-root /path/to/other/.claude/projects backfill
```

## Wire into Claude Code as an MCP server

`mcp-serve` exposes the archive to future Claude Code sessions as
four read-only tools. Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "ai-sessions": {
      "command": "/absolute/path/to/.venv/bin/ai-session-capture",
      "args": ["mcp-serve"]
    }
  }
}
```

Use the full venv path so Claude Code doesn't need
`ai-session-capture` on `$PATH`. Restart Claude Code. In any new
session, ask Claude things like *"what did I decide about X last
month?"* (optionally scoped to a source) and it will query the archive
directly via MCP.

## Security posture

Aggressive redaction is the default and is built into the project from
the ground up, not bolted on. Read [`SECURITY.md`](SECURITY.md) for
the threat model, redaction ordering, what we defend against, and —
just as important — what we explicitly do **not**. The one-line
version:

- Structural drops in the parser kill sensitive tool output at the
  source.
- Regex redaction scrubs provider tokens before any rendering.
- Every user-facing surface (body, filename, frontmatter, title, cwd)
  passes through redaction.
- A prominent warning block fires at the top of any output file where
  redaction caught something, nudging the reader to rotate the
  exposed credential and fix the habit that leaked it.

## Configuration

See [`config.toml.example`](config.toml.example) for every live knob.
Two guiding principles:

1. **Either implement and test, or remove.** Every documented config
   field has a test that would fail if it were ignored.
2. **Deriving beats configuring.** For example, the Claude transcripts
   root is derived from Claude Code's own canonicals rather than being
   a TOML field — see [`docs/adr/0004-derive-dont-configure-claude-root.md`](docs/adr/0004-derive-dont-configure-claude-root.md).

## Architecture

Short map in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Decision
records in [`docs/adr/`](docs/adr/). The package is organized around
the **adapter pattern** — a shared pipeline with source-specific
parsers at the front:

```
parser.py        — Claude Code JSONL stream + structural drops
codex_parser.py  — Codex rollout JSONL stream + structural drops
redact.py        — regex redaction + prompt-injection neutralization
render.py        — per-session + daily-index Markdown via Jinja2
state.py         — lock, atomic writes, content-hash idempotency
search.py        — SQLite FTS5 index (with source dimension)
cli.py           — daily / backfill / search / mcp-serve (with --source)
mcp_server.py    — read-only MCP tools (with source filter)
```

Adapters share everything downstream of the parser. New agents land
as new sibling parsers — no changes to redact / render / state /
search / mcp_server.

## Roadmap

See [`BACKLOG.md`](BACKLOG.md) for the prioritized work list. Next-up
items: **OpenCode adapter** (third agent, mechanically similar to the
Codex one) and **source-aware ingestion across machines** (multi-host
unified archive without losing provenance).

## Development

```sh
.venv/bin/pytest tests/          # full suite
.venv/bin/ruff check .           # lint
```

Contribution guidance for humans and AI coding agents lives in
[`AGENTS.md`](AGENTS.md) (mirrored as `CLAUDE.md` for Claude Code).

## License

See [`LICENSE`](LICENSE).
