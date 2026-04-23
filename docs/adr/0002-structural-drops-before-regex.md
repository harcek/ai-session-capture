# ADR-0002: Structural drops in parser, regex redaction in render

- Status: Accepted
- Date: 2026-04-20

## Context

We have two independent defenses against secrets in the output:

1. **Structural drops** — recognize `Bash(env)` / `Read(.env)` etc. at
   parse time and blank the `tool_result.content` at the source.
2. **Regex redaction** — pattern-match provider tokens in any text and
   replace with `[REDACTED:LABEL:hash6]`.

Either alone is insufficient. Where should each live in the pipeline?

## Decision

- **Structural drops run in `parser.py`** — earliest possible point,
  before the data leaves the JSONL reader.
- **Regex redaction runs in `render.py`** — right before text is
  serialized to Markdown, with a shared `RedactionReport` so the
  warning banner reflects the full day.

The order is deliberate and load-bearing.

## Rationale

- **Structural drops are a correctness property**, not a best-effort
  scan. If we know an `env` invocation produces secrets, we don't *try*
  to redact — we *remove* the content entirely. Running this as early
  as possible means the sensitive bytes never reach any downstream
  code, so a future bug in rendering, logging, or debugging can't
  accidentally leak them.
- **Regex redaction is best-effort by nature.** Patterns miss things;
  novel formats slip through. Running it just before serialization
  means even secrets that entered via some path we didn't anticipate
  (e.g., pasted inline by the user in a prompt) get one last chance to
  be caught before hitting disk.
- **Running regex in the renderer (not the parser)** also lets the
  `RedactionReport` be a per-day aggregate that drives the warning
  banner. If redaction ran in the parser, we'd either need to thread
  the report through more layers or accept banner-granularity drift.

## Consequences

- The parser knows about sensitive command/path shapes. That's a soft
  layer violation (parsing vs. security), but the alternative is worse:
  letting sensitive bytes transit the codebase.
- The `RedactionReport` instance is owned by the renderer and lives
  for exactly one render call. Don't move it up the stack without a
  reason — keeping it scoped tight makes "no plaintext in the report"
  trivially true.
- Adding a new sensitive pattern touches two places depending on type:
  command/path shape → `parser.py`; token regex → `redact.py`. The
  separation is principled; don't collapse it.
