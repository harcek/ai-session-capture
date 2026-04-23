# ADR-0004: Derive the Claude transcripts root; don't make it user config

- Status: Accepted
- Date: 2026-04-22
- Supersedes the `[scope].projects_root` TOML field introduced earlier the
  same day in response to an initial code-review finding.

## Context

A prior code review flagged that `[scope].projects_root` was documented
in `config.toml.example` but not actually honored by CLI discovery. The
immediate response was to wire the field through so the documented
contract matched behavior.

A follow-up design review then pointed out that honoring the field as
regular user config was the *wrong* answer to the right question. The
real issue is that the transcripts root is Claude Code's concern, not
this tool's — Claude Code's own docs define `~/.claude/projects` as the
default and `CLAUDE_CONFIG_DIR` as the authoritative override. Adding a
TOML field on top creates three (now four, counting the test env var)
competing sources of truth for a single semantic concept:

```
1. Claude Code default:    ~/.claude/projects
2. Claude Code override:   CLAUDE_CONFIG_DIR
3. Tool override:          [scope].projects_root        ← we added this
4. Test/dev override:      CLAUDE_PROJECTS_ROOT
```

Four mechanisms for "where are the transcripts" is too many for a
personal local tool. It also creates a genuine footgun: pointing the
TOML field at stale / copied / old-archive data would silently archive
the wrong universe on every scheduled run.

There is also a security consequence. The parser's `_assert_under_root`
path-traversal guard validates against whatever root is chosen. That
containment check is only as strong as the trust in the root itself; a
user-editable persistent root with no validation effectively removes
the containment. The fewer places the root can be set, the tighter the
security surface.

## Decision

Remove `[scope].projects_root` from the TOML schema entirely. Delete
the `ScopeConfig` dataclass (no remaining fields). Derive the
transcripts root from Claude Code's own canonicals plus a CLI flag for
one-off imports.

**Precedence:**

1. `--projects-root PATH` CLI flag — explicit, per-run override for
   one-off imports / debugging / migration.
2. `$CLAUDE_PROJECTS_ROOT` env var — **test/dev hook only**, not
   documented for end users. Takes precedence over everything else so
   test harnesses can redirect without fighting the CLI layer.
3. `$CLAUDE_CONFIG_DIR/projects` — honors Claude Code's own relocation
   convention. If a user has relocated their entire `~/.claude` tree
   with `CLAUDE_CONFIG_DIR`, this tool follows automatically.
4. `~/.claude/projects` — the Claude Code default.

## Rationale

- **Single authoritative source.** Claude Code owns transcript
  storage. The tool consumes it. No second source of truth.
- **Honor `CLAUDE_CONFIG_DIR`** — this is the real gap the initial
  fix missed. If a user sets it to relocate their Claude config, this
  tool now follows automatically instead of silently reading the wrong
  directory.
- **CLI flag for one-off imports** is a better UX than persistent
  config: it's explicit, auditable in shell history, and doesn't
  silently alter every scheduled run forever. Archiving transcripts
  from a backup or old machine is a legitimate use case; a
  `--projects-root` flag covers it cleanly.
- **Test hook stays intact.** `CLAUDE_PROJECTS_ROOT` env var remains
  as the test/dev override (the existing test suite and the
  `fake_projects_root` fixture depend on it). It's undocumented for
  users — a flag in `config.toml.example` comments that it's internal.
- **Containment stays strong.** Because the root is derived from a
  small set of well-known locations, the path-traversal guard's
  meaning is clear: "refuse files outside the Claude-canonical root
  or an explicit import path." No user-editable middleman.

## Consequences

- `ScopeConfig` dataclass removed; `Config.scope` field removed.
- `parser.default_projects_root()` no longer accepts a `cfg` argument —
  it's a pure env+default lookup.
- CLI gains a top-level `--projects-root PATH` flag; `cmd_daily`,
  `cmd_backfill`, and `cmd_search --rebuild` all resolve root via a
  single `_resolve_root(args)` helper.
- `config.toml.example` no longer has a `[scope]` section. A comment
  block documents the derivation rules and points to this ADR.
- Test `test_default_projects_root_precedence` updated to cover the
  new 3-tier chain (env > CLAUDE_CONFIG_DIR > default).
- A new test (`test_projects_root_cli_flag_overrides_env`) exercises
  the CLI flag.
- `docs/DEFERRED.md` updated: the "scope.include/exclude" entry notes
  that if filters are ever implemented, they land in a new section
  (e.g., `[filters]`) rather than reinstating `[scope]`.

## Alternatives considered

- **Keep `[scope].projects_root`, add CLAUDE_CONFIG_DIR support.**
  Fixes the env-var gap but keeps the competing-source-of-truth
  problem. Rejected.
- **Multi-source model (`[[sources]]` list).** Too much for 2
  projects of work. Could revisit if/when the user has an actual need
  to search across multiple Claude roots.
- **`[scope].include/exclude` filter knobs.** A legitimate use for the
  `[scope]` name, but no concrete use case today (2 projects; aliases
  handle the noise-names problem). Rejected for now — if the need
  arrives, land it in a new `[filters]` section rather than
  reinstating `[scope]`.
