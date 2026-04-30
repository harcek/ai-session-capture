"""FTS index + query tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from ai_session_capture import search as S
from ai_session_capture.config import Config
from ai_session_capture.parser import Record


def _r(**kw):
    defaults = dict(
        session_id="s1",
        timestamp=datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
        kind="user",
        content="hello",
        uuid="u1",
        parent_uuid="",
        is_sidechain=False,
        cwd="/p",
        project="proj",
        tool_calls=[],
        tool_results=[],
        thinking=[],
        raw_type="user",
    )
    defaults.update(kw)
    return Record(**defaults)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "index.db"


def test_build_session_rows_groups_and_redacts():
    cfg = Config()
    records = [
        _r(content="the key is AKIAIOSFODNN7EXAMPLE do not share"),
        _r(
            kind="assistant",
            content="got it, hiding that",
            uuid="u2",
            timestamp=datetime(2026, 4, 20, 10, 0, 5, tzinfo=UTC),
        ),
    ]
    rows = S.build_session_rows(records, cfg, UTC)
    assert len(rows) == 1
    r = rows[0]
    assert r.id == "s1"
    assert r.date == "2026-04-20"
    assert r.turn_count == 2
    assert r.redactions_total == 1
    assert "AKIAIOSFODNN7EXAMPLE" not in r.content
    assert "REDACTED:AWS_AKID" in r.content
    assert "hiding that" in r.content


def test_upsert_and_search(db):
    cfg = Config()
    records = [
        _r(content="rate limit strategy for openai api", uuid="u1"),
        _r(
            session_id="s2",
            content="deep value scanner rewrite",
            uuid="u2",
            timestamp=datetime(2026, 4, 19, 11, 0, tzinfo=UTC),
        ),
    ]
    rows = S.build_session_rows(records, cfg, UTC)
    inserted, skipped, orphans = S.upsert_rows(rows, path=db)
    assert inserted == 2
    assert skipped == 0

    hits = S.search("rate limit", path=db)
    assert len(hits) == 1
    assert hits[0].session_id == "s1"
    assert "[rate] [limit]" in hits[0].snippet or "[rate limit]" in hits[0].snippet

    hits = S.search("scanner", path=db)
    assert len(hits) == 1
    assert hits[0].session_id == "s2"


def test_upsert_is_idempotent(db):
    cfg = Config()
    records = [_r(content="deterministic world")]
    rows = S.build_session_rows(records, cfg, UTC)
    i1, s1, o1 = S.upsert_rows(rows, path=db)
    i2, s2, o2 = S.upsert_rows(rows, path=db)
    assert i1 == 1 and s1 == 0 and o1 == 0
    assert i2 == 0 and s2 == 1 and o2 == 0


def test_upsert_reindexes_when_content_changes(db):
    cfg = Config()
    v1 = S.build_session_rows([_r(content="original")], cfg, UTC)
    v2 = S.build_session_rows([_r(content="edited")], cfg, UTC)
    S.upsert_rows(v1, path=db)
    i, s, o = S.upsert_rows(v2, path=db)
    assert i == 1 and s == 0

    # FTS should find the new content, not the old
    assert len(S.search("edited", path=db)) == 1
    assert len(S.search("original", path=db)) == 0


def test_project_filter(db):
    rows = [
        S.SessionIndexRow(
            id="s1", date="2026-04-20", project="alpha", cwd="/a", first_ts="",
            turn_count=1, redactions_total=0, content="foo shared bar",
        ),
        S.SessionIndexRow(
            id="s2", date="2026-04-20", project="beta", cwd="/b", first_ts="",
            turn_count=1, redactions_total=0, content="baz shared qux",
        ),
    ]
    S.upsert_rows(rows, path=db)
    all_hits = S.search("shared", path=db)
    alpha_hits = S.search("shared", project="alpha", path=db)
    assert len(all_hits) == 2
    assert len(alpha_hits) == 1
    assert alpha_hits[0].project == "alpha"


def test_date_range_filter(db):
    rows = [
        S.SessionIndexRow(
            id=f"s{i}", date=f"2026-04-{i:02d}", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content=f"day{i} keyword",
        )
        for i in (18, 19, 20, 21)
    ]
    S.upsert_rows(rows, path=db)
    hits = S.search(
        "keyword",
        since=date(2026, 4, 19),
        until=date(2026, 4, 20),
        path=db,
    )
    assert sorted(h.date for h in hits) == ["2026-04-19", "2026-04-20"]


def test_list_projects_and_recent(db):
    rows = [
        S.SessionIndexRow(
            id="s1", date="2026-04-20", project="alpha", cwd="", first_ts="",
            turn_count=5, redactions_total=0, content="x",
        ),
        S.SessionIndexRow(
            id="s2", date="2026-04-19", project="alpha", cwd="", first_ts="",
            turn_count=3, redactions_total=0, content="y",
        ),
        S.SessionIndexRow(
            id="s3", date="2026-04-18", project="beta", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content="z",
        ),
    ]
    S.upsert_rows(rows, path=db)
    projects = S.list_projects(path=db)
    assert projects[0]["project"] == "alpha"
    assert projects[0]["n"] == 2

    recent = S.list_recent(limit=2, path=db)
    assert [r["session_id"] for r in recent] == ["s1", "s2"]

    got = S.get_session_text("s1", path=db)
    assert got is not None
    assert got["content"] == "x"


def test_rebuild_all_drops_prior_state(db):
    cfg = Config()
    S.upsert_rows(
        S.build_session_rows([_r(content="old")], cfg, UTC),
        path=db,
    )
    S.rebuild_all([_r(content="new")], cfg, UTC, path=db)
    assert len(S.search("new", path=db)) == 1
    assert len(S.search("old", path=db)) == 0


def test_invalid_fts_query_raises_syntax_error(db):
    """Malformed FTS syntax (unbalanced quote, etc.) raises FTSSyntaxError."""
    cfg = Config()
    records = [_r(content="some content here")]
    S.upsert_rows(S.build_session_rows(records, cfg, UTC), path=db)
    # Several flavors of user-input-level errors must all raise
    # FTSSyntaxError, not sqlite3.OperationalError:
    with pytest.raises(S.FTSSyntaxError):
        S.search('"unbalanced', path=db)


def test_upsert_cleans_orphan_rows(db):
    """Sessions whose touched-dates shrink shouldn't leave stale FTS rows.

    Seed three rows for session s1 across three dates, then upsert only
    two dates. The third row must be deleted from both sessions and
    sessions_fts.
    """
    # Use plain alpha tokens in content so FTS tokenization doesn't fight us.
    initial = [
        S.SessionIndexRow(
            id="s1", date=d, project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content=marker,
        )
        for d, marker in [
            ("2026-04-18", "alpha"),
            ("2026-04-19", "bravo"),
            ("2026-04-20", "charlie"),
        ]
    ]
    i, s, o = S.upsert_rows(initial, path=db)
    assert i == 3 and o == 0

    # Shrink: session now only has two dates with new content
    shrunk = [
        S.SessionIndexRow(
            id="s1", date=d, project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content=marker,
        )
        for d, marker in [
            ("2026-04-18", "alpha"),
            ("2026-04-19", "bravo"),
        ]
    ]
    i2, s2, o2 = S.upsert_rows(shrunk, path=db)
    # alpha + bravo unchanged ⇒ 0 insertions; charlie orphan cleaned
    assert o2 == 1

    # charlie's row must be gone; alpha + bravo still there
    assert len(S.search("charlie", path=db)) == 0
    assert len(S.search("alpha", path=db)) == 1
    assert len(S.search("bravo", path=db)) == 1

    # Confirm at the DB level too — no row at (s1, 2026-04-20)
    import sqlite3
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT date FROM sessions WHERE id = ? ORDER BY date", ("s1",)
    ).fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["2026-04-18", "2026-04-19"]


def test_upsert_does_not_clean_rows_for_absent_sessions(db):
    """Orphan cleanup is scoped per-session: a session not in the new
    input must keep all its existing rows."""
    a_rows = [
        S.SessionIndexRow(
            id="a", date="2026-04-20", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content="alpha",
        )
    ]
    b_rows = [
        S.SessionIndexRow(
            id="b", date="2026-04-20", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content="bravo",
        )
    ]
    S.upsert_rows(a_rows + b_rows, path=db)
    # Upsert only "a" again — "b" should survive untouched
    S.upsert_rows(a_rows, path=db)
    assert len(S.search("alpha", path=db)) == 1
    assert len(S.search("bravo", path=db)) == 1


@pytest.mark.parametrize(
    "value,expected_min,expected_max",
    [
        (0, 1, None),        # below min clamps up
        (-5, 1, None),       # negative clamps up
        (500, None, None),   # in-range passes
        (99999, None, 1000), # over max clamps down
        ("not-a-number", 20, 20),  # invalid type falls back to default
    ],
)
def test_search_limit_is_clamped(db, value, expected_min, expected_max):
    # Seed many rows so limit is observable
    rows = [
        S.SessionIndexRow(
            id=f"s{i}", date="2026-04-20", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content=f"banana {i}",
        )
        for i in range(30)
    ]
    S.upsert_rows(rows, path=db)
    n = S._clamp_limit(value)
    if expected_min is not None:
        assert n >= expected_min
    if expected_max is not None:
        assert n <= expected_max


def test_source_field_filters_search(db):
    """search(source=...) returns only rows from that source; default = union."""
    rows = [
        S.SessionIndexRow(
            id="sa", date="2026-04-20", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content="alpha shared",
            source="claude",
        ),
        S.SessionIndexRow(
            id="sb", date="2026-04-20", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content="beta shared",
            source="codex",
        ),
    ]
    S.upsert_rows(rows, path=db)

    union = S.search("shared", path=db)
    assert {r.session_id for r in union} == {"sa", "sb"}
    assert {r.source for r in union} == {"claude", "codex"}

    only_codex = S.search("shared", source="codex", path=db)
    assert {r.session_id for r in only_codex} == {"sb"}
    assert {r.source for r in only_codex} == {"codex"}


def test_same_session_id_across_sources_no_collision(db):
    """A session UUID can theoretically repeat across sources; both should
    upsert independently and stay distinct in the index."""
    rows = [
        S.SessionIndexRow(
            id="shared-uuid", date="2026-04-20", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content="from claude",
            source="claude",
        ),
        S.SessionIndexRow(
            id="shared-uuid", date="2026-04-20", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content="from codex",
            source="codex",
        ),
    ]
    S.upsert_rows(rows, path=db)
    hits = S.search("from", path=db)
    by_source = {r.source: r for r in hits}
    assert "claude" in by_source
    assert "codex" in by_source


def test_list_recent_filters_by_source(db):
    rows = [
        S.SessionIndexRow(
            id=f"s{i}", date=f"2026-04-{i:02d}", project="p", cwd="",
            first_ts="", turn_count=1, redactions_total=0,
            content=f"content {i}",
            source="claude" if i % 2 else "codex",
        )
        for i in (18, 19, 20, 21)
    ]
    S.upsert_rows(rows, path=db)
    only_codex = S.list_recent(limit=10, source="codex", path=db)
    assert all(r["source"] == "codex" for r in only_codex)
    assert {r["session_id"] for r in only_codex} == {"s18", "s20"}


def test_get_session_text_with_source_disambiguator(db):
    rows = [
        S.SessionIndexRow(
            id="dup", date="2026-04-20", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content="claude side",
            source="claude",
        ),
        S.SessionIndexRow(
            id="dup", date="2026-04-20", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content="codex side",
            source="codex",
        ),
    ]
    S.upsert_rows(rows, path=db)
    cx = S.get_session_text("dup", source="codex", path=db)
    assert cx is not None
    assert cx["content"] == "codex side"
    assert cx["source"] == "codex"


def test_legacy_db_migrates_to_source_column(tmp_path):
    """A pre-source-column DB should gain the column on first connect."""
    import sqlite3
    db = tmp_path / "old.db"
    # Create the legacy schema (no `source` column)
    legacy = sqlite3.connect(str(db))
    legacy.executescript("""
        CREATE TABLE sessions (
            id TEXT NOT NULL,
            date TEXT NOT NULL,
            project TEXT,
            cwd TEXT,
            first_ts TEXT,
            turn_count INTEGER NOT NULL DEFAULT 0,
            redactions_total INTEGER NOT NULL DEFAULT 0,
            content_hash TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            PRIMARY KEY (id, date)
        );
    """)
    legacy.execute(
        "INSERT INTO sessions (id, date, project, cwd, first_ts, "
        "turn_count, redactions_total, content_hash, indexed_at) "
        "VALUES ('legacy', '2026-04-01', 'p', '', '', 1, 0, 'h', 'now')"
    )
    legacy.commit()
    legacy.close()

    # Connect via our manager — migration runs
    with S.connect(db) as conn:
        cols = [
            r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
        ]
    assert "source" in cols
    assert "machine" in cols

    # Pre-existing row keeps the defaults
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT source, machine FROM sessions WHERE id='legacy'"
    ).fetchone()
    conn.close()
    assert row["source"] == "claude"
    assert row["machine"] == "unknown"


def test_legacy_v0_2_db_migrates_machine_pk(tmp_path):
    """A v0.2.0 DB (PK includes source but not machine) should
    rebuild the table to PK(id, date, source, machine) preserving
    rows."""
    import sqlite3
    db = tmp_path / "v02.db"
    legacy = sqlite3.connect(str(db))
    legacy.executescript("""
        CREATE TABLE sessions (
            id TEXT NOT NULL,
            date TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'claude',
            project TEXT,
            cwd TEXT,
            first_ts TEXT,
            turn_count INTEGER NOT NULL DEFAULT 0,
            redactions_total INTEGER NOT NULL DEFAULT 0,
            content_hash TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            PRIMARY KEY (id, date, source)
        );
    """)
    legacy.execute(
        "INSERT INTO sessions (id, date, source, project, cwd, first_ts, "
        "turn_count, redactions_total, content_hash, indexed_at) "
        "VALUES ('s1', '2026-04-01', 'codex', 'p', '', '', 3, 0, 'h', 'now')"
    )
    legacy.commit()
    legacy.close()

    with S.connect(db) as conn:
        pk_cols = [
            r["name"]
            for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
            if r["pk"] > 0
        ]
        row = conn.execute(
            "SELECT id, source, machine FROM sessions WHERE id='s1'"
        ).fetchone()
    assert "machine" in pk_cols and "source" in pk_cols
    assert row["source"] == "codex"
    assert row["machine"] == "unknown"


def test_search_filter_by_machine(db):
    """A machine filter narrows results to that host's captures."""
    rows = [
        S.SessionIndexRow(
            id="s1", date="2026-04-20", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content="banana",
            machine="mbp",
        ),
        S.SessionIndexRow(
            id="s2", date="2026-04-20", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content="banana",
            machine="ubuntu",
        ),
    ]
    S.upsert_rows(rows, path=db)
    all_hits = S.search("banana", path=db)
    mbp_hits = S.search("banana", machine="mbp", path=db)
    assert len(all_hits) == 2
    assert len(mbp_hits) == 1
    assert mbp_hits[0].machine == "mbp"


