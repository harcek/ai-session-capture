# ADR-0006: Multi-machine ingestion

- Status: Accepted
- Date: 2026-04-30
- Implements BACKLOG item "#B Multi-machine ingestion".
- Builds on [ADR-0005](0005-multi-source-codex-adapter.md) (multi-source).

## Context

After v0.2.0, sessions are discriminated by `source` (claude / codex /
…) but not by which machine produced them. Daniel runs the tool on
three machines (MBP, Mac mini, Ubuntu workstation) and wants a
**single archive he can query from any one of them** — "what did I
decide about X last month" should see all three machines' work, not
just the local one.

Two failure modes block this today:

1. **Path collisions on git merge.** Two machines independently
   render `daily/2026-04-30.md` from their own local sessions.
   When both push to a shared remote, the file is in conflict every
   day. Per-session files don't collide (UUIDs differ across
   sources/machines), but the daily index does.
2. **FTS index is local-only.** Each machine's
   `~/.local/state/ai-session-capture/index.db` reflects only that
   machine's captures. Even if the data repo is shared via git,
   search on machine X can't find machine Y's sessions because the
   FTS row was never inserted on X.

Wiring a remote (BACKLOG #C) alone solves neither — files arrive in
git but can't be merged cleanly, and search stays fragmented. #B
adds the missing dimension first; #C then becomes a small follow-up.

## Decision

### Machine identity from config, hostname fallback

A new `[machine]` section:

```toml
[machine]
name = "mbp"   # optional; if empty, use sanitized socket.gethostname()
```

Sanitization: lowercase, strip a trailing `.local`, replace any
character not in `[a-z0-9_-]` with `-`. Resolved once at run start
via `resolve_machine_name(cfg)`.

Why config-with-fallback rather than always-hostname:
- Hostnames vary across reboots and OS updates (`mbp` vs
  `mbp.local` vs `Daniels-MacBook-Pro.local`); a stable config
  value prevents accidental archive splits when the hostname
  changes.
- Defaulting to hostname keeps zero-config installs working.

### Output layout: `sessions/<machine>/<source>/<project>/<file>.md`

Per-machine subtrees, with the existing source/project structure
nested inside.

```
~/.local/share/ai-session-capture/
├── sessions/
│   ├── mbp/
│   │   ├── claude/<project>/<file>.md
│   │   └── codex/<project>/<file>.md
│   ├── mini/
│   │   └── claude/<project>/<file>.md
│   └── ubuntu/
│       └── codex/<project>/<file>.md
└── daily/
    ├── mbp/2026-04-30.md
    ├── mini/2026-04-30.md
    └── ubuntu/2026-04-30.md
```

Rationale:
- Per-machine subtrees never collide on git merge — each machine
  only writes under its own segment.
- Wiping one machine's contribution is `rm -rf sessions/<machine>/`.
- Mirrors the natural mental model: "all my MBP work is in
  `sessions/mbp/`."
- Source-first (alternate ordering) makes "all my Codex" cheaper
  but "all my MBP" expensive; the latter is the more common ask
  when triaging a single machine's archive.

### Daily index: `daily/<machine>/<date>.md`

Each machine's daily index lives under its own subtree. A
"cross-machine view of one day" is answered via FTS (filter to
`since=date until=date`), not by merging daily MDs.

Rejected alternatives:
- **Drop daily index entirely.** Tempting once FTS exists, but the
  daily MD provides Obsidian wiki-link breadcrumbs and a human-
  readable per-day overview that FTS doesn't replace. Keep it.
- **Flat `daily/<date>.md` with merge logic.** Each machine
  appends its sessions; merge logic per machine. Silent merge
  bugs are hard to detect. Avoid.

### `Record.machine` field

`Record.machine: str = ""` defaults to empty; both parsers
populate it from the resolved machine name. Frontmatter gains a
`machine: <name>` field and a `machine/<name>` tag.

### FTS schema gains `machine` column

`sessions` table and `sessions_fts` virtual table both gain
`machine TEXT`. PK becomes `(id, date, source, machine)`. Same
dump-and-rebuild migration pattern as v0.2.0 used for the `source`
column.

### `--rebuild` walks all MDs in the data dir

Today, `search --rebuild` re-runs the JSONL pipeline. v0.3.0 adds
a second path: walk every `<output>/sessions/**/*.md`, parse the
YAML frontmatter, and build `SessionIndexRow` from disk. The body
of each MD becomes the FTS content.

Why: this is what makes "one machine, all archives" actually work.
After `git pull`, run `--rebuild` and the FTS sees every machine's
captures, not just this one's. The JSONL-driven path stays for
daily/backfill ingest (cheaper, doesn't need to round-trip through
disk).

`index_row_from_md(path) → SessionIndexRow` is the new helper.
Frontmatter contract:
- `session_id`, `source`, `machine`, `project` are required.
- `started_at`, `redactions_total`, `turn_count` map straight
  through.
- `date` is the first element of `spans_dates` if present, else
  the calendar date of `started_at`.

### CLI `--machine` filter

- `daily` / `backfill`: informational no-op. Only this machine's
  JSONL exists on this filesystem; passing a non-current value
  logs a warning and proceeds with the current machine.
- `search`: passes through to FTS as a filter alongside `--source`.

### MCP `machine` parameter

All four tools (`search_sessions`, `list_projects`,
`list_recent_sessions`, `get_session_text`) gain an optional
`machine` parameter, plumbed into the same helpers as `source`.
Result rows already need to expose `machine` so the LLM can
post-filter or attribute hits.

### Migration shim

First v0.3.0 run with default-config `output.dir` detects a v0.2.0
shape (`sessions/claude/` or `sessions/codex/` at the data dir
root, or `daily/<date>.md` at the daily dir root) and moves them
under `sessions/<this-machine>/` and `daily/<this-machine>/`
respectively. Idempotent — re-running is a no-op.

Custom `output.dir` values are left untouched on the same principle
as `migrate_data_dir`: user's call.

### Relationship to BACKLOG #C

#B is self-contained — it works without #C (each machine still
only sees its own data, but the multi-machine *dimension* is in
the schema, the paths, and the API). #C (private remote +
auto-push) becomes a focused follow-up: wire
`[integrations.git]`, `git add/commit/push` after the state.py
write phase, opt-in default. Once #C lands and you run
`--rebuild` after pulling on any machine, the unified-archive
story is real.

