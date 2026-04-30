"""SQLite FTS5 search index over indexed sessions.

One row per (session, local-date) in ``sessions``; full redacted text
in the ``sessions_fts`` virtual table. Indexing is idempotent via a
content hash on the redacted text — unchanged sessions skip the
upsert. The DB lives in XDG state (not the data repo) so it rebuilds
cheaply on a new machine and doesn't pollute a text-only git repo.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .layout import sanitize_project
from .parser import Record
from .redact import RedactionReport, redact


# Split into pre/post-migration so column-adding migrations run between
# the table creation and the index/FTS creation that *depends* on the
# new column.
_SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS sessions (
    id               TEXT NOT NULL,
    date             TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'claude',
    project          TEXT,
    cwd              TEXT,
    first_ts         TEXT,
    turn_count       INTEGER NOT NULL DEFAULT 0,
    redactions_total INTEGER NOT NULL DEFAULT 0,
    content_hash     TEXT NOT NULL,
    indexed_at       TEXT NOT NULL,
    PRIMARY KEY (id, date, source)
);
"""

# Idempotent: ALTER TABLE ADD COLUMN raises if the column already exists;
# we catch the "duplicate column" error and ignore it.
_MIGRATIONS = [
    "ALTER TABLE sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'claude'",
]

_SCHEMA_INDEXES_AND_FTS = """
CREATE INDEX IF NOT EXISTS idx_sessions_date    ON sessions(date);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_sessions_source  ON sessions(source);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    session_id UNINDEXED,
    date UNINDEXED,
    source UNINDEXED,
    project,
    content,
    tokenize = "porter unicode61 remove_diacritics 2"
);
"""


def db_path() -> Path:
    """XDG state: ``~/.local/state/ai-session-capture/index.db``.

    One-shot migration: if an old ``logbook.db`` (pre-0.1.0 name) exists
    alongside and ``index.db`` does not, it's renamed in place. After
    a single run the old name is gone; nothing else ever touches
    ``logbook.db`` again.
    """
    from .state import state_dir

    state = state_dir()
    new_path = state / "index.db"
    old_path = state / "logbook.db"
    if old_path.exists() and not new_path.exists():
        old_path.rename(new_path)
    return new_path


@contextmanager
def connect(path: Path | None = None):
    """Open (and initialize) the FTS DB. ``path=None`` uses ``db_path()``."""
    p = path or db_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        # 1. Tables — creates if missing, no-op on pre-existing.
        conn.executescript(_SCHEMA_TABLES)
        # 2. Add columns to legacy tables. Idempotent via duplicate-
        # column catch; must run before indexes/PK-rebuild touch them.
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        # 3. Primary-key migration. SQLite's ALTER TABLE ADD COLUMN does
        # not update the PK, so a legacy table created with PK(id, date)
        # still has that PK after we add `source`. Detect and rebuild
        # the table preserving rows. ON CONFLICT(id, date, source)
        # upserts depend on this.
        pk_cols = [
            r["name"]
            for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
            if r["pk"] > 0
        ]
        if pk_cols and "source" not in pk_cols:
            existing = conn.execute(
                "SELECT id, date, source, project, cwd, first_ts, "
                "turn_count, redactions_total, content_hash, indexed_at "
                "FROM sessions"
            ).fetchall()
            conn.execute("DROP TABLE sessions")
            conn.executescript(_SCHEMA_TABLES)
            if existing:
                conn.executemany(
                    "INSERT INTO sessions (id, date, source, project, cwd, "
                    "first_ts, turn_count, redactions_total, content_hash, "
                    "indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [tuple(r) for r in existing],
                )
        # 4. FTS5 virtual tables can't be ALTERed; if pre-source, drop
        # and recreate (upsert paths repopulate on next run).
        cols = [
            r["name"]
            for r in conn.execute("PRAGMA table_info(sessions_fts)").fetchall()
        ]
        if cols and "source" not in cols:
            conn.execute("DROP TABLE sessions_fts")
        # 5. Indexes + (re-)created FTS table.
        conn.executescript(_SCHEMA_INDEXES_AND_FTS)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


@dataclass
class SessionIndexRow:
    id: str
    date: str  # ISO YYYY-MM-DD
    project: str
    cwd: str
    first_ts: str  # ISO datetime
    turn_count: int
    redactions_total: int
    content: str
    source: str = "claude"