def test_parse_session_md_extracts_required_keys():
    """A well-formed session MD round-trips through parse_session_md."""
    md = (
        "---\n"
        "session_id: abc123\n"
        "session_id_short: abc123\n"
        "project: deep-value-scanner\n"
        "started_at: 2026-04-20 13:21:00\n"
        "ended_at: 2026-04-20 13:30:00\n"
        "turn_count: 4\n"
        "redactions_total: 2\n"
        "redactions:\n"
        "  AWS_AKID: 2\n"
        "source: claude\n"
        "machine: mbp\n"
        "tags:\n"
        "  - claude-session\n"
        "  - machine/mbp\n"
        "  - project/deep-value-scanner\n"
        "---\n"
        "\n"
        "# Title\n"
        "\n"
        "body content here\n"
    )
    fm, body = S.parse_session_md(md)
    assert fm["session_id"] == "abc123"
    assert fm["source"] == "claude"
    assert fm["machine"] == "mbp"
    assert fm["project"] == "deep-value-scanner"
    assert "body content here" in body


def test_parse_session_md_rejects_missing_required_keys():
    """An MD missing one of the required frontmatter keys raises so
    a partial FTS row never lands silently."""
    md = "---\nsession_id: x\nsource: claude\n---\nbody\n"
    with pytest.raises(S.FrontmatterError):
        S.parse_session_md(md)