## Consequences

### Positive

- Per-machine subtrees mean #C doesn't introduce git-merge pain.
- FTS rebuild from MDs decouples the search index from the JSONL
  ingest path — useful in its own right (re-render after
  fixing a redaction regex without re-parsing transcripts).
- `Record.machine` is the obvious place to hang per-machine
  metadata if it's ever needed (host-specific tool paths, agent
  versions).

### Negative

- Breaking layout change for v0.2.0 users. Mitigated by the
  migration shim, but anyone with custom `output.dir` and tooling
  that depends on the v0.2.0 layout has to adjust manually.
- Frontmatter contract for FTS-from-MD path is a new schema
  surface. If a future version drops one of the required fields,
  rebuild silently produces incomplete rows. Mitigated by a
  required-fields check in `index_row_from_md` that raises on
  missing keys.
- `daily/<machine>/` reduces the convenience of "open one file to
  see today's work" when you're on a specific machine. Acceptable —
  `cat daily/$(hostname -s)/2026-04-30.md` is one shell line.

### Out of scope for v0.3.0

- **Auto-push (#C)** — separate commit once #B is stable.
- **Cross-machine deduplication.** A single Codex/Claude session
  conceptually couldn't span two machines (each machine's agent
  has its own session UUIDs), so the per-machine partitioning is
  exhaustive. If that ever changes, a `(source, session_id)` global
  primary key would need re-thinking.
- **Per-machine config sections** beyond `[machine].name`. Today
  there's no need for per-machine `[redaction]` overrides or
  similar; one config per machine is the answer if you ever need
  divergent behavior.

## Implementation order

1. Config: `[machine]` section + `resolve_machine_name(cfg)`.
2. `Record.machine` field; both parsers populate it.
3. Layout: `sessions/<machine>/<source>/<project>/`; render
   frontmatter + tag.
4. Daily index path: `daily/<machine>/<date>.md`.
5. Migration shim for legacy archives.
6. FTS schema migration (`machine` column + new PK).
7. `index_row_from_md` + `--rebuild`-from-MDs.
8. CLI `--machine` flag.
9. MCP `machine` parameter.
10. Smoke test against real Claude+Codex transcripts.
11. Docs + CHANGELOG `[0.3.0]`.

Each step keeps the test suite green. Single bundled commit at the
end, matching the v0.2.0 pattern.
