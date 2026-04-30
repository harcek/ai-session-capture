"""Stream-parse OpenAI Codex CLI rollout JSONL into normalized records.

Same security-first posture as the Claude parser: structural drops at
parse time for sensitive shell invocations and credential-file reads,
before content reaches downstream layers. Different file layout
(date-bucketed `YYYY/MM/DD/rollout-*.jsonl` instead of cwd-encoded
project dirs), different schema (typed envelope `{type, timestamp,
payload}` instead of Claude's role-based shape), but the resulting
``Record`` instances are uniform.

See `docs/adr/0005-multi-source-codex-adapter.md` for the design
decisions behind this module.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from .parser import (
    MAX_LINE_BYTES,
    Record,
    SessionMeta,
    assert_under_root,
    is_sensitive_bash,
    is_sensitive_path,
    parse_ts,
)

logger = logging.getLogger("csc")


_SYSTEM_PRIMING_MARKERS = (
    "<environment_context",
    "<user_instructions",
    "<system",
    "# AGENTS.md instructions",
    "# CLAUDE.md instructions",
)


def default_codex_root() -> Path:
    """Where Codex stores rollout JSONLs.

    Precedence: ``$CODEX_SESSIONS_ROOT`` (test/dev), then
    ``$CODEX_HOME/sessions``, then ``~/.codex/sessions``.
    """
    env_root = os.environ.get("CODEX_SESSIONS_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    home = os.environ.get("CODEX_HOME")
    if home:
        return (Path(home).expanduser() / "sessions").resolve()
    return Path("~/.codex/sessions").expanduser().resolve()


def iter_codex_jsonls(root: Path | None = None) -> Iterator[Path]:
    """Yield every Codex rollout JSONL under ``root``, sorted for determinism.

    Codex organizes by ``YYYY/MM/DD/rollout-*.jsonl`` but ``rglob`` accepts
    any depth — we don't tighten the glob so a future Codex layout shift
    (e.g., flatter or deeper) still picks up the files.
    """
    root = root or default_codex_root()
    if not root.exists():
        return
    yield from sorted(root.rglob("rollout-*.jsonl"))


def _iter_raw_lines(path: Path, root: Path) -> Iterator[dict]:
    """Codex-side wrapper around the shared ``iter_raw_lines``-style logic.

    Same body as the Claude version but takes ``root`` required (the
    Codex caller always knows it). Uses the shared ``assert_under_root``
    + ``MAX_LINE_BYTES`` so a tightening of one applies to both.
    """
    resolved = assert_under_root(path, root)
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if len(line) > MAX_LINE_BYTES:
                logger.debug(
                    "codex parser: skipping line >%d bytes in %s",
                    MAX_LINE_BYTES, path,
                )
                continue
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _is_priming(text: str) -> bool:
    """True if a user input_text block is Codex synthetic system priming."""
    if not text:
        return False
    stripped = text.lstrip()
    return any(stripped.startswith(m) for m in _SYSTEM_PRIMING_MARKERS)


def _project_from_cwd(cwd: str) -> str:
    if not cwd:
        return ""
    return Path(cwd).name or "root"


def _extract_args(args_str: str) -> tuple[str, str]:
    """Return ``(command, file_path)`` extracted from a Codex tool's
    ``arguments`` JSON-string.

    One JSON parse, both fields. ``command`` honors ``cmd``/``command``
    and falls back to argv-list joining. ``file_path`` honors
    ``file_path``/``path``/``filename``. Either return value may be ``""``.
    """
    if not args_str or not isinstance(args_str, str):
        return "", ""
    try:
        d = json.loads(args_str)
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(d, dict):
        return "", ""

    command = ""
    for key in ("cmd", "command"):
        v = d.get(key)
        if isinstance(v, str):
            command = v
            break
        if isinstance(v, list):
            command = " ".join(str(x) for x in v)
            break

    file_path = ""
    for key in ("file_path", "path", "filename"):
        v = d.get(key)
        if isinstance(v, str):
            file_path = v
            break

    return command, file_path


def _flatten_dict_output(output: dict) -> str:
    """Codex sometimes emits a structured-dict output. Flatten to text.

    Only string values are joined; nested structure is silently lost.
    Logged at DEBUG so a future Codex version with rich nested outputs
    surfaces in run.log.
    """
    text = "\n".join(v for v in output.values() if isinstance(v, str))
    skipped = sum(1 for v in output.values() if not isinstance(v, str))
    if skipped:
        logger.debug(
            "codex parser: flattened function_call_output dict, "
            "dropped %d non-string values", skipped,
        )
    return text


def parse_codex_file(path: Path, root: Path | None = None) -> Iterator[Record]:
    """Yield Records from one Codex rollout JSONL, in file order.

    State machine:
      - The current assistant record is **buffered** (not yielded
        immediately) so subsequent ``function_call`` and ``reasoning``
        records can attach their data before the consumer ever sees the
        record. This avoids late mutation of an already-yielded object.
      - ``function_call_output`` records queue into ``pending_results``
        and attach to the **next** user record's ``tool_results``.
      - At EOF, any buffered assistant is yielded; any unattached
        pending_results are dropped with a DEBUG log (no trailing user
        record arrived to receive them).
    """
    root = root or default_codex_root()
    session_id = ""
    project = ""
    cwd_current = ""
    record_index = 0  # ensures unique uuids even within a single timestamp

    tool_name_by_id: dict[str, str] = {}
    dropped_ids: set[str] = set()

    pending_assistant: Record | None = None
    pending_results: list[dict] = []

    def make_uuid() -> str:
        nonlocal record_index
        u = f"{session_id or 'unknown'}:{record_index:06d}"
        record_index += 1
        return u

    def flush_assistant() -> Iterator[Record]:
        nonlocal pending_assistant
        if pending_assistant is not None:
            yield pending_assistant
            pending_assistant = None

    for raw in _iter_raw_lines(path, root):
        rtype = raw.get("type", "")
        ts = parse_ts(raw)
        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type", "")

        if rtype == "session_meta":
            session_id = payload.get("id", "") or session_id
            cwd_current = payload.get("cwd", "") or cwd_current
            project = _project_from_cwd(cwd_current) or project
            continue

        if rtype == "turn_context":
            cwd_current = payload.get("cwd", "") or cwd_current
            project = _project_from_cwd(cwd_current) or project
            continue

        if rtype == "event_msg":
            if ptype == "user_message":
                # Redundant with response_item.message,role=user. Trip-wire
                # logged in case a future Codex version populates these.
                tels = payload.get("text_elements") or []
                imgs = payload.get("images") or []
                limgs = payload.get("local_images") or []
                if tels or imgs or limgs:
                    logger.debug(
                        "codex parser: event_msg.user_message has rich payload "
                        "(text_elements=%d, images=%d, local_images=%d) — content "
                        "may be lost. See ADR-0005.",
                        len(tels), len(imgs), len(limgs),
                    )
            # All event_msg.* are runtime telemetry; skip.
            continue

        if rtype != "response_item":
            logger.debug("codex parser: unknown top-level type %r", rtype)
            continue

        if ptype == "function_call":
            # Mutates the (still-buffered) pending_assistant — safe because
            # it hasn't been yielded yet.
            if pending_assistant is None:
                logger.debug(
                    "codex parser: orphan function_call (no preceding "
                    "assistant) in %s", path,
                )
                continue
            name = payload.get("name", "?")
            call_id = payload.get("call_id", "")
            args_str = payload.get("arguments", "")
            cmd, fp = _extract_args(args_str)
            tool_name_by_id[call_id] = name
            drop = (cmd and is_sensitive_bash(cmd)) or (
                fp and is_sensitive_path(fp)
            )
            if drop:
                dropped_ids.add(call_id)
            pending_assistant.tool_calls.append({
                "id": call_id,
                "name": name,
                "input": {"command": cmd, "file_path": fp} if (cmd or fp) else {},
                "dropped": bool(drop),
            })
            continue

        if ptype == "function_call_output":
            call_id = payload.get("call_id", "")
            if call_id not in tool_name_by_id:
                # Output without a registered call: don't fabricate a
                # parent — drop and log. Skipping is safer than queueing
                # with tool_name="?" which would emit redaction-bypassed
                # content if the missing call would have been sensitive.
                logger.debug(
                    "codex parser: orphan function_call_output (call_id=%r "
                    "not registered) in %s", call_id, path,
                )
                continue
            output = payload.get("output", "")
            if isinstance(output, dict):
                output = _flatten_dict_output(output)
            elif not isinstance(output, str):
                output = str(output)
            dropped = call_id in dropped_ids
            pending_results.append({
                "tool_use_id": call_id,
                "tool_name": tool_name_by_id[call_id],
                "content": "" if dropped else output,
                "is_error": False,
                "dropped": dropped,
            })
            continue

        if ptype == "reasoning":
            if pending_assistant is None:
                continue  # orphan reasoning, ignore
            summary = payload.get("summary") or []
            if isinstance(summary, list):
                for s in summary:
                    if isinstance(s, str):
                        pending_assistant.thinking.append(s)
            continue

        if ptype == "message":
            role = payload.get("role", "")
            if role == "developer":
                continue  # system-prompt-ish

            # New message arriving — flush any buffered assistant.
            yield from flush_assistant()

            content = payload.get("content") or []
            text_parts: list[str] = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") in ("input_text", "output_text", "text"):
                        text = block.get("text", "")
                        if role == "user" and _is_priming(text):
                            continue
                        if text:
                            text_parts.append(text)
            text = "\n".join(text_parts)

            if role == "user":
                rec = Record(
                    session_id=session_id,
                    timestamp=ts,
                    kind="user",
                    content=text,
                    uuid=make_uuid(),
                    parent_uuid="",
                    is_sidechain=False,
                    cwd=cwd_current,
                    project=project,
                    tool_calls=[],
                    tool_results=pending_results,
                    thinking=[],
                    raw_type="response_item/message/user",
                    source="codex",
                )
                pending_results = []
                yield rec
            else:  # assistant
                pending_assistant = Record(
                    session_id=session_id,
                    timestamp=ts,
                    kind="assistant",
                    content=text,
                    uuid=make_uuid(),
                    parent_uuid="",
                    is_sidechain=False,
                    cwd=cwd_current,
                    project=project,
                    tool_calls=[],
                    tool_results=[],
                    thinking=[],
                    raw_type="response_item/message/assistant",
                    source="codex",
                )
            continue

        logger.debug("codex parser: unknown response_item subtype %r", ptype)

    # EOF flush.
    yield from flush_assistant()
    if pending_results:
        # Tool outputs queued for a user message that never arrived.
        # Dropping is safer than fabricating a synthetic record;
        # structural drops have already removed sensitive content.
        logger.debug(
            "codex parser: %d unattached tool_result(s) at EOF in %s",
            len(pending_results), path,
        )


def collect_codex_meta(
    path: Path, root: Path | None = None
) -> dict[str, SessionMeta]:
    """Fast meta scan for a Codex rollout JSONL.

    Standalone for tests and meta-only callers; production flows that
    need both records and meta should derive meta from the records
    yielded by ``parse_codex_file`` to avoid a second file pass.
    """
    root = root or default_codex_root()
    out: dict[str, SessionMeta] = {}
    session_id = ""
    first_prompt: str | None = None

    for raw in _iter_raw_lines(path, root):
        rtype = raw.get("type", "")
        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            continue

        if rtype == "session_meta":
            session_id = payload.get("id", "") or session_id

        if first_prompt is None and rtype == "response_item":
            if payload.get("type") == "message" and payload.get("role") == "user":
                content = payload.get("content") or []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") in ("input_text", "text"):
                            text = block.get("text", "")
                            if text and not _is_priming(text):
                                first_prompt = text
                                break

    if session_id:
        out[session_id] = SessionMeta(
            session_id=session_id,
            custom_title=None,
            agent_name=None,
            first_prompt=first_prompt,
        )
    return out
