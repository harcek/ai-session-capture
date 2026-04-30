"""Command-line interface — the one entrypoint the scheduler calls.

``claude-session-capture daily`` is the cron/timer path; it renders
yesterday's local-date and writes to the output repo, idempotent by
content hash. ``backfill`` walks every historical JSONL and emits a file
per distinct date. ``--dry-run`` and ``--show-redactions`` are inspection
aids; they never touch disk.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import search as search_mod
from .codex_parser import (
    collect_codex_meta,
    default_codex_root,
    iter_codex_jsonls,
    parse_codex_file,
)
from .config import Config, default_config_path
from .parser import (
    SessionMeta,
    collect_session_meta,
    default_projects_root,
    iter_jsonls,
    parse_file,
)
from .render import (
    SessionRender,
    render_daily_index,
    render_session_file,
    resolve_tz,
)
from .state import (
    clear_last_error,
    flock_exclusive,
    notify_failure,
    setup_logging,
    state_dir,
    write_at,
    write_last_error,
)


# Recognized values for the --source CLI flag.
KNOWN_SOURCES = ("claude", "codex")
SOURCE_ALL = "all"


def _resolve_root(args) -> Path:
    """Pick the Claude projects root.

    ``--projects-root`` CLI flag wins if provided; otherwise delegate to
    :func:`parser.default_projects_root` which handles the env var /
    CLAUDE_CONFIG_DIR / default tiers. See ADR-0004.
    """
    if getattr(args, "projects_root", None):
        return Path(args.projects_root).expanduser().resolve()
    return default_projects_root()


def _resolve_sources(args) -> tuple[str, ...]:
    """Return the tuple of source names to ingest for this invocation.

    Defaults to all known sources. ``--source X`` narrows to that one.
    ``--source all`` is the explicit union form. Unknown values raise
    via argparse `choices`.
    """
    raw = getattr(args, "source", None) or SOURCE_ALL
    if raw == SOURCE_ALL:
        return KNOWN_SOURCES
    return (raw,)


def _load_all_records(
    logger: logging.Logger, args, sources: tuple[str, ...]
) -> list:
    """Parse every JSONL across the requested sources; skip individual failures.

    Adapters with a missing root directory contribute zero records
    (``iter_*_jsonls`` returns empty), so on a machine without Codex
    installed, ``--source all`` quietly captures only Claude.
    """
    records: list = []

    if "claude" in sources:
        root = _resolve_root(args)
        for jsonl in iter_jsonls(root):
            try:
                records.extend(parse_file(jsonl, root=root))
            except Exception as e:  # noqa: BLE001
                logger.warning("skipped %s: %s", jsonl, e)

    if "codex" in sources:
        codex_root = default_codex_root()
        for jsonl in iter_codex_jsonls(codex_root):
            try:
                records.extend(parse_codex_file(jsonl, root=codex_root))
            except Exception as e:  # noqa: BLE001
                logger.warning("skipped %s: %s", jsonl, e)

    return records


def _load_all_meta(
    logger: logging.Logger, args, sources: tuple[str, ...]
) -> dict[str, SessionMeta]:
    """Session-level metadata across the requested sources."""
    out: dict[str, SessionMeta] = {}

    if "claude" in sources:
        root = _resolve_root(args)
        for jsonl in iter_jsonls(root):
            try:
                out.update(collect_session_meta(jsonl, root=root))
            except Exception as e:  # noqa: BLE001
                logger.warning("meta-skipped %s: %s", jsonl, e)

    if "codex" in sources:
        codex_root = default_codex_root()
        for jsonl in iter_codex_jsonls(codex_root):
            try:
                out.update(collect_codex_meta(jsonl, root=codex_root))
            except Exception as e:  # noqa: BLE001
                logger.warning("meta-skipped %s: %s", jsonl, e)

    return out


def _render_all_sessions(
    records: list,
    meta: dict[str, SessionMeta],
    cfg: Config,
    tz,
) -> dict[str, SessionRender]:
    """Render one SessionRender per distinct session_id present in records."""
    by_session: dict[str, list] = {}
    for r in records:
        by_session.setdefault(r.session_id or "unknown", []).append(r)
    out: dict[str, SessionRender] = {}
    for sid, recs in by_session.items():
        out[sid] = render_session_file(sid, recs, meta.get(sid), cfg, tz)
    return out


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def cmd_daily(args, cfg: Config, logger: logging.Logger) -> int:
    tz = resolve_tz(cfg)
    target = args.date or (datetime.now(tz).date() - timedelta(days=1))
    sources = _resolve_sources(args)

    records = _load_all_records(logger, args, sources)
    meta = _load_all_meta(logger, args, sources)

    # Identify session IDs that had any turn on the target local-date.
    touching_ids = {
        r.session_id
        for r in records
        if r.timestamp and r.timestamp.astimezone(tz).date() == target
    }
    touching_records = [r for r in records if r.session_id in touching_ids]

    # Render each touching session fully (including turns on other days)
    renders_by_id = _render_all_sessions(touching_records, meta, cfg, tz)
    renders = list(renders_by_id.values())

    # Build the daily index
    day_records = [
        r for r in records
        if r.timestamp and r.timestamp.astimezone(tz).date() == target
    ]
    index = render_daily_index(target, renders, cfg, tz, all_records=day_records)

    if args.dry_run:
        sys.stdout.write(f"# daily index ({index.relpath})\n\n{index.markdown}\n")
        for sr in renders:
            sys.stdout.write(f"# session ({sr.relpath})\n\n{sr.markdown}\n")
        return 0

    if args.show_redactions:
        total_red = sum(sr.report.total() for sr in renders)
        counts: dict[str, int] = {}
        for sr in renders:
            for k, v in sr.report.counts.items():
                counts[k] = counts.get(k, 0) + v
        sys.stdout.write(
            f"{target} — {total_red} redaction(s)"
            + (f": {counts}\n" if counts else "\n")
        )
        return 0

    output_dir = Path(cfg.output.dir).expanduser().resolve()
    wrote_n = 0
    for sr in renders:
        if write_at(output_dir, sr.relpath, sr.markdown, cursor_key=f"session:{sr.session_id}"):
            wrote_n += 1
    # granularity.mode = "session" skips daily index entirely. "daily"
    # is legacy / deprecated — we treat it like "session+daily" with a
    # warning so old configs don't break silently.
    mode = cfg.granularity.mode
    if mode == "daily":
        logger.warning(
            "granularity.mode=\"daily\" is deprecated; treating as session+daily"
        )
    if mode == "session":
        logger.info("daily %s: %d sessions (%d written), no index (mode=session)",
                    target, len(renders), wrote_n)
    else:
        idx_wrote = write_at(
            output_dir, index.relpath, index.markdown, cursor_key=f"daily:{target.isoformat()}"
        )
        logger.info(
            "daily %s: %d sessions (%d written), index %s",
            target,
            len(renders),
            wrote_n,
            "written" if idx_wrote else "unchanged",
        )

    # Refresh FTS index: for any session that touched the target date,
    # re-index ALL of its dates — a cross-day session's row on an earlier
    # date might otherwise drift out of sync when new turns land today.
    rows = search_mod.build_session_rows(touching_records, cfg, tz)
    ins, skip, orphans = search_mod.upsert_rows(rows)
    logger.info(
        "index %s: %d upserted, %d unchanged, %d orphans cleaned",
        target, ins, skip, orphans,
    )
    return 0


def cmd_backfill(args, cfg: Config, logger: logging.Logger) -> int:
    tz = resolve_tz(cfg)
    sources = _resolve_sources(args)
    records = _load_all_records(logger, args, sources)
    meta = _load_all_meta(logger, args, sources)
    if not records:
        logger.warning("no records found for sources=%s", ",".join(sources))
        return 0

    renders_by_id = _render_all_sessions(records, meta, cfg, tz)
    renders = list(renders_by_id.values())

    # Which local dates were touched by any session?
    dates_touched: set[date] = set()
    for sr in renders:
        dates_touched.update(sr.dates_touched)

    output_dir = Path(cfg.output.dir).expanduser().resolve()
    sess_written = 0
    sess_unchanged = 0
    idx_written = 0
    idx_unchanged = 0
    total_redactions = sum(sr.report.total() for sr in renders)

    # Write session files
    for sr in renders:
        if args.dry_run:
            logger.info(
                "dry-run session %s: %d chars, %d redactions → %s",
                sr.session_id[:8],
                len(sr.markdown),
                sr.report.total(),
                sr.relpath,
            )
            continue
        if write_at(
            output_dir, sr.relpath, sr.markdown, cursor_key=f"session:{sr.session_id}"
        ):
            sess_written += 1
        else:
            sess_unchanged += 1

    # Write daily indexes (unless granularity.mode says to skip)
    mode = cfg.granularity.mode
    if mode == "daily":
        logger.warning(
            "granularity.mode=\"daily\" is deprecated; treating as session+daily"
        )
    skip_daily = mode == "session"
    if not skip_daily:
        for d in sorted(dates_touched):
            day_records = [
                r
                for r in records
                if r.timestamp and r.timestamp.astimezone(tz).date() == d
            ]
            index = render_daily_index(d, renders, cfg, tz, all_records=day_records)
            if args.dry_run:
                logger.info("dry-run daily %s: %d chars", d, len(index.markdown))
                continue
            if write_at(
                output_dir, index.relpath, index.markdown, cursor_key=f"daily:{d.isoformat()}"
            ):
                idx_written += 1
            else:
                idx_unchanged += 1

    logger.info(
        "backfill done: %d sessions (%d written, %d unchanged), "
        "%d daily indexes (%d written, %d unchanged), %d total redactions",
        len(renders),
        sess_written,
        sess_unchanged,
        len(dates_touched),
        idx_written,
        idx_unchanged,
        total_redactions,
    )

    if not args.dry_run:
        rows = search_mod.build_session_rows(records, cfg, tz)
        ins, skip, orphans = search_mod.upsert_rows(rows)
        logger.info(
            "backfill index: %d upserted, %d unchanged, %d orphans cleaned",
            ins, skip, orphans,
        )
    return 0


def cmd_search(args, cfg: Config, logger: logging.Logger) -> int:
    tz = resolve_tz(cfg)

    if args.rebuild:
        sources = _resolve_sources(args)
        records = _load_all_records(logger, args, sources)
        n = search_mod.rebuild_all(records, cfg, tz)
        logger.info(
            "rebuilt index from scratch: %d sessions across sources=%s",
            n, ",".join(sources),
        )
        return 0

    if not args.query:
        sys.stderr.write("error: search needs a QUERY or --rebuild\n")
        return 2

    # For querying the index, --source narrows to a single adapter (or
    # None = union). "all" maps to None.
    source_filter = (
        None if not args.source or args.source == SOURCE_ALL else args.source
    )

    try:
        results = search_mod.search(
            args.query,
            project=args.project,
            source=source_filter,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    except search_mod.FTSSyntaxError as e:
        sys.stderr.write(
            f"error: invalid FTS query: {e}\n"
            'hint: use double-quoted phrases ("rate limit"), boolean AND/OR/NOT, '
            "or prefix wildcards (foo*). Quotes inside the query must be balanced.\n"
        )
        return 2

    if args.format == "json":
        import json

        payload = [
            {
                "session_id": r.session_id,
                "date": r.date,
                "source": r.source,
                "project": r.project,
                "cwd": r.cwd,
                "first_ts": r.first_ts,
                "turn_count": r.turn_count,
                "redactions_total": r.redactions_total,
                "snippet": r.snippet,
            }
            for r in results
        ]
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 0

    if not results:
        sys.stdout.write(f"no matches for: {args.query}\n")
        return 0
    for r in results:
        sys.stdout.write(
            f"{r.date} · {r.source} · {r.project or '—'} · {r.session_id[:8]} "
            f"({r.turn_count} turns"
            + (f", {r.redactions_total} redactions" if r.redactions_total else "")
            + ")\n"
        )
        if r.snippet:
            for line in r.snippet.splitlines():
                sys.stdout.write(f"    {line}\n")
        sys.stdout.write("\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-session-capture",
        description="Capture Claude Code sessions into a daily Markdown archive.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help=f"path to config.toml (default: {default_config_path()})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="render to stdout / log but do not write any files",
    )
    p.add_argument(
        "--show-redactions",
        action="store_true",
        help="print redaction count summary only and exit",
    )
    p.add_argument(
        "--date",
        type=_parse_date,
        help="YYYY-MM-DD (overrides default 'yesterday' for the daily command)",
    )
    p.add_argument(
        "--projects-root",
        type=str,
        default=None,
        help=(
            "override the Claude Code transcripts root for this run (one-off "
            "imports / debugging). Default derives from CLAUDE_CONFIG_DIR or "
            "falls back to ~/.claude/projects. See ADR-0004."
        ),
    )
    p.add_argument("--verbose", "-v", action="store_true")

    source_choices = (*KNOWN_SOURCES, SOURCE_ALL)
    sub = p.add_subparsers(dest="cmd", required=True)

    daily_p = sub.add_parser(
        "daily", help="render yesterday's MD (what the scheduler runs)"
    )
    daily_p.add_argument(
        "--source", choices=source_choices, default=SOURCE_ALL,
        help="which adapter source to ingest (default: all)",
    )

    backfill_p = sub.add_parser("backfill", help="render every historical date")
    backfill_p.add_argument(
        "--source", choices=source_choices, default=SOURCE_ALL,
        help="which adapter source to ingest (default: all)",
    )

    sp = sub.add_parser("search", help="query the FTS index over captured sessions")
    sp.add_argument("query", nargs="?", help="FTS5 query (phrases, AND/OR/NOT, prefix*)")
    sp.add_argument("--project", help="filter to sessions from this project")
    sp.add_argument(
        "--source", choices=source_choices, default=SOURCE_ALL,
        help="filter results by adapter source (default: all)",
    )
    sp.add_argument("--since", type=_parse_date, help="YYYY-MM-DD, inclusive")
    sp.add_argument("--until", type=_parse_date, help="YYYY-MM-DD, inclusive")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--format", choices=("text", "json"), default="text")
    sp.add_argument(
        "--rebuild",
        action="store_true",
        help="drop and re-index from scratch (no QUERY needed)",
    )

    sub.add_parser(
        "mcp-serve",
        help="run the MCP server over stdio (wire into Claude Code settings)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    os.umask(0o077)
    # Bootstrap logger at default level; promote after config is loaded.
    logger = setup_logging(verbose=args.verbose)

    try:
        cfg = Config.load(args.config if args.config.exists() else None)
    except Exception as e:  # noqa: BLE001
        logger.exception("config load failed")
        write_last_error(f"config load failed: {e}")
        notify_failure("claude-session-capture", f"config load failed: {e}")
        return 2

    # Apply configured log level if --verbose isn't forcing debug
    if not args.verbose:
        from .state import set_log_level

        set_log_level(cfg.logging.level)

    # mcp-serve is a long-running read-only server — it must not hold the
    # flock (that would block daily/backfill runs scheduled alongside it).
    if args.cmd == "mcp-serve":
        try:
            from .mcp_server import run_stdio
        except ImportError as e:
            sys.stderr.write(
                f"error: mcp extra not installed. Install with:\n"
                f"  pip install -e '.[mcp]'\n({e})\n"
            )
            return 2
        return run_stdio()

    lock_path = state_dir() / "run.lock"
    try:
        with flock_exclusive(lock_path):
            if args.cmd == "daily":
                rc = cmd_daily(args, cfg, logger)
            elif args.cmd == "backfill":
                rc = cmd_backfill(args, cfg, logger)
            elif args.cmd == "search":
                rc = cmd_search(args, cfg, logger)
            else:  # pragma: no cover — argparse enforces subcommand presence
                rc = 2
    except KeyboardInterrupt:
        logger.info("interrupted")
        return 130
    except Exception as e:  # noqa: BLE001
        logger.exception("run failed")
        write_last_error(str(e))
        notify_failure("claude-session-capture", f"run failed: {e}")
        return 1

    if rc == 0:
        clear_last_error()
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