def _to_local_date(ts: datetime | None, tz: ZoneInfo) -> date | None:
    if not ts:
        return None
    return ts.astimezone(tz).date()


def build_session_rows(
    records: Iterable[Record], cfg: Config, tz: ZoneInfo
) -> list[SessionIndexRow]:
    """Group records by (session_id, local-date) and build one row per group.

    Text content is redacted before being added to the row. Dropped tool
    results contribute no content. Sidechain records follow the configured
    mode (off/summary/full) exactly as the renderer would.
    """
    by_key: OrderedDict[tuple[str, date], list[Record]] = OrderedDict()

    for r in records:
        if not r.timestamp:
            continue
        local = r.timestamp.astimezone(tz).date()
        if r.is_sidechain and cfg.content.sidechain == "off":
            continue
        if r.kind == "slash_command" and not cfg.content.slash_commands:
            continue
        by_key.setdefault((r.session_id or "unknown", local), []).append(r)

    out: list[SessionIndexRow] = []
    for (sid, d), recs in by_key.items():
        recs.sort(
            key=lambda r: (
                r.timestamp or datetime.min.replace(tzinfo=UTC),
                r.uuid,
            )
        )
        report = RedactionReport()
        text_parts: list[str] = []
        turn_count = 0
        for r in recs:
            if r.is_sidechain and cfg.content.sidechain == "summary":
                continue  # counted via turn_count of main only; not indexed text
            turn_count += 1
            if r.content:
                t = (
                    redact(r.content, report)[0] if cfg.redaction.enabled else r.content
                )
                text_parts.append(t)
            for tr in r.tool_results or []:
                if tr.get("dropped"):
                    continue
                rt = tr.get("content", "")
                if isinstance(rt, list):
                    rt = "\n".join(
                        b.get("text", "") for b in rt if isinstance(b, dict)
                    )
                if not isinstance(rt, str):
                    rt = str(rt)
                if rt and cfg.redaction.enabled:
                    rt, _ = redact(rt, report)
                if rt:
                    text_parts.append(rt)

        first_ts = next((r.timestamp for r in recs if r.timestamp), None)
        # Use the aliased + sanitized project name so the FTS index agrees
        # with the filesystem layout — --project filters and MCP queries
        # take the same name the user sees in sessions/<source>/<project>/.
        raw_project = (recs[0].project or "") if recs else ""
        # source is per-session (uniform within a session); take from the
        # first record. Defaults to "claude" via Record's default.
        source = (recs[0].source or "claude") if recs else "claude"
        out.append(
            SessionIndexRow(
                id=sid,
                date=d.isoformat(),
                source=source,
                project=sanitize_project(raw_project, cfg),
                cwd=next((r.cwd for r in recs if r.cwd), ""),
                first_ts=(first_ts.astimezone(tz).isoformat() if first_ts else ""),
                turn_count=turn_count,
                redactions_total=report.total(),
                content="\n\n".join(p for p in text_parts if p),
            )
        )
    return out


