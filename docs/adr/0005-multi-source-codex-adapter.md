# ADR-0005: Multi-source ingestion + Codex adapter

- Status: Accepted
- Date: 2026-04-23
- Implements BACKLOG item "Codex transcript adapter" and partially
  implements "Source-aware ingestion across machines" (the
  per-source dimension; per-machine remains future work).

## Context

The 0.1.0 release ships only a Claude Code adapter. Daniel uses
both Claude Code and OpenAI's Codex CLI; future support for
OpenCode is also on the roadmap. The natural product framing is
"AI session capture for any agent" — hence the repo name
`ai-session-capture`. The code so far has been
`claude-session-capture` because that was the only adapter.

Adding a second adapter forces several decisions at once:

1. **CLI / package name.** Should the binary stay
   `claude-session-capture` with a `--source codex` flag, or rename
   to `ai-session-capture` to match the repo and the broader
   product story?
2. **Output layout.** How do per-source archives sit in the same
   data repo without colliding?
3. **Schema.** Codex's rollout JSONL is structurally different
   from Claude's. How is `Record` extended without breaking the
   existing pipeline?
4. **Configuration.** Should the user have to opt in to each
   source?
5. **Data-fidelity policy.** Codex emits both `response_item.message`
   and `event_msg.user_message` for what looks like the same user
   turn. How is overlap handled without losing data?
6. **Scheduling, install, MCP.** Do these all need rename + logic
   changes?

## Decision

### CLI + Python package both rename to `ai-session-capture`

- Binary: `claude-session-capture` → `ai-session-capture`.
- Python package: `src/claude_session_capture/` → `src/ai_session_capture/`.
- XDG config / state / data dirs likewise rename, with one-shot
  in-place migration (rename the old directory to the new on first
  run if the new one doesn't exist; same shim pattern used for
  `logbook.db` → `index.db`).
- The rename ships **as part of v0.2.0**, in the same commit as
  Codex support — never as a standalone cosmetic change. This
  satisfies LESSON #13: the rename is meaningful precisely because
  the code now does what the new name promises.
- MCP `mcpServers` config in `~/.claude/settings.json` requires a
  one-line edit on each user's machine; documented in the
  CHANGELOG.

### Output layout: `sessions/<source>/<project>/<file>.md`

A new top-level folder per source under `sessions/`. Examples:

```
~/.local/share/ai-sessions/
├── sessions/
│   ├── claude/
│   │   ├── deep-value-scanner/2026-04-20_…_<id>_<slug>.md
│   │   └── _scratch/…
│   └── codex/
│       ├── deep-value-scanner/2026-03-18_…_<id>_<slug>.md
│       └── _scratch/…
└── daily/2026-04-23.md
```

Rationale:
- Clean mental model — "all my Codex stuff" is `sessions/codex/`.
- Disposable per source — wiping `sessions/codex/` and re-running
  `backfill --source codex` rebuilds just that source.
- Daily-index files stay flat and pull from all sources.
- Project names can collide across sources (same `cwd` used with
  both Claude and Codex), and that's fine — they're disambiguated
  by the source segment in the path.

### `Record` gains a `source` field

`source: str = "claude"` defaults to claude for any code path that
hasn't been updated. The FTS index gains a `source` column with a
schema migration on first run. MCP `search_sessions` and
`list_recent_sessions` gain an optional `source` filter.

### Codex parser as a sibling module: `src/ai_session_capture/codex_parser.py`

Sub-module within the renamed package, not a separate package.
Reuses the existing redact / render / state / search / mcp_server
verbatim — those are source-agnostic. When a third adapter
(OpenCode) lands, that's the moment to factor the shared core out
into its own module; until then, in-package siblings keep the diff
focused.

### Configuration: discover all sources, no opt-in required

The capture tool walks both `~/.claude/projects/` and
`~/.codex/sessions/` by default if either exists. Absent dirs are
silently skipped — no error, no warning. Users who only have
Claude installed see only Claude in their archive; nothing they
need to configure.

CLI gains `--source claude|codex|all` (default `all`) for
selective backfill / search.

### Data-fidelity policy for Codex's overlapping records

Codex emits both:
- `response_item.message, role=user` — primary conversation
- `event_msg.user_message` — runtime echo of the user input,
  with `text_elements` and `images` arrays

Empirical observation across 5 sample sessions: `text_elements`
and `images` are **always empty** in current Codex versions, and
the `message` field of `event_msg.user_message` is always a
string-shadow of the prior `response_item.message` body.

