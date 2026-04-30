"""Render normalized records into session files + daily index files.

Two output shapes:

- ``render_session_file`` — one Markdown file for an entire session
  (possibly spanning multiple days), sorted chronologically. This is the
  canonical "content" file.
- ``render_daily_index`` — one Markdown file per calendar day, acting as
  a timeline that wiki-links to every session that touched that day.

A shared ``RedactionReport`` threads through every text scrub so the
warning block in each output reflects exactly what's in that document.
Rendering is deterministic: same inputs produce byte-identical output,
which is what the state layer's content-hash idempotency depends on.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from .config import Config
from .layout import (
    SessionNaming,
    daily_index_relpath,
    sanitize_project,
    session_relpath,
)
from .parser import Record, SessionMeta
from .redact import RedactionReport, redact


def _fmt_time(ts: datetime | None, tz: ZoneInfo | None) -> str:
    if not ts:
        return "--:--:--"
    if tz:
        ts = ts.astimezone(tz)
    return ts.strftime("%H:%M:%S")


def _fmt_datetime(ts: datetime | None, tz: ZoneInfo | None) -> str:
    if not ts:
        return "—"
    if tz:
        ts = ts.astimezone(tz)
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _to_local_date(ts: datetime | None, tz: ZoneInfo | None) -> date | None:
    if not ts:
        return None
    if tz:
        ts = ts.astimezone(tz)
    return ts.date()


def resolve_tz(cfg: Config) -> ZoneInfo:
    """Return a ZoneInfo per config, or the system's local TZ in auto mode."""
    if cfg.timezone.mode == "explicit" and cfg.timezone.name:
        return ZoneInfo(cfg.timezone.name)
    try:
        link = Path("/etc/localtime")
        if link.is_symlink():
            target = link.resolve()
            parts = target.parts
            if "zoneinfo" in parts:
                i = parts.index("zoneinfo")
                return ZoneInfo("/".join(parts[i + 1 :]))
        import time

        name = time.tzname[0] if time.tzname else ""
        if name and name not in ("UTC", ""):
            try:
                return ZoneInfo(name)
            except Exception:
                pass
    except Exception:
        pass
    return ZoneInfo("UTC")


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n\n_…[truncated {len(text) - limit} chars]_"


def _summarize_tool_input(name: str, inp: dict) -> str:
    if not isinstance(inp, dict) or not inp:
        return ""
    if name == "Bash":
        cmd = str(inp.get("command", ""))
        return cmd.splitlines()[0][:120] if cmd else ""
    if name in ("Read", "Edit", "Write"):
        return str(inp.get("file_path", ""))[:120]
    if name == "Grep":
        return f'"{str(inp.get("pattern", ""))[:60]}"'
    if name == "Glob":
        return str(inp.get("pattern", ""))[:120]
    for v in inp.values():
        if isinstance(v, str):
            return v.splitlines()[0][:120] if v else ""
    return ""


def _build_turn(
    r: Record, cfg: Config, report: RedactionReport, tz: ZoneInfo
) -> dict:
    content = r.content or ""
    if cfg.redaction.enabled and content:
        content, _ = redact(content, report)
    content = _truncate(content, cfg.formatting.max_message_chars)

    tool_calls_display: list[str] = []
    if r.tool_calls and cfg.content.tool_calls != "off":
        for tc in r.tool_calls:
            if tc["dropped"]:
                tool_calls_display.append(f"~~{tc['name']}~~ (dropped: sensitive)")
            else:
                preview = _summarize_tool_input(tc["name"], tc["input"])
                label = f"{tc['name']}"
                if preview:
                    label += f": `{preview}`"
                tool_calls_display.append(label)

    tool_results_display: list[dict] = []
    if r.tool_results and cfg.content.tool_results != "off":
        for tr in r.tool_results:
            if tr["dropped"]:
                tool_results_display.append(
                    {"tool_name": tr["tool_name"], "dropped": True, "preview": ""}
                )
                continue
            result_text = tr["content"]
            if isinstance(result_text, list):
                parts = [b.get("text", "") for b in result_text if isinstance(b, dict)]
                result_text = "\n".join(p for p in parts if p)
            if not isinstance(result_text, str):
                result_text = str(result_text)
            if cfg.redaction.enabled and result_text:
                result_text, _ = redact(result_text, report)
            if cfg.content.tool_results == "summary":
                first_line = result_text.strip().splitlines()[:1]
                preview_line = first_line[0][:180] if first_line else ""
                if len(result_text) > len(preview_line):
                    preview_line += f" … [+{len(result_text) - len(preview_line)} chars]"
            else:
                preview_line = _truncate(result_text, cfg.formatting.max_message_chars)
            tool_results_display.append(
                {
                    "tool_name": tr["tool_name"],
                    "dropped": False,
                    "is_error": tr["is_error"],
                    "preview": preview_line,
                }
            )

    local_ts = r.timestamp.astimezone(tz) if r.timestamp else None
    full_time = (
        local_ts.strftime("%Y-%m-%d %H:%M:%S") if local_ts else "—"
    )
    return {
        "kind": r.kind,
        "full_time": full_time,
        "content": content,
        "tool_calls_display": tool_calls_display,
        "tool_results_display": tool_results_display,
        "is_sidechain": r.is_sidechain,
    }


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


