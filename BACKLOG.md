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

### #A — OpenCode transcript adapter

Same pattern as the Codex adapter shipped in v0.2.0: a sibling parser
module (`opencode_parser.py`) producing `Record(source="opencode")`
instances, reusing the shared redact / render / state / search /
mcp_server pipeline. Different transcript shape; mechanical to add
once the format is reverse-engineered.

- Depends on: nothing shipped. Reuses ADR-0005's adapter pattern.
- Estimated effort: M (1–2 days including format research).

### #B — Multi-machine ingestion

Support multiple roots per source (e.g. each of your machines
contributes its own `~/.claude/projects/` and `~/.codex/sessions/`),
with provenance preserved in the archive. v0.2.0 adds the `source`
discriminator (per-tool); this adds a `machine` discriminator
alongside it. Useful for unified archives across a laptop +
workstation + Mac mini setup.

Rough shape:

```toml
[[machines.claude]]
name = "laptop"
root = "~/Archives/laptop-claude-projects"

[[machines.claude]]
name = "workstation"
root = "auto"   # the current machine's default
```

- Depends on: nothing shipped.
- Estimated effort: M (1 day).

### #C — Private remote + auto-push

Wire a private git remote for the data repo; daily runs optionally
commit and push after writing. Sync approach is a prerequisite
decision — evaluate Syncthing vs. own SSH hub vs. private GitHub
based on the operator's threat model and available infrastructure
before wiring the push logic.

---

## ◇ Backlog

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

- **Multi-source + Codex adapter + rename (ADR-0005, 0.2.0)** —
  package and CLI renamed `claude-session-capture` →
  `ai-session-capture`; second adapter (`codex_parser.py`) ingests
  `~/.codex/sessions/` rollouts; `source` column on FTS;
  `--source claude|codex|all` flag on CLI + MCP; output layout
  now `sessions/<source>/<project>/`.
- **Per-session + daily-index layout (ADR-0003)** — replaced the v1
  "one file per day, all sessions mixed" layout. One file per session
  UUID (cross-day → one file pinned to start date), thin per-day
  index with wiki-links.
- **Derive-don't-configure Claude root (ADR-0004)** — removed
  `[scope].projects_root`; derive from Claude Code canonicals +
  `--projects-root` CLI flag + honoring `CLAUDE_CONFIG_DIR`.
- **SQLite FTS5 search index** — `search` CLI subcommand with
  `--rebuild`, integrated into `daily`/`backfill`. Index at
  `~/.local/state/ai-session-capture/index.db`.
- **MCP server** — `mcp-serve`, four read-only tools
  (`search_sessions`, `list_projects`, `list_recent_sessions`,
  `get_session_text`). Optional `[mcp]` extra.
- **Phase 1 (0.1.0)** — parse → redact → render → state → CLI →
  scheduling templates → data-repo init. First full end-to-end pipe.
