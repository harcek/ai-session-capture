"""Command-line interface — the one entrypoint the scheduler calls.

``ai-session-capture daily`` is the cron/timer path; it renders
yesterday's local-date and writes to the output repo, idempotent by
content hash. ``backfill`` walks every historical JSONL and emits a file
per distinct date. ``--dry-run`` and ``--show-redactions`` are inspection
aids; they never touch disk.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
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
    atomic_write_text,
    clear_last_error,
    flock_exclusive,
    migrate_archive_to_per_machine,
    migrate_data_dir,
    notify_failure,
    resolve_machine_name,
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


def _warn_machine_flag_on_ingest(args, current: str, logger: logging.Logger) -> None:
    """``--machine`` on daily/backfill is informational; only this
    machine's JSONL is on this filesystem. Non-current values are a
    likely scripting mistake — warn so the user notices, but proceed.
    """
    requested = getattr(args, "machine", None)
    if requested and requested != current:
        logger.warning(
            "ignoring --machine=%s on ingest path; this run captures the "
            "local machine (%s) only. Use `search --machine=%s` to query "
            "another machine's archive.",
            requested, current, requested,
        )


def _load_all_records(
    logger: logging.Logger, args, sources: tuple[str, ...], machine: str
) -> list:
    """Parse every JSONL across the requested sources; skip individual failures.

    Adapters with a missing root directory contribute zero records
    (``iter_*_jsonls`` returns empty), so on a machine without Codex
    installed, ``--source all`` quietly captures only Claude.

    ``machine`` is stamped onto every Record so the renderer and FTS
    index can partition by host (ADR-0006).
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

    for r in records:
        r.machine = machine
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
    machine = resolve_machine_name(cfg)
    _warn_machine_flag_on_ingest(args, machine, logger)

    records = _load_all_records(logger, args, sources, machine)
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
    index = render_daily_index(target, renders, cfg, tz, all_records=day_records, machine=machine)

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
    machine = resolve_machine_name(cfg)
    _warn_machine_flag_on_ingest(args, machine, logger)
    records = _load_all_records(logger, args, sources, machine)
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
            index = render_daily_index(d, renders, cfg, tz, all_records=day_records, machine=machine)
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
    if args.rebuild:
        # Walk the rendered session MDs in the data dir — this is the
        # path that lets one machine query the unified archive after a
        # ``git pull`` brought in MDs from other machines (ADR-0006).
        # The legacy parse-from-JSONL rebuild lived in this slot until
        # v0.3.0; see ``search_mod.rebuild_all`` for the by-records
        # variant if it's needed again later.
        output_dir = Path(cfg.output.dir).expanduser()
        n, skipped = search_mod.rebuild_all_from_disk(output_dir)
        logger.info(
            "rebuilt index from %s: %d sessions indexed, %d skipped (bad frontmatter)",
            output_dir, n, skipped,
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
            machine=getattr(args, "machine", None) or None,
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
                "machine": r.machine,
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
            f"{r.date} · {r.machine}/{r.source} · {r.project or '—'} · "
            f"{r.session_id[:8]} ({r.turn_count} turns"
            + (f", {r.redactions_total} redactions" if r.redactions_total else "")
            + ")\n"
        )
        if r.snippet:
            for line in r.snippet.splitlines():
                sys.stdout.write(f"    {line}\n")
        sys.stdout.write("\n")
    return 0


