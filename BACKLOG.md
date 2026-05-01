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

### #A — Private remote + auto-push

Wire a private git remote for the data repo; daily runs optionally
commit and push after writing. With v0.3.0's per-machine layout in
place, push collisions are gone — each machine writes only under
`sessions/<this-machine>/`. Open question: sync approach (Syncthing
vs. own SSH hub vs. private GitHub) — evaluate against the
operator's threat model before wiring the push logic. After this
lands, running `search --rebuild` after `git pull` reindexes every
machine's captures from any host.

- Depends on: nothing shipped.
- Estimated effort: S (a few hours once the remote is decided).

### #B — OpenCode transcript adapter

Same pattern as the Codex adapter shipped in v0.2.0: a sibling
parser module (`opencode_parser.py`) producing
`Record(source="opencode")` instances, reusing the shared redact /
render / state / search / mcp_server pipeline. Different transcript
shape; mechanical to add once the format is reverse-engineered.
Also the natural trigger for the StrEnum + table-dispatch
refactors deferred during /simplify.

- Depends on: nothing shipped. Reuses ADR-0005's adapter pattern.
- Estimated effort: M (1–2 days including format research).

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

### Large-session ergonomics — Obsidian indexing + agent slicing

Real session MDs grow huge when a session spans weeks (3000+ turns,
multi-megabyte single files). Two consumer-side surfaces hit this:

1. **Obsidian** struggles to index the vault. Search, graph view,
   and link rendering get sluggish or stall on the big MDs.
2. **LLM-driven processing** (e.g. weekly digests, see ◇◇ Maybe)
   blows the token budget if it loads whole session files.

Options to evaluate when this becomes blocking:

- **Tell Obsidian not to index `sessions/`**, only the daily index
  files. Obsidian has `Settings → Files & Links → Excluded files`
  for folder-level exclusion; clicking a wiki-link still opens the
  underlying session MD on demand without pre-indexing. Lowest-cost
  workaround — pure config, no code change.
- **Daily-only consumption surface**. Configure Obsidian (or the
  user's reading habit) to enter via daily indexes and drill in.
  Pairs naturally with the exclusion above.
- **Per-day session shards**. Architectural shift: render a session
  as multiple MDs split by date instead of one file pinned to start
  date. ADR-0003 chose the single-file shape deliberately; revisit
  only if the workarounds above prove insufficient.
- **Highlights/summary alongside full MD**. Renderer also emits a
  short "highlights" file per session next to the full one;
  Obsidian and LLM agents read the short version, drill in only
  on demand. Adds rendering complexity.

For LLM agent processing specifically (the digest case), the
straightforward tactic — applicable today without any code change —
is **slice by timestamp blocks before sending to the LLM**:

- Each session MD body uses `### [YYYY-MM-DD HH:MM:SS] {Q,A}`
  headers consistently.
- For a per-day digest of a session, grep the file for blocks
  whose timestamp prefix matches the target date — typically
  10–200 lines per day even on a multi-week session.
- Send only the slice to the LLM. Token cost stays bounded.

That tactic is also the design hint for the **Weekly LLM digest**
item (◇◇ Maybe) when it lands — the digest doesn't need full
session loads, just per-date slices.

- Surfaced by: an external agent attempting to build a digest and
  hitting both the Obsidian indexing wall and the token-budget
  wall in the same session (2026-05-01).

---

## ◇◇ Maybe

### Weekly LLM digest

Monday 07:00 cron: summarize last week's sessions into
`weekly/YYYY-WW.md`. Standup material. Revisit once there's ≥4 weeks
of data to meaningfully summarize.

Implementation hint when this lands: don't load full session MDs
into the LLM context — slice by `### [YYYY-MM-DD HH:MM:SS]` block
prefix and send only the per-date slices. Keeps token cost bounded
even for multi-week sessions. See "Large-session ergonomics"
in `◇ Backlog` for the full rationale.

### Phase 3 possibilities

- Local embeddings + vector search (chromadb or sqlite-vec).
- Per-project tag extraction, decision tracking.
- Export to other tools (Notion, Logseq).
- Auto-linking between related sessions across time.

---

## Completed (for context)

- **Multi-machine ingestion (ADR-0006, 0.3.0)** — `[machine]`
  config section, `Record.machine` field, layout
  `sessions/<machine>/<source>/<project>/`, daily index
  `daily/<machine>/<date>.md`, FTS gains a `machine` column with PK
  migration, `--machine` filter on CLI + MCP, `search --rebuild`
  walks session MDs on disk so other machines' captures index
  without their JSONL on this filesystem.
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