def _content_hash(row: SessionIndexRow) -> str:
    # Hash what we actually index, so hash changes iff searchable text changes.
    payload = f"{row.source}\n{row.project}\n{row.cwd}\n{row.turn_count}\n{row.content}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def upsert_rows(
    rows: Iterable[SessionIndexRow], *, path: Path | None = None
) -> tuple[int, int, int]:
    """Upsert ``rows`` into the FTS index. Returns ``(inserted, skipped, orphans_cleaned)``.

    A row is skipped when its content hash matches an existing row at the
    same ``(id, date)`` — that's the idempotency gate.

    Orphan cleanup: for each session id present in the new row set, any
    existing ``(session_id, date)`` whose date isn't in the new set for
    that session is deleted. This covers the cross-day drift case where
    a session's touched-dates set shrank (e.g., a JSONL line became
    malformed and is now being skipped, or a timestamp was corrected).
    Sessions absent from ``rows`` are left alone.
    """
    rows = list(rows)  # we iterate twice (orphan-scan + upsert)
    inserted = 0
    skipped = 0
    orphans = 0
    now = datetime.now(UTC).isoformat()

    # Orphan sweep is now scoped per (session_id, source) — same session
    # id can theoretically appear under different sources (uuid collision
    # across vendors) without one wiping the other.
    new_dates_by_key: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        new_dates_by_key.setdefault((row.id, row.source), set()).add(row.date)

    with connect(path) as conn:
        for (sid, src), new_dates in new_dates_by_key.items():
            existing_dates = {
                r["date"]
                for r in conn.execute(
                    "SELECT date FROM sessions WHERE id = ? AND source = ?",
                    (sid, src),
                ).fetchall()
            }
            stale = existing_dates - new_dates
            for d in stale:
                conn.execute(
                    "DELETE FROM sessions WHERE id = ? AND date = ? AND source = ?",
                    (sid, d, src),
                )
                conn.execute(
                    "DELETE FROM sessions_fts "
                    "WHERE session_id = ? AND date = ? AND source = ?",
                    (sid, d, src),
                )
                orphans += 1

        for row in rows:
            new_hash = _content_hash(row)
            existing = conn.execute(
                "SELECT content_hash FROM sessions "
                "WHERE id = ? AND date = ? AND source = ?",
                (row.id, row.date, row.source),
            ).fetchone()
            if existing and existing["content_hash"] == new_hash:
                skipped += 1
                continue
            conn.execute(
                """INSERT INTO sessions (id, date, source, project, cwd, first_ts,
                       turn_count, redactions_total, content_hash, indexed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id, date, source) DO UPDATE SET
                       project          = excluded.project,
                       cwd              = excluded.cwd,
                       first_ts         = excluded.first_ts,
                       turn_count       = excluded.turn_count,
                       redactions_total = excluded.redactions_total,
                       content_hash     = excluded.content_hash,
                       indexed_at       = excluded.indexed_at""",
                (
                    row.id,
                    row.date,
                    row.source,
                    row.project,
                    row.cwd,
                    row.first_ts,
                    row.turn_count,
                    row.redactions_total,
                    new_hash,
                    now,
                ),
            )
            conn.execute(
                "DELETE FROM sessions_fts "
                "WHERE session_id = ? AND date = ? AND source = ?",
                (row.id, row.date, row.source),
            )
            conn.execute(
                """INSERT INTO sessions_fts (session_id, date, source, project, content)
                   VALUES (?,?,?,?,?)""",
                (row.id, row.date, row.source, row.project, row.content),
            )
            inserted += 1
    return inserted, skipped, orphans


def rebuild_all(records: Iterable[Record], cfg: Config, tz: ZoneInfo,
                *, path: Path | None = None) -> int:
    """Drop and re-populate the whole index from ``records``. Returns row count."""
    with connect(path) as conn:
        conn.execute("DELETE FROM sessions_fts")
        conn.execute("DELETE FROM sessions")
    rows = build_session_rows(records, cfg, tz)
    inserted, _, _ = upsert_rows(rows, path=path)
    return inserted


@dataclass
class SearchResult:
    session_id: str
    date: str
    project: str
    cwd: str
    first_ts: str
    turn_count: int
    redactions_total: int
    snippet: str
    source: str = "claude"


SEARCH_LIMIT_MIN = 1
SEARCH_LIMIT_MAX = 1000
SEARCH_LIMIT_DEFAULT = 20


