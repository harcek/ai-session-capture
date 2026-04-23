# Deferred review findings

This doc captures items from the 2026-04-22 code review that we
consciously chose not to implement right now, with the reason each was
deferred. Pair with `BACKLOG.md` when prioritizing — if you want one of
these, move it into the backlog with a concrete motivating use case.

The goal is to avoid two failure modes: (a) silently dropping valid
review feedback, and (b) implementing defensive noise that no one
asked for.

## Deferred

### `scope.include` / `scope.exclude` glob filters

- **Review finding:** `scope.include` / `scope.exclude` are documented
  in the original config example but not wired anywhere.
- **Why deferred:** Current usage has a small number of projects; no
  present need to include or exclude subsets. The alias
  system (`[projects.aliases]`) already covers the "rename noise to
  `_scratch`" case. Real implementation would need `fnmatch`-based
  predicate handling inside `iter_jsonls` and additional test
  coverage.
- **Handling:** Removed from `config.toml.example` on 2026-04-22 — no
  phantom knobs. Note: the `[scope]` section no longer exists at all
  after ADR-0004 removed `scope.projects_root`. If this filter use case
  arrives, it lands in a new dedicated section (e.g. `[filters]` or
  `[selection]`) rather than reinstating `[scope]`.
- **Estimated effort:** S (half a day) when picked up.

### Output-root symlink / ownership validation

- **Review finding:** `state.write_at` follows symlinks implicitly; a
  compromised or misconfigured `cfg.output.dir` could redirect writes
  elsewhere. Suggestion: validate ownership, refuse symlinks, check
  file type.
- **Why deferred:** Trust model is config-wide. If the config is
  compromised, the attacker already controls `redaction.enabled`,
  `[projects.aliases]`, and `redaction` patterns — treating the
  output dir as a special trust boundary singles out one knob
  arbitrarily. The `O_NOFOLLOW` we already use on *reads* of the
  JSONL input is the real boundary (hostile JSONL content); writes
  into our own configured output dir live in user-trust space.
- **Handling:** No change. If you ever move to a multi-user or
  supply-chain-threatened environment, revisit.

### CI / lockfile / dependency audit

- **Review finding:** No `.github/workflows/ci.yml`, no `uv.lock`, no
  `pip-audit`/`safety` run in CI.
- **Why deferred:** Project has no GitHub remote yet. Wiring CI on a
  repo without a remote is premature — the first PR after the remote
  is wired is the right time.
  - Lockfile: `uv lock` is a 30-second add when it matters (e.g.
    when you want reproducible installs across your 3 machines).
  - Dependency audit: runtime deps are `jinja2` + `platformdirs` +
    (optional) `mcp`. Tiny surface. Not urgent.
- **Handling:** Do all three in one pass the day the GitHub remote is
  wired (backlog #1).
- **Estimated effort:** S (1–2 hours).

### Observability summaries

- **Review finding:** No counters for skipped malformed lines, skipped
  files, indexed row counts, drift/rebuild counters, etc.
- **Why deferred:** Premature for a local-only single-user tool that's
  been running for less than a week. The current `logger.info` output
  already carries the essentials. If something eventually feels weird
  in practice, *then* we'll know exactly which counter to add.
- **Handling:** Revisit after 4+ weeks of unattended scheduler runs.

### Canary-secret fixtures for redaction regression

- **Review finding:** No fixture JSONL with known secrets seeded in to
  prove the redaction regex catches them — a test would break the day
  a refactor nerfs a pattern.
- **Why deferred:** Already tracked as backlog #8 (security hardening
  round 2). Waiting for a triggering event — either a near-miss leak
  or a refactor that would plausibly nerf the regex.
- **Handling:** BACKLOG.md #8.

### `age` encryption of output MDs

- **Review finding:** Recommended before any automatic remote sync.
- **Why deferred:** Already tracked as backlog #3. Opt-in feature.
  Current posture is plaintext MDs inside a private git repo with
  aggressive redaction + structural drops — appropriate for
  local-only + private remote, not for public-remote risk.
- **Handling:** BACKLOG.md #3. Revisit the day the data repo gets a
  remote.

## What we explicitly *removed* (phantom config)

Not deferred — actively deleted on 2026-04-22 because they were
documented but never implemented, and implementing them wouldn't be
valuable:

- `content.user_prompts` / `content.assistant_text` — "render a
  session without user prompts" isn't a coherent use case; prompts
  are half the value of the archive.
- `content.assistant_thinking` — we don't surface thinking blocks
  from the parser; the knob pretended to control something that
  didn't exist.
- `content.system_reminders` — same reason.
- `formatting.timestamp` options ("iso" / "HH:MM:SS" / "none") —
  we always render full datetime. Changing this is trivial if anyone
  ever wanted it, but no one has.
- `scope.include` / `scope.exclude` — moved to "deferred with use
  case" (see above) rather than "pretend-implemented."

Removal policy: **either implement and test, or remove.** Never keep
a documented config knob that does nothing.