@dataclass
class SessionRender:
    """Output of ``render_session_file`` for one session."""

    session_id: str
    project: str
    relpath: PurePosixPath
    markdown: str
    report: RedactionReport
    first_ts: datetime | None
    last_ts: datetime | None
    turn_count: int
    dates_touched: list[date]


def render_session_file(
    session_id: str,
    records: Iterable[Record],
    meta: SessionMeta | None,
    cfg: Config,
    tz: ZoneInfo | None = None,
) -> SessionRender:
    """Render a whole session (possibly across days) as one Markdown file."""
    tz = tz or resolve_tz(cfg)
    report = RedactionReport()

    # Filter + sort
    filtered: list[Record] = []
    sidechain_count = 0
    for r in records:
        if r.session_id and r.session_id != session_id:
            continue
        if r.is_sidechain and cfg.content.sidechain == "off":
            continue
        if r.is_sidechain and cfg.content.sidechain == "summary":
            sidechain_count += 1
            continue
        if r.kind == "slash_command" and not cfg.content.slash_commands:
            continue
        filtered.append(r)

    filtered.sort(
        key=lambda r: (
            r.timestamp or datetime.min.replace(tzinfo=UTC),
            r.uuid,
        )
    )

    turns = [_build_turn(r, cfg, report, tz) for r in filtered]

    first_ts = next((r.timestamp for r in filtered if r.timestamp), None)
    last_ts = next((r.timestamp for r in reversed(filtered) if r.timestamp), None)
    project_raw = next((r.project for r in filtered if r.project), "") or (
        meta.session_id[:8] if meta else ""
    )
    project = sanitize_project(project_raw, cfg)
    cwd = next((r.cwd for r in filtered if r.cwd), "")

    dates_touched = sorted(
        {d for d in (_to_local_date(r.timestamp, tz) for r in filtered) if d}
    )

    counts_fmt = (
        ", ".join(f"{k}={v}" for k, v in sorted(report.counts.items()))
        if report.counts
        else ""
    )

    # Redact meta fields BEFORE they touch filenames, frontmatter, or the title
    # header — these strings come from user-typed content and can contain
    # the same secrets that the body redaction catches. Use a throwaway
    # report so we don't double-count the same leak in the warning banner.
    throwaway = RedactionReport()
    safe_title_source = None
    if meta and meta.custom_title:
        safe_title_source, _ = redact(meta.custom_title, throwaway)
    safe_first_prompt = None
    if meta and meta.first_prompt:
        safe_first_prompt, _ = redact(meta.first_prompt, throwaway)
    # Same principle for cwd — a path like /home/x/project-sk-ant-… would
    # otherwise leak both into the frontmatter and into the header line.
    safe_cwd = ""
    if cwd:
        safe_cwd, _ = redact(cwd, throwaway)

    title = (safe_title_source or safe_first_prompt or "")[:80]
    if title:
        title = title.replace("\n", " ").strip()

    md = _jinja_env().get_template("session.md.j2").render(
        session_id=session_id,
        session_id_short=session_id[:8] if session_id else "unknown",
        project=project,
        title=title,
        started_at=_fmt_datetime(first_ts, tz),
        ended_at=_fmt_datetime(last_ts, tz),
        dates=[d.isoformat() for d in dates_touched],
        cwd=safe_cwd,
        turns=turns,
        turn_count=len(turns),
        sidechain_count=sidechain_count,
        redactions={
            "total": report.total(),
            "counts": report.counts,
            "counts_fmt": counts_fmt,
        },
        frontmatter_enabled=cfg.output.frontmatter.enabled,
    )

    # source is uniform within a session (set by the parser); pull it from
    # any record. Falls back to "claude" via Record's default.
    source = next((r.source for r in filtered if r.source), "claude")
    naming = SessionNaming(
        session_id=session_id,
        project_raw=project_raw,
        first_ts=first_ts,
        custom_title=safe_title_source,
        first_prompt=safe_first_prompt,
        source=source,
    )
    relpath = session_relpath(naming, cfg, tz)

    return SessionRender(
        session_id=session_id,
        project=project,
        relpath=relpath,
        markdown=md,
        report=report,
        first_ts=first_ts,
        last_ts=last_ts,
        turn_count=len(turns),
        dates_touched=dates_touched,
    )