def test_rebuild_all_from_disk_indexes_per_machine_subtrees(tmp_path, db):
    """Rebuild walks sessions/<machine>/<source>/<project>/*.md and
    indexes everything it finds — even MDs from machines whose JSONL
    isn't on this filesystem (the post-`git pull` case)."""
    sessions = tmp_path / "sessions"
    mbp_md = sessions / "mbp" / "claude" / "p" / "session.md"
    ubuntu_md = sessions / "ubuntu" / "codex" / "p" / "session.md"
    mbp_md.parent.mkdir(parents=True)
    ubuntu_md.parent.mkdir(parents=True)
    mbp_md.write_text(
        "---\nsession_id: s1\nsource: claude\nmachine: mbp\nproject: p\n"
        "started_at: 2026-04-20 10:00:00\nturn_count: 3\nredactions_total: 0\n"
        "---\n\nclaude body about apples\n"
    )
    ubuntu_md.write_text(
        "---\nsession_id: s2\nsource: codex\nmachine: ubuntu\nproject: p\n"
        "started_at: 2026-04-21 11:00:00\nturn_count: 5\nredactions_total: 0\n"
        "---\n\ncodex body about apples\n"
    )
    n, skipped = S.rebuild_all_from_disk(tmp_path, path=db)
    assert n == 2
    assert skipped == 0
    hits = S.search("apples", path=db)
    machines = {h.machine for h in hits}
    assert machines == {"mbp", "ubuntu"}


