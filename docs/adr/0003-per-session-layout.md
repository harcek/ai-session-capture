# ADR-0003: Per-session files + per-day index (breaks the v1 daily-only layout)

- Status: Accepted
- Date: 2026-04-21
- Supersedes: the v1 "one MD per day with all sessions mixed" layout

## Context

The v1 layout wrote one MD per local date, with every session that
touched that day concatenated inside. After three days of backfill
(2026-04-18..20) a single file was already 130 KB with ~500 turns
across two different projects. Issues:

- Cross-project context is mingled by chronology alone — hard to scan
  "what happened on project X today" without grep.
- No addressable unit for an individual session (share / link / MCP
  `get_session_text`).
- Obsidian-style backlinks/graphs have nothing to latch onto.
- Concurrent-session timelines interleave confusingly.

## Decision

Switch to **per-session files + per-day index files**.

```
~/.local/share/claude-sessions/
├── sessions/
│   └── <project>/<YYYY-MM-DD>_<HH-MM>_<uuid-short>[_<slug>].md
├── daily/
│   └── <YYYY-MM-DD>.md
├── README.md, .gitignore, .gitattributes
```

- **Session file** — one per session UUID, pinned to the session's
  first-turn local date/time. A session that spans midnight lives in
  a single file (still one UUID = one story). Turns are sorted
  chronologically across all days the session touched.
- **Daily index** — one per local date, listing every session that
  touched that day. Format is a timeline table with Obsidian wiki-links
  (`[[../sessions/<project>/<stem>|<uuid-short>]]`). Non-Obsidian
  readers see raw text; Obsidian resolves the links and builds a graph.

Filename grammar: `<YYYY-MM-DD>_<HH-MM>_<uuid-short>[_<slug>].md`.

- `uuid-short` is always present — it's the uniqueness guarantee,
  8 chars padded right with zeros if shorter.
- `slug` prefers `/rename`-set `custom-title`; falls back to the first
  substantive user prompt (skipping `<local-command-*>` wrappers);
  omitted entirely for empty sessions. Capped at 5 words / 60 chars.

Project name derivation: last `--`-split segment of Claude Code's
cwd-encoded dir name, lowercased + filesystem-sanitized, capped at
48 chars, aliased via `[projects.aliases]` (e.g. `home-openclaw →
_scratch`, `tmp → _scratch`). The `_scratch` fallback groups sessions
launched outside a real project dir.

## Rationale

- **Per-session is the natural unit of work.** Each file has a stable
  address that the MCP server, Obsidian backlinks, and casual
  filesystem browsing can all use.
- **Cross-midnight sessions stay one story.** Splitting by date would
  fragment the narrative at an arbitrary boundary; the FTS index still
  tracks `(session_id, local-date)` separately so day-based queries
  stay accurate.
- **Daily index is lightweight.** It's a thin timeline + redaction
  banner + table of wiki-links — not a duplication of session content.
  Obsidian's graph view becomes useful immediately.
- **Redaction must happen before filenames.** Secrets pasted into the
  first prompt or set as a custom title would otherwise leak into the
  filename (via the slug) and the frontmatter (via the title field).
  `render_session_file` now scrubs both before they touch the layout
  layer. Regression-tested.
- **Project aliases are a config-level escape hatch.** The
  cwd-derivation heuristic is shallow by design; aliases let users
  collapse noise (`home-openclaw`, `tmp`) into `_scratch` without
  changing the tool's logic.

## Consequences

- **Breaking change to the output layout.** v1 daily MDs (one file per
  day at the repo root) are deleted and regenerated — this was
  explicitly chosen over a migration path because v1 output has only a
  few days of history and is cheap to rebuild.
- **File count grows with session traffic**, not day count. A busy year
  might be 1–2k session files plus 365 index files. Still small by
  filesystem standards; git handles it trivially.
- **Cursor keys are namespaced** (`session:<uuid>`, `daily:<date>`) so
  the idempotency gate never confuses the two kinds of files. Old v1
  cursor keys (raw `YYYY-MM-DD`) are orphaned but harmless.
- **The FTS index is unchanged.** Still keyed by `(session_id, local-date)`
  — search results map back to file paths via the `dates_touched`
  metadata on each SessionRender.
- **The renderer is now two functions, not one.** `render_session_file`
  handles one session fully; `render_daily_index` summarizes a date.
  Both share the turn-builder and redaction pipeline.
