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
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from .parser import (
    SENSITIVE_BASH,
    SENSITIVE_PATH,
    Record,
    SessionMeta,
    is_sensitive_bash,
    is_sensitive_path,
)

MAX_LINE_BYTES = 10 * 1024 * 1024  # match Claude parser's cap

logger = logging.getLogger("csc")


# Codex's first user record per session contains synthetic system priming
# (AGENTS.md + environment_context blocks). Skip blocks whose stripped
# text starts with one of these markers — same idiom as the Claude
# parser's <command-name> / <local-command-*> skip in collect_session_meta.
_SYSTEM_PRIMING_MARKERS = (
    "<environment_context",
    "<user_instructions",
    "<system",
    "# AGENTS.md instructions",
    "# CLAUDE.md instructions",
)


def default_codex_root() -> Path:
    """Where Codex stores rollout JSONLs.

    Precedence:
      1. ``$CODEX_SESSIONS_ROOT`` (test/dev hook, undocumented for users)
      2. ``$CODEX_HOME/sessions`` (Codex's own override)
      3. ``~/.codex/sessions`` (default)
    """
    env_root = os.environ.get("CODEX_SESSIONS_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    home = os.environ.get("CODEX_HOME")
    if home:
        return (Path(home).expanduser() / "sessions").resolve()
    return Path("~/.codex/sessions").expanduser().resolve()


def iter_codex_jsonls(root: Path | None = None) -> Iterator[Path]:
    """Yield every Codex rollout JSONL recursively under ``root``.

    Walks YYYY/MM/DD/ subdirs. Returns sorted paths so iteration order
    is deterministic across runs (matters for the FTS upsert).
    """
    root = root or default_codex_root()
    if not root.exists():
        return
    yield from sorted(root.rglob("rollout-*.jsonl"))


def _assert_under_root(path: Path, root: Path) -> Path:
    """Refuse to read JSONLs outside ``root`` (path-traversal + symlink guard)."""
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError(f"refusing to read JSONL outside {root}: {resolved}")
    st = os.lstat(resolved)
    if st.st_uid != os.getuid():
        raise ValueError(f"refusing to read non-owned file: {resolved}")
    return resolved


def _iter_raw_lines(path: Path, root: Path) -> Iterator[dict]:
    """Yield parsed JSON dicts line by line. Mirrors the Claude parser's helper."""
    resolved = _assert_under_root(path, root)
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if len(line) > MAX_LINE_BYTES:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _parse_ts(raw: dict) -> datetime | None:
    ts = raw.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _is_priming(text: str) -> bool:
    """True if a user input_text block looks like Codex system priming."""
    if not text:
        return False
    stripped = text.lstrip()
    return any(stripped.startswith(m) for m in _SYSTEM_PRIMING_MARKERS)


def _project_from_cwd(cwd: str) -> str:
    """Last path segment, like Claude — but Codex stores cwd cleanly."""
    if not cwd:
        return ""
    return Path(cwd).name or "root"


def _command_from_args(args_str: str) -> str:
    """Extract the actual command from a Codex function_call's arguments JSON.

    Codex's ``exec_command`` tool stores the shell command in
    ``arguments.cmd`` (sometimes ``command``). Other tools use other
    field names — this returns whatever non-empty string field is
    present so downstream redaction has the best chance of seeing it.
    """
    if not args_str or not isinstance(args_str, str):
        return ""
    try:
        d = json.loads(args_str)
    except json.JSONDecodeError:
        return ""
    if not isinstance(d, dict):
        return ""
    for key in ("cmd", "command"):
        v = d.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, list):  # exec_command sometimes uses argv-list form
            return " ".join(str(x) for x in v)
    return ""


def _file_path_from_args(args_str: str) -> str:
    """Pull a file_path out of Codex tool arguments, if present."""
    if not args_str or not isinstance(args_str, str):
        return ""
    try:
        d = json.loads(args_str)
    except json.JSONDecodeError:
        return ""
    if not isinstance(d, dict):
        return ""
    for key in ("file_path", "path", "filename"):
        v = d.get(key)
        if isinstance(v, str):
            return v
    return ""