def test_rebuild_all_from_disk_skips_malformed_frontmatter(tmp_path, db):
    """A malformed MD doesn't halt the whole rebuild; it's just
    counted in `skipped`."""
    sessions = tmp_path / "sessions"
    good = sessions / "mbp" / "claude" / "p" / "good.md"
    bad = sessions / "mbp" / "claude" / "p" / "bad.md"
    good.parent.mkdir(parents=True)
    good.write_text(
        "---\nsession_id: s1\nsource: claude\nmachine: mbp\nproject: p\n"
        "started_at: 2026-04-20 10:00:00\nturn_count: 1\nredactions_total: 0\n"
        "---\n\nbody\n"
    )
    bad.write_text("not a session md at all")
    n, skipped = S.rebuild_all_from_disk(tmp_path, path=db)
    assert n == 1
    assert skipped == 1


def test_same_session_id_different_machines_coexist(db):
    """Two machines may produce sessions with colliding ids; the
    composite PK keeps both rows and FTS returns both."""
    rows = [
        S.SessionIndexRow(
            id="shared", date="2026-04-20", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content="apple", machine="mbp",
        ),
        S.SessionIndexRow(
            id="shared", date="2026-04-20", project="p", cwd="", first_ts="",
            turn_count=1, redactions_total=0, content="apple", machine="ubuntu",
        ),
    ]
    ins, _, _ = S.upsert_rows(rows, path=db)
    assert ins == 2
    hits = S.search("apple", path=db)
    machines = {h.machine for h in hits}
    assert machines == {"mbp", "ubuntu"}


def test_fts_phrase_and_prefix_queries(db):
    cfg = Config()
    records = [
        _r(content="rate limiting with exponential backoff", uuid="u1"),
        _r(
            session_id="s2",
            content="rate a movie please",
            uuid="u2",
            timestamp=datetime(2026, 4, 19, 11, 0, tzinfo=UTC),
        ),
    ]
    S.upsert_rows(S.build_session_rows(records, cfg, UTC), path=db)
    # Phrase match binds both words
    phrase = S.search('"rate limiting"', path=db)
    assert len(phrase) == 1
    assert phrase[0].session_id == "s1"
    # Prefix match
    pre = S.search("limi*", path=db)
    assert len(pre) == 1
    assert pre[0].session_id == "s1"