def cmd_migrate_machine(args, cfg: Config, logger: logging.Logger) -> int:
    """Rename a machine in place across paths, frontmatter, and FTS.

    Without this, changing ``[machine].name`` and re-running
    ``backfill`` either leaks orphan subtrees from the old name or
    requires a wipe-and-rebackfill, which silently loses any session
    whose JSONL has since been pruned. This subcommand is the only
    way to rename a machine while preserving sessions whose source
    JSONLs may no longer exist on disk.
    """
    old = args.old
    new = args.new
    if old == new:
        logger.info("nothing to do — old and new machine names match")
        return 0

    output = Path(cfg.output.dir).expanduser()
    sessions_old = output / "sessions" / old
    sessions_new = output / "sessions" / new
    daily_old = output / "daily" / old
    daily_new = output / "daily" / new

    if not sessions_old.exists() and not daily_old.exists():
        logger.warning("no archive subtree found for machine %r at %s", old, output)
        return 0

    # Refuse to merge — the user almost certainly didn't mean it, and
    # merging would silently union per-session content.
    if sessions_new.exists():
        logger.error(
            "target sessions/%s already exists; refusing to merge. "
            "Move or remove the existing subtree first.", new,
        )
        return 2
    if daily_new.exists():
        logger.error(
            "target daily/%s already exists; refusing to merge.", new,
        )
        return 2

    mds: list[Path] = []
    if sessions_old.exists():
        mds.extend(sessions_old.rglob("*.md"))
    if daily_old.exists():
        mds.extend(daily_old.rglob("*.md"))

    if args.dry_run:
        logger.info(
            "dry-run: would rewrite machine field in %d MDs and rename "
            "sessions/%s → sessions/%s + daily/%s → daily/%s",
            len(mds), old, new, old, new,
        )
        return 0

    # Frontmatter: rewrite the canonical `machine: <old>` line and the
    # `- machine/<old>` tag entry. Both must move in lockstep with the
    # filesystem move — the from-disk FTS rebuild reads frontmatter as
    # truth.
    machine_line = re.compile(r"^machine: " + re.escape(old) + r"$", re.MULTILINE)
    machine_tag = re.compile(r"^(\s+- machine/)" + re.escape(old) + r"$", re.MULTILINE)
    rewritten = 0
    skipped = 0
    for md in mds:
        text = md.read_text(encoding="utf-8")
        new_text = machine_line.sub(f"machine: {new}", text)
        new_text = machine_tag.sub(rf"\g<1>{new}", new_text)
        if new_text != text:
            atomic_write_text(md, new_text)
            rewritten += 1
        else:
            # MD pre-dates v0.3.0 frontmatter (no `machine:` line) — moving
            # the file is fine, but it won't be discoverable via `--machine`
            # filters until re-rendered from JSONL.
            logger.warning("no machine field in %s — moving without rewrite", md)
            skipped += 1

    # Filesystem move — happens after frontmatter rewrite so a partial
    # failure leaves a coherent state at the old path.
    if sessions_old.exists():
        sessions_old.rename(sessions_new)
    if daily_old.exists():
        daily_old.rename(daily_new)

    # FTS: UPDATE the regular sessions table (in-place column update is
    # supported); for the FTS5 virtual table, follow the project's
    # established DELETE+INSERT idiom (see search.upsert_rows) so we
    # don't tickle UNINDEXED-column UPDATE semantics across SQLite
    # versions.
    fts_rows_updated = 0
    with search_mod.connect() as conn:
        cur = conn.execute(
            "UPDATE sessions SET machine = ? WHERE machine = ?",
            (new, old),
        )
        fts_rows_updated = cur.rowcount

        rows = conn.execute(
            "SELECT session_id, date, source, project, content "
            "FROM sessions_fts WHERE machine = ?",
            (old,),
        ).fetchall()
        conn.execute("DELETE FROM sessions_fts WHERE machine = ?", (old,))
        for r in rows:
            conn.execute(
                "INSERT INTO sessions_fts "
                "(session_id, date, source, machine, project, content) "
                "VALUES (?,?,?,?,?,?)",
                (r["session_id"], r["date"], r["source"], new, r["project"], r["content"]),
            )

    # cursor.json keys are relative paths; entries containing the old
    # machine segment are now stale (the file at that relpath is gone).
    # Drop them so a subsequent backfill writes fresh entries against
    # the new paths instead of treating the new files as never-seen
    # (which is harmless but wastes a hash compute).
    cursor_path = state_dir() / "cursor.json"
    if cursor_path.exists():
        try:
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cursor = {}
        old_seg = f"/{old}/"
        cleaned = {k: v for k, v in cursor.items() if old_seg not in k}
        if len(cleaned) != len(cursor):
            atomic_write_text(
                cursor_path,
                json.dumps(cleaned, indent=2, sort_keys=True),
            )

    logger.info(
        "migrated machine %r → %r: %d MDs rewritten, %d MDs skipped (no machine field), "
        "%d FTS rows updated",
        old, new, rewritten, skipped, fts_rows_updated,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ai-session-capture",
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

    # ``--machine`` is a query-time filter on `search`; on `daily` /
    # `backfill` only this machine's JSONL is on this filesystem, so
    # passing a non-current value warns and is otherwise a no-op
    # (kept as a flag for symmetry + scriptability).
    daily_p = sub.add_parser(
        "daily", help="render yesterday's MD (what the scheduler runs)"
    )
    daily_p.add_argument(
        "--source", choices=source_choices, default=SOURCE_ALL,
        help="which adapter source to ingest (default: all)",
    )
    daily_p.add_argument(
        "--machine",
        help="ignored on ingest paths (only the local machine's JSONL is "
             "on this filesystem); see `search --machine` for query-time use",
    )

    backfill_p = sub.add_parser("backfill", help="render every historical date")
    backfill_p.add_argument(
        "--source", choices=source_choices, default=SOURCE_ALL,
        help="which adapter source to ingest (default: all)",
    )
    backfill_p.add_argument(
        "--machine",
        help="ignored on ingest paths; see `search --machine` for query-time use",
    )

    sp = sub.add_parser("search", help="query the FTS index over captured sessions")
    sp.add_argument("query", nargs="?", help="FTS5 query (phrases, AND/OR/NOT, prefix*)")
    sp.add_argument("--project", help="filter to sessions from this project")
    sp.add_argument(
        "--source", choices=source_choices, default=SOURCE_ALL,
        help="filter results by adapter source (default: all)",
    )
    sp.add_argument(
        "--machine",
        help="filter results by machine (e.g. `mbp`, `ubuntu`); default: all",
    )
    sp.add_argument("--since", type=_parse_date, help="YYYY-MM-DD, inclusive")
    sp.add_argument("--until", type=_parse_date, help="YYYY-MM-DD, inclusive")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--format", choices=("text", "json"), default="text")
    sp.add_argument(
        "--rebuild",
        action="store_true",
        help="drop and re-index from scratch by walking session MDs in the "
             "data dir (no QUERY needed)",
    )

    mm = sub.add_parser(
        "migrate-machine",
        help="rename a machine in place across paths, frontmatter, and FTS",
    )
    mm.add_argument("old", help="current machine segment, e.g. `openclaw-egik`")
    mm.add_argument("new", help="desired machine segment, e.g. `mbp`")
    mm.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change; touch nothing",
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
        notify_failure("ai-session-capture", f"config load failed: {e}")
        return 2

    migrate_data_dir(cfg)
    migrate_archive_to_per_machine(cfg, resolve_machine_name(cfg))

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
            elif args.cmd == "migrate-machine":
                rc = cmd_migrate_machine(args, cfg, logger)
            else:  # pragma: no cover — argparse enforces subcommand presence
                rc = 2
    except KeyboardInterrupt:
        logger.info("interrupted")
        return 130
    except Exception as e:  # noqa: BLE001
        logger.exception("run failed")
        write_last_error(str(e))
        notify_failure("ai-session-capture", f"run failed: {e}")
        return 1

    if rc == 0:
        clear_last_error()
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