@dataclass
class DailyIndexRender:
    """Output of ``render_daily_index`` for one local date."""

    for_date: date
    relpath: PurePosixPath
    markdown: str


def render_daily_index(
    for_date: date,
    session_renders: Iterable[SessionRender],
    cfg: Config,
    tz: ZoneInfo | None = None,
    *,
    all_records: Iterable[Record] | None = None,
) -> DailyIndexRender:
    """Build the daily index that backlinks to every session touching ``for_date``.

    ``session_renders`` is expected to contain every session that touched the
    target date (caller filters). ``all_records`` is optional; when
    provided, we use it to compute per-day turn counts for the index.
    """
    tz = tz or resolve_tz(cfg)

    # Per-session turn counts + first-turn-on-this-day times
    turn_counts_today: dict[str, int] = {}
    first_turn_today: dict[str, datetime] = {}
    report_day = RedactionReport()
    if all_records is not None:
        for r in all_records:
            if not r.timestamp:
                continue
            if r.timestamp.astimezone(tz).date() != for_date:
                continue
            if r.is_sidechain and cfg.content.sidechain == "off":
                continue
            if r.is_sidechain and cfg.content.sidechain == "summary":
                continue
            if r.kind == "slash_command" and not cfg.content.slash_commands:
                continue
            turn_counts_today[r.session_id] = turn_counts_today.get(r.session_id, 0) + 1
            prev = first_turn_today.get(r.session_id)
            if prev is None or r.timestamp < prev:
                first_turn_today[r.session_id] = r.timestamp
            # Run redaction to collect counts (same RedactionReport semantics
            # as session files — the index warning block reflects the day's
            # aggregate, not each session's individual counts)
            if cfg.redaction.enabled:
                if r.content:
                    redact(r.content, report_day)
                for tr in r.tool_results or []:
                    if tr.get("dropped"):
                        continue
                    t = tr.get("content", "")
                    if isinstance(t, list):
                        t = "\n".join(b.get("text", "") for b in t if isinstance(b, dict))
                    if isinstance(t, str) and t:
                        redact(t, report_day)

    # Build session view rows for the template
    sessions_view: list[dict] = []
    for sr in session_renders:
        touches_today = for_date in sr.dates_touched
        if not touches_today:
            continue
        # Earliest turn on this specific day (preferred), falling back to the
        # session's overall first timestamp, then to a dash.
        today_ts = first_turn_today.get(sr.session_id)
        if today_ts is not None:
            first_time = today_ts.astimezone(tz).strftime("%H:%M:%S")
        elif sr.first_ts and sr.first_ts.astimezone(tz).date() == for_date:
            first_time = sr.first_ts.astimezone(tz).strftime("%H:%M:%S")
        else:
            first_time = "—"
        label = sr.session_id[:8]
        # Link target: the relative path from the daily/ subdir up to the session file,
        # minus the .md extension — Obsidian resolves wiki-links by stem.
        # From daily/2026-04-20.md the session file lives at ../sessions/<proj>/<file>.md.
        link_target = "../" + sr.relpath.as_posix()
        if link_target.endswith(".md"):
            link_target = link_target[:-3]
        sessions_view.append(
            {
                "first_time": first_time,
                "project": sr.project,
                "turn_count_today": turn_counts_today.get(sr.session_id, sr.turn_count),
                "link_target": link_target,
                "label": label,
            }
        )

    # Stable ordering: by first_time ascending, then project, then label
    sessions_view.sort(key=lambda s: (s["first_time"], s["project"], s["label"]))

    projects = sorted({s["project"] for s in sessions_view})
    turn_count = sum(s["turn_count_today"] for s in sessions_view)

    counts_fmt = (
        ", ".join(f"{k}={v}" for k, v in sorted(report_day.counts.items()))
        if report_day.counts
        else ""
    )

    md = _jinja_env().get_template("daily_index.md.j2").render(
        date=for_date.isoformat(),
        sessions=sessions_view,
        projects=projects,
        turn_count=turn_count,
        redactions={
            "total": report_day.total(),
            "counts": report_day.counts,
            "counts_fmt": counts_fmt,
        },
        frontmatter_enabled=cfg.output.frontmatter.enabled,
    )

    return DailyIndexRender(
        for_date=for_date,
        relpath=daily_index_relpath(for_date),
        markdown=md,
    )
