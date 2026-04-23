# Security posture

`ai-session-capture` exists because AI-coding-agent transcripts
contain sensitive material and you want a redacted, shareable archive.
This release ships the Claude Code adapter; the threat model and
defenses below apply to anything the adapter-based architecture
ingests — future Codex / OpenCode adapters inherit the same posture.

The posture is documented here so future-you (or anyone reading the
repo) knows what it protects against — and, more importantly, what
it does **not**.

## Threat model

What we defend against, ranked by likelihood × impact:

| # | Threat | Defense |
|---|---|---|
| 1 | Secrets accidentally pasted or echoed into a session get committed to the data repo | Structural drops (parse-time) + regex redaction (render-time) + warning banner |
| 2 | Symlink attacks against the parser's JSONL reader | `O_NOFOLLOW` on every open; path must resolve under `CLAUDE_PROJECTS_ROOT`; file-owner check |
| 3 | Path traversal out of the projects root | Explicit `is_relative_to(root)` check on every resolved path |
| 4 | TOCTOU races on the output MD | Atomic tmp + `os.replace`; umask 0o077; dir 0700, files 0600 |
| 5 | Concurrent scheduler fires racing each other | `fcntl.flock` non-blocking exclusive lock; second run errors out cleanly |
| 6 | Prompt injection via hostile transcript content being fed back to future LLMs reading the archive | Zero-width + bidi-override char stripping; all transcript content is wrapped in fenced blocks when rendered |
| 7 | Malformed or truncated JSONL wedging a scheduled run | Per-line try/except, 10 MiB line cap, silent skip on decode error |

## What we do **not** defend against

Be honest about the limits:

- **A determined adversary with local code-execution on your machine.**
  If someone's root on your Mac, this tool is irrelevant to their day.
- **GitHub / sync-provider insiders or breaches.** A private GitHub repo
  is "encrypted at rest by the provider" — not "confidential from the
  provider." The `age` encryption backlog item (#3) closes this gap,
  but it's opt-in and not default.
- **New secret patterns you've never shown me.** The regex set covers
  the common providers (AWS, GitHub, OpenAI, Anthropic, Slack, Google,
  Stripe, JWTs, SSH/PEM keys, DB URLs with creds, sensitive env-style
  assignments). Your-company-specific tokens that look arbitrary need
  to be added to the config `[redaction].patterns` list.
- **Content inside images / attachments.** We capture a placeholder
  line for attachments; their bytes are not read or redacted.
- **Secrets inside the *content* field of a `tool_use`** (as opposed to
  a `tool_result`). If Claude emits a Bash tool_use whose `command`
  field contains a pasted secret, the tool_use input is shown in the
  "tool call" summary. Run `--dry-run --show-redactions` after any
  session that involved pasting credentials and eyeball the output.

## Redaction ordering

The pipeline is deliberate — longest/most-specific patterns run first so
they don't get chewed by broader scanners later:

```
1. SSH_PRIVATE_KEY, PEM_PRIVATE_KEY  (multi-line blocks)
2. ANTHROPIC_KEY  (before OPENAI_KEY so sk-ant- wins over sk-)
3. OPENAI_KEY
4. AWS_AKID, GITHUB_* (four variants), SLACK, GOOGLE, STRIPE
5. JWT, DB_URL_WITH_CREDS
6. ENV_ASSIGN  (only when the KEY name matches a sensitive-word regex)
```

Matches become `[REDACTED:LABEL:hash6]` where `hash6` is the first six
chars of `sha256(match)`. The hash lets the same secret appear as the
same placeholder across a file, which helps correlation without leaking.

## The warning banner

When any redaction fires, the top of the daily MD gets a prominent block
summarizing the counts and reminding you to:

1. **Rotate** the exposed secret.
2. **Audit** the path that leaked it (`cat .env`? pasted inline? echoed
   by a tool?) and close it.
3. **Substitute** placeholder values in prompts.

This is intentional product friction. The redaction is defense in depth,
not a habit enabler. Every hit on the counter is a behavior to fix.

## File system hygiene

- `umask 0o077` at CLI entry, so any file we create defaults to `0600`.
- Output dir enforced to `0700` on every write.
- Tmp files live in the same directory as their target and are always
  cleaned up on exception; `os.replace` makes the rename atomic across
  any filesystem we care about.
- State dir (`~/.local/state/claude-session-capture/`) holds
  `cursor.json` (content hashes keyed by date), `run.lock`, `run.log`
  (rotating 5 MB × 3), and `last-error` (cleared on success).

## Structural drops

The parser drops sensitive tool results **before they enter the
rendering pipeline**, so the sensitive bytes never touch the output MD
even if redaction regex has a bug. Caught at the `tool_use` layer and
matched to the subsequent `tool_result` via `tool_use_id`.

Matches that trigger a drop:

- `Bash` with a command matching `env`, `printenv`, `cat .env`,
  `cat ~/.netrc`, `cat ~/.ssh/…`, `aws configure`,
  `aws sts get-caller-identity`, `gh auth token`, `op read`,
  `security find-generic-password`, `kubectl … secret`, `vault kv/read`,
  `gcloud auth print/login`, `az account get-access-token`,
  `heroku config`, `doppler secrets`.
- `Read` with a `file_path` matching `.env`, `.aws/`, `.ssh/`, `.gnupg/`,
  `.config/gh/`, `.netrc`, `.pgpass`, `.npmrc`, `.pypirc`,
  `credentials[.json]`, `secrets.{yaml,json,toml}`, `id_rsa*`,
  `id_ed25519*`, `*.pem`, `*.p12`, `*.pfx`, `.kube/config`.

If you need to extend this list, the patterns live in
`src/claude_session_capture/parser.py::SENSITIVE_BASH` and
`SENSITIVE_PATH`.

## What a leak looks like

If a secret somehow survives both structural drops and regex redaction
(undetected new pattern, typo in the regex, novel provider format) and
lands in a committed daily MD:

1. **Treat the secret as burned.** Rotate it at the provider immediately.
   Don't wait until you've scrubbed git history — the remote is compromised.
2. **Scrub git history** with `git filter-repo --replace-text secrets.txt`
   (BFG is the older tool; filter-repo is the current one). Force-push
   all branches and tags.
3. **Delete and recreate the remote repo** if the secret was high-value
   — caches, forks, and `GHArchive` may retain even after scrub.
4. **Audit access logs** on the rotated credential.
5. **Add the pattern** to `[redaction].patterns` in config and write a
   regression test in `tests/test_redact.py` before closing the loop.

## Review cadence

- After any change to `parser.py::SENSITIVE_*` or `redact.py::_*_PATTERNS`,
  re-run the full test suite (`pytest tests/`) and eyeball a dry-run
  (`claude-session-capture --dry-run backfill | head -200`).
- Quarterly: run `--show-redactions` over the last 30 days, compare
  counts against baseline, investigate any big drop (might mean a
  regex broke).
