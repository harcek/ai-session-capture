# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
version numbers are semver-ish (0.x while the layout settles).

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
