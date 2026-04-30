# Agents guide

Guidance for any AI coding agent (Claude Code, Codex, OpenCode, etc.)
working in this repository. Humans are welcome to read it too — it's
the same playbook.

`CLAUDE.md` is a symlink to this file so Claude Code's auto-loader
picks up the same guidance. Keep any updates here; the symlink
propagates them.

## Project in one paragraph

`ai-session-capture` is a redaction-first, local-first archive for
AI coding-agent sessions. As of v0.2.0 it ships **two adapters in
one package** (`src/ai_session_capture/`): a Claude Code adapter
(`parser.py`) and a Codex adapter (`codex_parser.py`). OpenCode
will follow as a third sibling parser (see [`BACKLOG.md`](BACKLOG.md)).
The core pipeline — parse, structurally drop sensitive tool output,
regex-redact, render Markdown, index in FTS5, serve via MCP — is
shared; adapters contribute only their source-specific parsers.

## Non-negotiable principles

1. **Structural drops before regex redaction.** Sensitive tool output
   (`env`, `cat .env`, Reads of credential files) is blanked at parse
   time, before anything downstream sees it. See
   [`docs/adr/0002-structural-drops-before-regex.md`](docs/adr/0002-structural-drops-before-regex.md).
2. **Redact every user-surface — enumerate them.** Body, filename,
   frontmatter, title, cwd, log lines. When adding a new surface,
   add a test that seeds a secret and asserts it doesn't land there.
3. **Either implement and test, or remove.** Never ship a documented
   config knob that does nothing. ADR-0004 is the canonical example of
   the deeper version of this rule: if a field's correct behavior is
   to derive from another source, the answer is to delete the field,
   not to wire it up.
4. **Deterministic rendering.** Same inputs → byte-identical output.
   The content-hash idempotency gate in `state.py` depends on this.
5. **File hygiene.** `umask 0o077` on entry, directories `0o700`,
   files `0o600`, atomic `tmp + rename` with `O_NOFOLLOW` on opens.
6. **Commits are functional increments.** Not per-file, not a grab-
   bag. Each commit has a descriptive prose body and a matching
   `CHANGELOG.md` entry.
7. **No AI-attribution trailers in commits.** The maintainer's
   convention.

## Project layout

```
src/ai_session_capture/
  parser.py        — Claude Code JSONL stream + structural drops
  codex_parser.py  — Codex rollout JSONL stream + structural drops
  redact.py        — regex redaction + injection neutralization
  render.py        — Jinja2 session + daily-index rendering
  state.py         — flock, atomic writes, cursor hashes, logging
  search.py        — SQLite FTS5 index + query (source-aware)
  cli.py           — argparse entrypoints (--source flag)
  mcp_server.py    — MCP stdio server (optional [mcp] extra)
  layout.py        — filename / path generation
  config.py        — TOML → dataclass loader
  templates/       — Jinja2 session.md.j2 + daily_index.md.j2

tests/             — pytest suite (run ``pytest tests/``)
docs/
  ARCHITECTURE.md — 5-minute orientation
  adr/            — architectural decision records (numbered)
  DEFERRED.md     — review findings consciously not acted on

scheduling/       — launchd plist + systemd .service/.timer templates
scripts/          — install.sh / uninstall.sh (POSIX sh, uname-branched)

BACKLOG.md        — work-item queue with priority
CHANGELOG.md      — history, semver-ish
SECURITY.md       — threat model, redaction posture
```

## Running tests

```sh
.venv/bin/pytest tests/
.venv/bin/ruff check .
```

Baseline expectations live in the CHANGELOG; the number grows as
features land. Any new feature should have tests; no test → no ship.

## Commit conventions

- Subject ≤ 70 chars, imperative mood ("Add FTS cross-day re-index",
  not "Added..."). Body wraps at ~72 chars, explains *why*, not
  *what*.
- One functional increment per commit. If the diff spans two
  concerns, split it.
- Update `CHANGELOG.md` in the same commit, under `[Unreleased]`,
  categorized: Added / Changed / Removed / Security / Fixed.
- Never include AI-attribution footers (`Co-Authored-By: Claude
  ...`) — maintainer preference.

## Where decisions live

Before proposing a design, check:

- `docs/ARCHITECTURE.md` — the high-level map.
- `docs/adr/` — numbered decision records. Each ADR captures
  context / decision / rationale / consequences. New non-obvious
  decisions land as new ADRs.
- `docs/DEFERRED.md` — review findings we consciously didn't act on,
  with reasons.
- `BACKLOG.md` — prioritized work items.

## Common footguns (things we've done wrong before)

- **Adding a config knob you haven't wired.** No phantom fields. The
  rule: a config field ships with a test that would fail if the field
  were ignored. If you can't write such a test, the feature isn't
  ready to expose.
- **Cross-module drift.** If user-visible data passes through a
  transformation (sanitization, alias, redaction), grep every module
  that touches that data and apply the transformation consistently.
  Unit tests with defaults will miss the divergence.
- **Overbroad `except` catches.** Don't catch a broad class and
  relabel — allow-list the real-infrastructure failures and let them
  propagate. The alternative hides bugs as user errors.
- **Weak regression tests.** If your assertion would pass against the
  pre-fix code too, the test doesn't prove the fix. Test the broken
  behavior explicitly.
- **Orphan records on shrinking inputs.** Upserts must delete what's
  no longer in the incoming set. "Upsert + forget" silently grows
  stale state.
- **Fixing the narrow contract vs. rethinking the design.** A
  reviewer flagging "config field X is ignored" might mean "wire it
  up" or might mean "this field shouldn't exist at all." Ask the
  second question before committing to the first. Our
  [`docs/adr/0004-derive-dont-configure-claude-root.md`](docs/adr/0004-derive-dont-configure-claude-root.md)
  is the concrete example.

## What NOT to do

- Don't make the Claude transcripts root a user config knob. It's
  derived from Claude Code's own canonicals (see ADR-0004). If a
  future feature seems to need this, re-read the ADR first.
- Don't broaden the MCP tools to *write* anything. The server is
  read-only by design. Writes are a scheduled-job concern.
- Don't add a new dependency without checking it's already in
  `pyproject.toml` or without a clear reason. Small dependency
  surface is an explicit goal.
- Don't bundle a new agent's transcript ingestion into an existing
  parser. Each adapter is a sibling module (see `parser.py` and
  `codex_parser.py`) and reuses everything downstream of itself —
  redact, render, state, search, mcp_server are source-agnostic.

## Quick reference for scheduled runs

Headless (`daily` via scheduler) is the primary path. If a scheduled
run fails, the rotating `run.log` under the XDG state dir has the
traceback, and a `last-error` sentinel file is written (cleared on
next success). Desktop notifications fire best-effort on failure.
