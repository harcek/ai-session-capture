# ai-session-capture

> A private, redaction-first archive initially for Claude Code sessions,
> producing Markdown files and a local search/MCP index.
>
> Human-readable, git-friendly, and local-first by default.

A scheduled tool that parses Claude Code's own session transcripts
(`~/.claude/projects/*/*.jsonl`), redacts anything that looks like a
credential, and writes a human-readable Markdown archive plus a local
SQLite FTS5 search index. Future Claude Code sessions can query the
archive via an MCP server, turning the backfill into long-term memory
across sessions.

No network calls at runtime. No database server. No web surface. The
output is plain Markdown in a git repo you own; the index is a single
local SQLite file. Everything runs as the invoking user under XDG
paths.

## Who this is for

Anyone who uses Claude Code and wants a searchable, shareable, secret-
scrubbed record of their sessions — for reference, for standup notes,
for onboarding teammates to past decisions, or for querying from
future Claude sessions via MCP.

## What it does

- **Captures** — parses every JSONL under `~/.claude/projects/` once a
  day (or on demand) into normalized session records.
- **Redacts** — aggressive by default. Structural drops at parse time
  kill sensitive tool outputs (`env`, `cat .env`, reads of
  `.aws/`/`.ssh/`) before they leave the parser. Regex pass scrubs
  AWS / GitHub / OpenAI / Anthropic / Slack / Stripe / JWT / SSH key
  tokens, plus `.env`-style assignments with sensitive key names.
- **Renders** — one Markdown file per session under
  `sessions/<project>/<YYYY-MM-DD>_<HH-MM>_<id>[_<slug>].md`, plus a
  thin per-day index file under `daily/<YYYY-MM-DD>.md` with
  Obsidian-style wiki-links to the sessions that touched that day.
- **Indexes** — per-session rows in a local SQLite FTS5 table for
  millisecond-latency phrase / AND / OR / prefix search across years
  of archive.
- **Serves (optional)** — an MCP server exposes four read-only query
  tools (`search_sessions`, `list_projects`, `list_recent_sessions`,
  `get_session_text`) that Claude Code sessions can call directly.

## Paths (XDG-standard)

| What | Default path |
|---|---|
| Config | `~/.config/claude-session-capture/config.toml` |
| State (cursor, lockfile, run log, FTS DB) | `~/.local/state/claude-session-capture/` |
| Output archive (your data, a git repo) | `~/.local/share/claude-sessions/` |
| Input (read-only) | `~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR/projects`) |

All paths can be overridden — see [`config.toml.example`](config.toml.example)
and [`docs/adr/0004-derive-dont-configure-claude-root.md`](docs/adr/0004-derive-dont-configure-claude-root.md).

## Install

```sh
git clone <this repo>
cd claude-session-capture
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

## Usage

```sh
# One-off runs
claude-session-capture backfill                   # process all historical transcripts
claude-session-capture daily                      # render yesterday's sessions (what the scheduler does)
claude-session-capture --dry-run backfill         # render to stdout, touch nothing
claude-session-capture --show-redactions daily    # just the redaction count, no body

# Search the archive (SQLite FTS5 — phrase, AND/OR/NOT, prefix)
claude-session-capture search "rate limit"
claude-session-capture search "scanner" --project <your-project> --since 2026-04-01
claude-session-capture search "redact*" --limit 5 --format json
claude-session-capture search --rebuild           # drop and re-index everything

# One-off import from an archive / another machine
claude-session-capture --projects-root /path/to/other/.claude/projects backfill
```

## Wire into Claude Code as an MCP server

`mcp-serve` exposes the archive to future Claude Code sessions as
four read-only tools. Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "claude-sessions": {
      "command": "/absolute/path/to/.venv/bin/claude-session-capture",
      "args": ["mcp-serve"]
    }
  }
}
```

Use the full venv path so Claude Code doesn't need
`claude-session-capture` on `$PATH`. Restart Claude Code. In any new
session, ask Claude things like *"what did I decide about X last
month?"* and it will query the archive directly via MCP.

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
records in [`docs/adr/`](docs/adr/). Roughly 1,800 lines of Python
across seven modules, intentionally small:

```
parser.py      — stream JSONL, structural drops
redact.py      — regex redaction + prompt-injection neutralization
render.py      — per-session + daily-index Markdown via Jinja2
state.py       — lock, atomic writes, content-hash idempotency
search.py      — SQLite FTS5 index
cli.py         — daily / backfill / search / mcp-serve
mcp_server.py  — read-only MCP tools
```

## Roadmap

See [`BACKLOG.md`](BACKLOG.md) for the prioritized work list. The
natural next step is **source-aware ingestion across machines** —
unifying multiple `~/.claude/projects/` roots into one archive without
losing provenance. A later extension could add adapters for **Codex**
and **OpenCode**, covering the three dominant coding-agent
transcript formats.

## Development

```sh
.venv/bin/pytest tests/          # full suite
.venv/bin/ruff check .           # lint
```

Contribution guidance for humans and AI coding agents lives in
[`AGENTS.md`](AGENTS.md) (mirrored as `CLAUDE.md` for Claude Code).

## License

See [`LICENSE`](LICENSE).
