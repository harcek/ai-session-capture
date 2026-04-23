# ADR-0001: TOML over YAML (or JSON) for config

- Status: Accepted
- Date: 2026-04-20

## Context

The tool needs a user-editable config file for content filters, redaction
knobs, output paths, etc. Three realistic formats: TOML, YAML, JSON.

## Decision

Use **TOML**. Load via stdlib `tomllib` (Python 3.11+). No schema library.
Dataclass defaults with a small `from_dict` walker; unknown fields
silently ignored so typos can't wedge a headless 06:00 run.

## Rationale

- **TOML is in stdlib** (as of 3.11). JSON is too; YAML needs a dep.
- **JSON has no comments.** A config the user edits should have comments.
- **YAML has comments** but also Turing-complete corners that matter
  exactly zero for this use case. Every YAML library I've ever used has
  a surprising edge case (anchor resolution, `on`/`off` being booleans,
  etc.). Not worth the dep and the footguns for a 10-field config.
- **TOML's shape matches dataclasses cleanly.** Nested tables
  (`[output.frontmatter]`) map onto nested dataclass fields with a
  two-line recursive merge. No schema lib needed.

## Consequences

- Users must write TOML, not YAML. One more minor learning curve, but
  TOML is simple.
- If the schema grows past ~25 fields or starts needing coercion (str →
  enum, etc.), reconsider `pydantic` or `msgspec`. Not today.
- `config.toml.example` in the repo root is the canonical reference,
  co-located with the code that reads it so it stays in sync.
