# Backlog — ai-session-capture

Prioritized work items. Pick from the top when you have time; every
item is independently shippable.

## Status key

- **🔜 Next-up** — picked for the next work session
- **◇ Backlog** — planned, not scheduled
- **◇◇ Maybe** — worth writing down; may never ship

See also [`docs/DEFERRED.md`](docs/DEFERRED.md) for review findings
consciously *not* acted on, with reasons. Move items from DEFERRED →
here when a concrete motivating use case arrives.

---

## 🔜 Next-up

### #A — Source-aware ingestion across machines

Support multiple Claude transcript roots (e.g. each of your machines
contributes its own `~/.claude/projects/`) as named sources, with
provenance preserved in the archive. This is the natural next step
after local-first is stable: a unified archive without losing which
machine a session came from.

Rough shape (see `docs/adr/0004-...` for the precedence + why the
single-root design stops here):

```toml
[[sources]]
name = "laptop"
root = "~/Archives/laptop-claude-projects"

[[sources]]
name = "workstation"
root = "auto"   # the current machine's default
```

Each session's frontmatter gains a `source` field; FTS index gets a
`source` column; `--source <name>` filter on the CLI. Session IDs
across sources are disambiguated via `(source, session_id)`.

- Why core: eliminates the "which machine's archive wins?" problem
  for multi-machine workflows.
- Depends on nothing shipped; internal-only refactor.
- Estimated effort: M (1 day).

### #B — Private remote + auto-push

Wire a private git remote for the data repo; daily runs optionally
commit and push after writing. Sync approach is a prerequisite
decision — evaluate Syncthing vs. own SSH hub vs. private GitHub
based on the operator's threat model and available infrastructure
before wiring the push logic.

---

## ◇ Backlog

### Codex transcript adapter

Codex (and its CLI incarnations) stores session data in a different
shape than Claude Code. Add an adapter that normalizes Codex
transcripts into the same internal `Record` type so they land in the
same archive with a `source=codex` tag. Lives as a sibling module
(`codex_parser.py`) alongside the Claude parser — no changes to the
existing parser.

- Depends on: #A (source-aware ingestion).
- Estimated effort: M (1–2 days including format research).

### OpenCode transcript adapter

Same pattern for OpenCode. Different enough shape that it needs its
own adapter; common enough use case that one tool covering all three
(Claude Code + Codex + OpenCode) is a real differentiator.

- Depends on: #A.
- Estimated effort: M (similar to Codex adapter).

### Obsidian compatibility polish

Wiki-link rewriting for project/file mentions (`[[project]]`), tag
prefix config (`claude/` + `project/`), richer Dataview-friendly
frontmatter (`type`, `tokens`, `cost` if available, `branch`,
`files_changed`). Phase 1 frontmatter is already Obsidian-compatible;
this is polish — worth doing only once a reader actively uses
Obsidian as their consumption surface.

### `age` encryption opt-in

`[security].encrypt_output = true` writes `YYYY-MM-DD.md.age` to
recipient public keys instead of plaintext MDs. Defers until the data
repo hits a risky remote (public repo leak, provider admin exposure);
local-only posture doesn't need it.

### Failure notification webhook

Phase 1 has desktop notify + `last-error` sentinel. Phase 2 adds an
optional webhook (email / Slack / ntfy.sh) for machines where
desktop notifications don't reach a human (headless boxes, Mac mini
without console).

### Security hardening round 2

- Canary tokens seeded in a fixture JSONL that the test suite expects
  to catch (regression guard for redaction regex edits).
- Post-commit `gitleaks` / `trufflehog` sweep on the data repo.
- Symlink / TOCTOU audit pass on `state.py` writes.
- Pre-push hook on the data repo once the remote is wired.

---

## ◇◇ Maybe

### Weekly LLM digest

Monday 07:00 cron: summarize last week's sessions into
`weekly/YYYY-WW.md`. Standup material. Revisit once there's ≥4 weeks
of data to meaningfully summarize.

### Phase 3 possibilities

- Local embeddings + vector search (chromadb or sqlite-vec).
- Per-project tag extraction, decision tracking.
- Export to other tools (Notion, Logseq).
- Auto-linking between related sessions across time.

---

## Completed (for context)

- **Per-session + daily-index layout (ADR-0003)** — replaced the v1
  "one file per day, all sessions mixed" layout. One file per session
  UUID (cross-day → one file pinned to start date), thin per-day
  index with wiki-links.
- **Derive-don't-configure Claude root (ADR-0004)** — removed
  `[scope].projects_root`; derive from Claude Code canonicals +
  `--projects-root` CLI flag + honoring `CLAUDE_CONFIG_DIR`.
- **SQLite FTS5 search index** — `search` CLI subcommand with
  `--rebuild`, integrated into `daily`/`backfill`. Index at
  `~/.local/state/claude-session-capture/index.db`.
- **MCP server** — `mcp-serve`, four read-only tools
  (`search_sessions`, `list_projects`, `list_recent_sessions`,
  `get_session_text`). Optional `[mcp]` extra.
- **Phase 1 (0.1.0)** — parse → redact → render → state → CLI →
  scheduling templates → data-repo init. First full end-to-end pipe.