def parse_codex_file(path: Path, root: Path | None = None) -> Iterator[Record]:
    """Yield Records from one Codex rollout JSONL, in file order.

    Schema dispatch:
      - session_meta            → captured separately by collect_codex_meta
      - turn_context            → updates current cwd (also exposed as a
                                  Record so the renderer can show it)
      - response_item.message,user      → user prompt (skipping priming blocks)
      - response_item.message,assistant → assistant text
      - response_item.message,developer → system-prompt-ish, skipped
      - response_item.function_call     → tool invocation
                                         (paired to next function_call_output)
      - response_item.function_call_output → tool result
      - response_item.reasoning         → thinking blocks (off by default downstream)
      - event_msg.user_message          → skipped (redundant with response_item.message)
                                          BUT logs DEBUG if it carries
                                          text_elements/images (future-proofing)
      - event_msg.*                     → telemetry, skipped
    """
    root = root or default_codex_root()
    session_id = ""
    project = ""
    cwd_current = ""

    # Tool-call/result pairing: same idiom as the Claude parser
    tool_name_by_id: dict[str, str] = {}
    dropped_ids: set[str] = set()

    # We need to attach function_calls/function_call_outputs to the
    # most-recent assistant/user record respectively. Keep refs.
    last_assistant_record: Record | None = None
    pending_results_for_next_user: list[dict] = []

    for raw in _iter_raw_lines(path, root):
        rtype = raw.get("type", "")
        ts = _parse_ts(raw)
        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type", "")

        # session_meta — extract id + cwd, no Record yielded
        if rtype == "session_meta":
            session_id = payload.get("id", "") or session_id
            cwd_current = payload.get("cwd", "") or cwd_current
            project = _project_from_cwd(cwd_current) or project
            continue

        # turn_context — refresh cwd if it changed mid-session
        if rtype == "turn_context":
            cwd_current = payload.get("cwd", "") or cwd_current
            project = _project_from_cwd(cwd_current) or project
            continue

        # response_item → message / function_call / function_call_output / reasoning
        if rtype == "response_item":
            if ptype == "message":
                role = payload.get("role", "")
                if role == "developer":
                    continue  # system-prompt-ish, skip

                content = payload.get("content") or []
                text_parts: list[str] = []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type", "")
                        if btype in ("input_text", "output_text", "text"):
                            text = block.get("text", "")
                            if role == "user" and _is_priming(text):
                                continue  # skip system-priming blocks
                            if text:
                                text_parts.append(text)
                text = "\n".join(text_parts)

                # Attach pending tool_results to this user record
                tool_results = []
                if role == "user" and pending_results_for_next_user:
                    tool_results = pending_results_for_next_user
                    pending_results_for_next_user = []

                rec = Record(
                    session_id=session_id,
                    timestamp=ts,
                    kind="user" if role == "user" else "assistant",
                    content=text,
                    uuid=f"{session_id}:{(ts.isoformat() if ts else '')}",
                    parent_uuid="",
                    is_sidechain=False,
                    cwd=cwd_current,
                    project=project,
                    tool_calls=[],
                    tool_results=tool_results,
                    thinking=[],
                    raw_type=f"response_item/message/{role}",
                    source="codex",
                )
                if role == "assistant":
                    last_assistant_record = rec
                yield rec
                continue

            if ptype == "function_call":
                name = payload.get("name", "?")
                call_id = payload.get("call_id", "")
                args_str = payload.get("arguments", "")
                tool_name_by_id[call_id] = name

                # Sensitive-tool detection: parse the args JSON.
                # Codex's exec_command is the Bash equivalent; map either
                # the cmd or the file_path through our existing regex.
                drop = False
                cmd = _command_from_args(args_str)
                fp = _file_path_from_args(args_str)
                if cmd and is_sensitive_bash(cmd):
                    drop = True
                if fp and is_sensitive_path(fp):
                    drop = True
                if drop:
                    dropped_ids.add(call_id)

                tool_call = {
                    "id": call_id,
                    "name": name,
                    "input": {"command": cmd, "file_path": fp} if (cmd or fp) else {},
                    "dropped": drop,
                }
                if last_assistant_record is not None:
                    last_assistant_record.tool_calls.append(tool_call)
                # else: orphaned function_call (no preceding assistant);
                # skip rather than fabricate a parent
                continue

            if ptype == "function_call_output":
                call_id = payload.get("call_id", "")
                output = payload.get("output", "")
                if isinstance(output, dict):
                    # Codex sometimes emits a structured output dict;
                    # flatten any string-valued key for downstream redaction.
                    output = "\n".join(
                        v for v in output.values() if isinstance(v, str)
                    )
                if not isinstance(output, str):
                    output = str(output)
                dropped = call_id in dropped_ids
                pending_results_for_next_user.append({
                    "tool_use_id": call_id,
                    "tool_name": tool_name_by_id.get(call_id, "?"),
                    "content": "" if dropped else output,
                    "is_error": False,
                    "dropped": dropped,
                })
                continue

            if ptype == "reasoning":
                # Thinking-equivalent. Off by default in the renderer; we
                # attach to the most recent assistant record so the
                # downstream filter applies uniformly.
                if last_assistant_record is not None:
                    summary = payload.get("summary") or []
                    if isinstance(summary, list):
                        for s in summary:
                            if isinstance(s, str):
                                last_assistant_record.thinking.append(s)
                continue

            # Unknown response_item subtype — log + skip
            logger.debug("codex parser: unknown response_item subtype %r", ptype)
            continue

        # event_msg.* — runtime telemetry (mostly skipped)
        if rtype == "event_msg":
            if ptype == "user_message":
                # Redundant with response_item.message,role=user. But guard
                # against future Codex versions populating text_elements
                # or images — log loudly so we see it in run.log if it
                # ever happens. See ADR-0005.
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
                continue
            # All other event_msg types (agent_message, task_started,
            # task_complete, token_count, …) are pure telemetry. Skip.
            continue

        # Unknown top-level type — log + skip
        logger.debug("codex parser: unknown top-level type %r", rtype)


def collect_codex_meta(
    path: Path, root: Path | None = None
) -> dict[str, SessionMeta]:
    """Fast meta scan for a Codex rollout JSONL.

    Codex doesn't have a /rename custom-title concept. We populate
    ``first_prompt`` from the first non-priming user input_text block.
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
