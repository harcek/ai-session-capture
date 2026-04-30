"""Stream-parse Claude Code JSONL transcripts into normalized records.

Structural drops run at parse time: invocations of commands like
`env`, `printenv`, `cat .env`, or Reads of credential files have their
subsequent ``tool_result`` blanked out before the content is ever handed
downstream. This is the strongest layer of the security posture — regex
redaction in ``redact.py`` is a second line of defense, not the first.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MAX_LINE_BYTES = 10 * 1024 * 1024  # 10 MiB; longer lines are skipped


def default_projects_root() -> Path:
    """Where Claude Code stores JSONL transcripts.

    Derives from Claude Code's own canonicals rather than introducing a
    second source of truth. Precedence:

    1. ``$CLAUDE_PROJECTS_ROOT`` env var — **test/dev hook only**, not
       documented for end users. Takes highest precedence so test
       harnesses can redirect without fighting the CLI/flag layer.
    2. ``$CLAUDE_CONFIG_DIR/projects`` — honors Claude Code's own
       config-dir override. If the user has relocated their entire
       ``~/.claude`` tree via ``CLAUDE_CONFIG_DIR``, this tool follows.
    3. ``~/.claude/projects`` — the Claude Code default.

    A CLI ``--projects-root PATH`` flag (handled at the call site)
    overrides all of the above for one-off imports or debugging. There
    is no TOML config knob — see ``docs/adr/0004-derive-dont-configure-
    claude-root.md``.
    """
    env_root = os.environ.get("CLAUDE_PROJECTS_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return (Path(config_dir).expanduser() / "projects").resolve()
    return Path("~/.claude/projects").expanduser().resolve()


SENSITIVE_BASH = re.compile(
    r"""
    (?:^|[\s;|&])
    (?:
        env\s*(?:$|\|)                 |
        printenv\b                     |
        set\s*(?:$|\|)                 |
        export\s*(?:$|\|)              |
        cat\s+[^\s]*
          (?:\.env(?:\..+)?|\.netrc|\.pgpass|id_rsa|id_ed25519|\.pem|\.key|credentials(?:\.json)?)
                                        |
        aws\s+(?:configure|sts\s+get-caller-identity)
                                        |
        gh\s+auth\s+(?:token|status)    |
        op\s+read\s                     |
        security\s+find-generic-password
                                        |
        kubectl\s+(?:.*\s)?secret       |
        doppler\s+secrets               |
        vault\s+(?:kv|read)\s           |
        gcloud\s+auth\s+(?:print|login|application-default\s+print)
                                        |
        az\s+account\s+get-access-token
                                        |
        heroku\s+config
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

SENSITIVE_PATH = re.compile(
    r"""
    (?:^|/)
    (?:
        \.env(?:\..+)?$
        | \.aws(?:/|$)
        | \.ssh(?:/|$)
        | \.gnupg(?:/|$)
        | \.config/gh(?:/|$)
        | \.netrc$
        | \.pgpass$
        | \.npmrc$
        | \.pypirc$
        | credentials(?:\.json)?$
        | secrets?\.(?:ya?ml|json|toml)$
        | id_(?:rsa|ed25519|ecdsa|dsa)(?:\.pub)?$
        | [^/]+\.pem$
        | [^/]+\.p12$
        | [^/]+\.pfx$
        | \.kube/config$
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_sensitive_bash(command: str) -> bool:
    return bool(command and SENSITIVE_BASH.search(command))


def is_sensitive_path(file_path: str) -> bool:
    return bool(file_path and SENSITIVE_PATH.search(file_path))


@dataclass
class Record:
    """One normalized line from a JSONL transcript, downstream-ready.

    ``source`` discriminates between agent-tool adapters (claude / codex /
    opencode / …). Defaults to "claude" for backwards-compat across any
    pre-existing FTS rows or callers that don't specify a source.
    See ADR-0005.
    """

    session_id: str
    timestamp: datetime | None
    kind: str  # "user" | "assistant" | "slash_command" | "attachment"
    content: str
    uuid: str = ""
    parent_uuid: str = ""
    is_sidechain: bool = False
    cwd: str = ""
    project: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)
    raw_type: str = ""
    source: str = "claude"


@dataclass
class SessionMeta:
    """Session-level metadata derived from JSONL meta records.

    Populated by :func:`collect_session_meta`. Exists so the render layer
    can name session files after a human title (via ``/rename``) or the
    first substantive user prompt, rather than falling back to the UUID.
    """

    session_id: str
    custom_title: str | None = None
    agent_name: str | None = None
    first_prompt: str | None = None


def _assert_under_root(path: Path, root: Path) -> Path:
    """Refuse to read JSONLs outside ``root`` (path-traversal + symlink guard)."""
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError(f"refusing to read JSONL outside {root}: {resolved}")
    st = os.lstat(resolved)
    if st.st_uid != os.getuid():
        raise ValueError(f"refusing to read non-owned file: {resolved}")
    return resolved


def iter_raw_lines(path: Path, root: Path | None = None) -> Iterator[dict]:
    """Yield parsed JSON dicts line by line. Malformed lines skip silently."""
    root = root or default_projects_root()
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


def _derive_project(jsonl_path: Path) -> str:
    """Recover a human-ish project name from Claude Code's cwd-encoded dir.

    ``~/.claude/projects/-home-openclaw--openclaw-workspace-projects-deep-value-scanner/``
    → ``deep-value-scanner``. Returns ``"root"`` when the encoded path has no
    trailing segment (i.e., the session ran directly in ``$HOME``).
    """
    name = jsonl_path.parent.name
    parts = [p for p in name.lstrip("-").split("--") if p]
    return parts[-1] if parts else "root"


def _walk_content(
    content: object,
    tool_name_by_id: dict[str, str],
    dropped_ids: set[str],
) -> tuple[str, list[dict], list[dict], list[str]]:
    """Flatten a message.content field into text + tool calls + results + thinking."""
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    thinking: list[str] = []

    if isinstance(content, str):
        return content, [], [], []
    if not isinstance(content, list):
        return "", [], [], []

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "thinking":
            thinking.append(block.get("thinking", ""))
        elif btype == "tool_use":
            name = block.get("name", "?")
            tool_id = block.get("id", "")
            inp = block.get("input") or {}
            tool_name_by_id[tool_id] = name
            drop = False
            if name == "Bash" and is_sensitive_bash(str(inp.get("command", ""))):
                drop = True
            elif name == "Read" and is_sensitive_path(str(inp.get("file_path", ""))):
                drop = True
            if drop:
                dropped_ids.add(tool_id)
            tool_calls.append(
                {"id": tool_id, "name": name, "input": inp, "dropped": drop}
            )
        elif btype == "tool_result":
            tid = block.get("tool_use_id", "")
            dropped = tid in dropped_ids
            tool_results.append(
                {
                    "tool_use_id": tid,
                    "tool_name": tool_name_by_id.get(tid, "?"),
                    "content": "" if dropped else block.get("content", ""),
                    "is_error": bool(block.get("is_error")),
                    "dropped": dropped,
                }
            )

    return "\n".join(t for t in text_parts if t), tool_calls, tool_results, thinking


def parse_file(path: Path, root: Path | None = None) -> Iterator[Record]:
    """Yield Records from one JSONL file, in file order.

    Tool-call/tool-result pairs are matched across lines within the file:
    a ``tool_use`` on line N registers its id, and the ``tool_result`` on a
    later line with matching ``tool_use_id`` is dropped if the original call
    was flagged sensitive.
    """
    project = _derive_project(path)
    tool_name_by_id: dict[str, str] = {}
    dropped_ids: set[str] = set()

    for raw in iter_raw_lines(path, root=root):
        rtype = raw.get("type", "")
        ts = _parse_ts(raw)
        sid = raw.get("sessionId", "")
        uuid = raw.get("uuid", "")
        parent = raw.get("parentUuid") or ""
        sidechain = bool(raw.get("isSidechain", False))
        cwd = raw.get("cwd", "")

        if rtype in ("user", "assistant"):
            msg = raw.get("message") or {}
            text, calls, results, thinking = _walk_content(
                msg.get("content", ""), tool_name_by_id, dropped_ids
            )
            yield Record(
                session_id=sid,
                timestamp=ts,
                kind=rtype,
                content=text,
                uuid=uuid,
                parent_uuid=parent,
                is_sidechain=sidechain,
                cwd=cwd,
                project=project,
                tool_calls=calls,
                tool_results=results,
                thinking=thinking,
                raw_type=rtype,
            )
        elif rtype == "system" and raw.get("subtype") == "local_command":
            yield Record(
                session_id=sid,
                timestamp=ts,
                kind="slash_command",
                content=str(raw.get("content", "")),
                uuid=uuid,
                parent_uuid=parent,
                is_sidechain=sidechain,
                cwd=cwd,
                project=project,
                raw_type="system/local_command",
            )
        elif rtype == "attachment":
            att = raw.get("attachment") or {}
            yield Record(
                session_id=sid,
                timestamp=ts,
                kind="attachment",
                content=f"[attachment: {att.get('type', '?')}]",
                uuid=uuid,
                parent_uuid=parent,
                is_sidechain=sidechain,
                cwd=cwd,
                project=project,
                raw_type="attachment",
            )
        # silently skipped types: system/turn_duration, system/away_summary,
        # permission-mode, file-history-snapshot, last-prompt, custom-title,
        # agent-name, queue-operation


def iter_jsonls(root: Path | None = None) -> Iterator[Path]:
    """Yield every readable JSONL under ``root`` (defaults to the real projects dir)."""
    root = root or default_projects_root()
    if not root.exists():
        return
    yield from sorted(root.glob("*/*.jsonl"))


def collect_session_meta(
    path: Path, root: Path | None = None
) -> dict[str, SessionMeta]:
    """Fast scan for session-level metadata — custom-title, agent-name, first prompt.

    ``custom-title`` records fire repeatedly as the title is refreshed; we
    keep the last one seen. The first user prompt is the first ``type=user``
    record whose content is substantive prose — we skip turns that start
    with an angle-bracket tag (``<local-command-stdout>``,
    ``<command-name>``, ``<local-command-caveat>``, etc.), which are
    Claude Code's mechanical wrappers around slash-command output, not
    real user prompts.
    """
    out: dict[str, SessionMeta] = {}
    for raw in iter_raw_lines(path, root=root):
        sid = raw.get("sessionId") or ""
        if not sid:
            continue
        meta = out.setdefault(sid, SessionMeta(session_id=sid))
        rtype = raw.get("type")

        if rtype == "custom-title":
            title = raw.get("customTitle")
            if title:
                meta.custom_title = title  # always take the latest

        elif rtype == "agent-name":
            name = raw.get("agentName")
            if name:
                meta.agent_name = name

        elif rtype == "user" and meta.first_prompt is None:
            msg = raw.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            break
            stripped = text.lstrip() if text else ""
            if stripped and not stripped.startswith("<"):
                meta.first_prompt = text
    return out