**Policy:** treat `response_item.message` as authoritative; skip
`event_msg.user_message` for body content. To guard against
future Codex versions that actually populate `text_elements` /
`images`, the parser logs at DEBUG level whenever it encounters
an `event_msg.user_message` with non-empty `text_elements` or
`images`. If the log surfaces real content in practice, we promote
those fields to first-class handling.

This is the same "don't lose data" discipline that gave us the
orphan-row cleanup in `search.upsert_rows` — when behavior depends
on an empirical observation, leave a trip-wire that fires if the
observation no longer holds.

**System priming detection.** Codex's first
`response_item.message, role=user` record per session contains
multiple `input_text` blocks that are not user content but
system priming (`# AGENTS.md instructions for …`,
`<environment_context><cwd>…</cwd></environment_context>`).
Detection: skip any `input_text` block whose stripped content
starts with one of a known set of system-priming markers
(`<environment_context`, `# AGENTS.md instructions`,
`<user-instructions>`, `<system>`). Same idiom as the Claude
parser's `<command-name>` / `<local-command-*>` skip.

### Project derivation differs from Claude

Codex stores the cwd cleanly inside `session_meta.payload.cwd` and
again in each `turn_context.payload.cwd`. We use the
`session_meta` value (set once at session start) and fall back to
`turn_context` if absent. No path-encoding gymnastics needed.

### File discovery

Codex layout is `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.
Claude layout is `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`.
Each adapter owns its own `iter_*_jsonls` and root-resolution
function. Path-traversal containment guards remain per-adapter
(refuse files outside the resolved root for that source).

### Scheduling and install scripts

Renamed launchd label / systemd unit names ship with the v0.2.0
rename. Existing units from v0.1.0 are tolerated — they can be
removed at the user's discretion via `./scripts/uninstall.sh`
(which keeps its old-LABEL fallback for one release). Detailed in
the CHANGELOG.

## Consequences

### Positive

- One coherent v0.2.0 release: rename + multi-source + Codex
  support + ADR-0005 land together.
- Future OpenCode adapter is mechanical: new sibling parser,
  new `--source opencode` value, no architectural changes.
- `sessions/<source>/` layout makes per-source archive management
  obvious and reversible.
- Search and MCP gain a `source` dimension users will appreciate
  ("show me only my Codex work on the scanner").

### Negative

- Breaking change for v0.1.0 users:
  - One-line edit to `~/.claude/settings.json` (MCP `command`
    field).
  - Re-run `./scripts/install.sh` to register new-named scheduling
    units.
  - First run after upgrade migrates XDG dirs in place — visible
    only via `last-error` if migration fails.
- `Record.source` is a discriminator that downstream code must
  respect when filtering. Caught by tests, but tooling (jq, ad-hoc
  shell scripts) that read the FTS DB directly will see a new
  column.
- The system-priming-marker list is heuristic. False negatives
  (priming we don't detect) leak into the archive as if they were
  user prompts; false positives (real user text matching a marker)
  get dropped. Both are unlikely given the markers are
  `<…>`-bracketed mechanical wrappers, but worth a regression
  fixture per marker we add.

### Out of scope for v0.2.0

- **Multi-machine ingestion** (BACKLOG item #A). The `source`
  axis is per-tool, not per-machine. Multi-machine adds a
  `machine` discriminator alongside `source` and is its own
  refactor.
- **OpenCode adapter** — same pattern as Codex, lands as v0.3.0.
- **Tool-call schema unification.** Today, `tool_calls`/
  `tool_results` items carry source-specific shapes; the renderer
  handles each via simple type-dispatch. A normalized internal
  shape (`{name, input_summary, dropped, ...}` regardless of
  source) is a future cleanup.

## Implementation order

1. **Codex parser** (`codex_parser.py`) producing `Record(source="codex")`
   instances. Tests against synthetic fixtures.
2. **`Record.source` field** + downstream consumers updated.
3. **FTS schema migration** (`source` column).
4. **CLI `--source` flag** in `daily`, `backfill`, `search`.
5. **MCP source filter** on `search_sessions` and
   `list_recent_sessions`.
6. **Render layout change** to `sessions/<source>/<project>/`.
7. **The rename**: package, CLI, paths, units, plus migration
   shims. Done last so all the tests and code are updated under
   the old names first; the rename is a mechanical sed.
8. **Docs**: CHANGELOG `[0.2.0]` entry, README + AGENTS update,
   this ADR finalized.

Each step keeps the test suite green. The rename in step 7 is a
single mechanical patch.