def _clamp_limit(limit: int) -> int:
    """Clamp ``limit`` to ``[SEARCH_LIMIT_MIN, SEARCH_LIMIT_MAX]``."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return SEARCH_LIMIT_DEFAULT
    if n < SEARCH_LIMIT_MIN:
        return SEARCH_LIMIT_MIN
    if n > SEARCH_LIMIT_MAX:
        return SEARCH_LIMIT_MAX
    return n


class FTSSyntaxError(ValueError):
    """Raised for invalid FTS5 query syntax — distinct from runtime failures."""


def search(
    query: str,
    *,
    project: str | None = None,
    source: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: int = SEARCH_LIMIT_DEFAULT,
    path: Path | None = None,
) -> list[SearchResult]:
    """Run an FTS5 query. Supports phrase matching, AND/OR/NOT, prefix (``foo*``).

    Invalid FTS syntax raises :class:`FTSSyntaxError` with the original
    SQLite message (no traceback noise at the call site). ``limit`` is
    clamped to ``[1, 1000]``. ``source`` filters by adapter
    (``"claude"`` / ``"codex"`` / …); ``None`` means union.
    """
    conditions = ["sessions_fts MATCH ?"]
    params: list[object] = [query]
    if project:
        conditions.append("s.project = ?")
        params.append(project)
    if source:
        conditions.append("s.source = ?")
        params.append(source)
    if since:
        conditions.append("s.date >= ?")
        params.append(since.isoformat())
    if until:
        conditions.append("s.date <= ?")
        params.append(until.isoformat())

    sql = f"""
        SELECT s.id AS session_id,
               s.date,
               s.source,
               s.project,
               s.cwd,
               s.first_ts,
               s.turn_count,
               s.redactions_total,
               snippet(sessions_fts, 4, '[', ']', ' … ', 24) AS snip
        FROM sessions_fts
        JOIN sessions s
          ON s.id = sessions_fts.session_id
         AND s.date = sessions_fts.date
         AND s.source = sessions_fts.source
        WHERE {" AND ".join(conditions)}
        ORDER BY s.date DESC, s.first_ts DESC
        LIMIT ?
    """
    params.append(_clamp_limit(limit))

    try:
        with connect(path) as conn:
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # SQLite surfaces FTS5 query problems (syntax, unterminated
        # strings, unknown columns/queries) via plain OperationalError
        # with no error code we can key off. Allow-list the cases that
        # ARE real infrastructure failures — those propagate unchanged
        # so the top-level handler writes last-error and notifies.
        # Everything else is treated as a user query error.
        msg = str(e).lower()
        infrastructure_markers = (
            "database is locked",
            "disk i/o",
            "malformed database",
            "database or disk is full",
            "no such table",  # only fires if the schema is broken
            "database disk image",
        )
        if any(m in msg for m in infrastructure_markers):
            raise
        raise FTSSyntaxError(str(e)) from e

    return [
        SearchResult(
            session_id=r["session_id"],
            date=r["date"],
            source=r["source"] or "claude",
            project=r["project"] or "",
            cwd=r["cwd"] or "",
            first_ts=r["first_ts"] or "",
            turn_count=r["turn_count"],
            redactions_total=r["redactions_total"],
            snippet=r["snip"] or "",
        )
        for r in rows
    ]


def list_projects(
    source: str | None = None, path: Path | None = None
) -> list[dict]:
    """Distinct project names with session counts, most-active first.

    Optionally filter by ``source`` (``"claude"`` / ``"codex"`` / …);
    ``None`` (default) means union across sources.
    """
    sql = """
        SELECT project, source, COUNT(*) AS n,
               MIN(date) AS earliest, MAX(date) AS latest
        FROM sessions
        WHERE project IS NOT NULL AND project != ''
    """
    params: list[object] = []
    if source:
        sql += " AND source = ? "
        params.append(source)
    sql += " GROUP BY project, source ORDER BY n DESC"
    with connect(path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_recent(
    limit: int = 10,
    project: str | None = None,
    source: str | None = None,
    path: Path | None = None,
) -> list[dict]:
    """Most-recent sessions by date + first_ts, optionally filtered.

    ``limit`` is clamped to ``[1, 1000]``.
    """
    sql = """
        SELECT id AS session_id, date, source, project, cwd, first_ts,
               turn_count, redactions_total
        FROM sessions
    """
    params: list[object] = []
    where: list[str] = []
    if project:
        where.append("project = ?")
        params.append(project)
    if source:
        where.append("source = ?")
        params.append(source)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date DESC, first_ts DESC LIMIT ?"
    params.append(_clamp_limit(limit))
    with connect(path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_session_text(
    session_id: str,
    date_str: str | None = None,
    source: str | None = None,
    path: Path | None = None,
) -> dict | None:
    """Fetch the full indexed text for a session.

    Optionally pin to a ``date_str`` and/or ``source`` (useful when a
    session id might collide across sources).
    """
    sql = """
        SELECT s.id AS session_id, s.date, s.source, s.project, s.cwd,
               s.first_ts, s.turn_count, s.redactions_total, f.content
        FROM sessions s
        JOIN sessions_fts f
          ON f.session_id = s.id
         AND f.date = s.date
         AND f.source = s.source
        WHERE s.id = ?
    """
    params: list[object] = [session_id]
    if date_str:
        sql += " AND s.date = ?"
        params.append(date_str)
    if source:
        sql += " AND s.source = ?"
        params.append(source)
    sql += " ORDER BY s.date DESC LIMIT 1"
    with connect(path) as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None
