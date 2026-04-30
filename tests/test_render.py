"""Session + daily-index renderer tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

from ai_session_capture.config import Config
from ai_session_capture.parser import Record, SessionMeta
from ai_session_capture.render import (
    render_daily_index,
    render_session_file,
    resolve_tz,
)


def _r(**kw):
    defaults = dict(
        session_id="s1",
        timestamp=datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
        kind="user",
        content="hi",
        uuid="u1",
        parent_uuid="",
        is_sidechain=False,
        cwd="/home/user/proj",
        project="proj",
        tool_calls=[],
        tool_results=[],
        thinking=[],
        raw_type="user",
    )
    defaults.update(kw)
    return Record(**defaults)


def test_render_session_file_basic():
    cfg = Config()
    meta = SessionMeta(session_id="s1", custom_title="hello session")
    records = [
        _r(content="what is up", uuid="u1"),
        _r(
            kind="assistant",
            content="not much",
            uuid="u2",
            timestamp=datetime(2026, 4, 20, 10, 0, 5, tzinfo=UTC),
        ),
    ]
    result = render_session_file("s1", records, meta, cfg, tz=UTC)
    assert result.session_id == "s1"
    assert result.turn_count == 2
    assert result.dates_touched == [date(2026, 4, 20)]
    assert "what is up" in result.markdown
    assert "not much" in result.markdown
    assert "hello session" in result.markdown  # title
    assert str(result.relpath).startswith("sessions/")
    assert str(result.relpath).endswith(".md")


def test_render_session_file_frontmatter_uses_source():
    """Codex sessions get `source: codex` and a `codex-session` tag,
    Claude sessions stay `claude`. Without this the renderer would
    hardcode `claude-session` for every adapter (regression guard for
    the v0.2.0 multi-source rename)."""
    cfg = Config()
    claude_records = [_r(session_id="sclaude", source="claude", content="hi from claude")]
    codex_records = [_r(session_id="scodex", source="codex", content="hi from codex")]
    claude_md = render_session_file("sclaude", claude_records, None, cfg, tz=UTC).markdown
    codex_md = render_session_file("scodex", codex_records, None, cfg, tz=UTC).markdown
    assert "source: claude" in claude_md
    assert "- claude-session" in claude_md
    assert "- codex-session" not in claude_md
    assert "source: codex" in codex_md
    assert "- codex-session" in codex_md
    assert "- claude-session" not in codex_md


def test_render_session_file_cross_day_produces_single_file():
    cfg = Config()
    records = [
        _r(content="late night", uuid="u1",
           timestamp=datetime(2026, 4, 19, 23, 50, tzinfo=UTC)),
        _r(content="early morning", uuid="u2",
           timestamp=datetime(2026, 4, 20, 0, 10, tzinfo=UTC)),
    ]
    result = render_session_file("s1", records, None, cfg, tz=UTC)
    assert result.turn_count == 2
    assert result.dates_touched == [date(2026, 4, 19), date(2026, 4, 20)]
    # Filename is pinned to the START date
    assert "2026-04-19" in str(result.relpath)
    assert "2026-04-20" not in str(result.relpath)


def test_render_session_file_redaction_emits_warning():
    cfg = Config()
    records = [
        _r(content=f"the AWS key is AKIAIOSFODNN7EXAMPLE", uuid="u1"),
    ]
    result = render_session_file("s1", records, None, cfg, tz=UTC)
    assert "AKIAIOSFODNN7EXAMPLE" not in result.markdown
    assert "REDACTED:AWS_AKID" in result.markdown
    assert "secret pattern(s) redacted" in result.markdown
    assert result.report.total() == 1


def test_render_session_file_applies_sidechain_modes():
    cfg = Config()
    records = [
        _r(content="main", uuid="u1"),
        _r(content="sub", uuid="u2", is_sidechain=True),
    ]
    cfg.content.sidechain = "off"
    off_r = render_session_file("s1", records, None, cfg, tz=UTC)
    assert "main" in off_r.markdown
    assert "sub" not in off_r.markdown
    assert "sidechain" not in off_r.markdown

    cfg.content.sidechain = "summary"
    summary_r = render_session_file("s1", records, None, cfg, tz=UTC)
    assert "main" in summary_r.markdown
    assert "sub" not in summary_r.markdown
    assert "1 sidechain message(s) omitted" in summary_r.markdown

    cfg.content.sidechain = "full"
    full_r = render_session_file("s1", records, None, cfg, tz=UTC)
    assert "main" in full_r.markdown
    assert "sub" in full_r.markdown


def test_render_session_file_is_deterministic():
    cfg = Config()
    records = [
        _r(content=f"turn {i}", uuid=f"u{i}",
           timestamp=datetime(2026, 4, 20, 10, i, tzinfo=UTC))
        for i in range(5)
    ]
    a = render_session_file("s1", records, None, cfg, tz=UTC)
    b = render_session_file("s1", records, None, cfg, tz=UTC)
    assert a.markdown == b.markdown


def test_render_session_file_tool_call_summary():
    cfg = Config()
    records = [
        _r(
            kind="assistant",
            content="checking",
            uuid="u1",
            tool_calls=[
                {"id": "t1", "name": "Bash", "input": {"command": "ls -la"}, "dropped": False}
            ],
        ),
    ]
    r = render_session_file("s1", records, None, cfg, tz=UTC)
    assert "Bash" in r.markdown
    assert "ls -la" in r.markdown


def test_render_session_file_dropped_tool_shows_note():
    cfg = Config()
    records = [
        _r(
            kind="assistant",
            content="",
            uuid="u1",
            tool_calls=[
                {"id": "t1", "name": "Bash", "input": {"command": "env"}, "dropped": True}
            ],
        ),
    ]
    r = render_session_file("s1", records, None, cfg, tz=UTC)
    assert "dropped: sensitive" in r.markdown


def test_render_session_file_uses_first_prompt_as_slug_fallback():
    cfg = Config()
    # No custom title; rely on first_prompt slug
    meta = SessionMeta(session_id="s1", first_prompt="how do I handle rate limits")
    records = [_r(content="body", uuid="u1")]
    r = render_session_file("s1", records, meta, cfg, tz=UTC)
    assert "how-do-i-handle-rate" in str(r.relpath)


def test_render_session_file_redacts_title_and_filename():
    """Secrets in custom_title or first_prompt must not leak into filename/title."""
    cfg = Config()
    meta = SessionMeta(
        session_id="s1",
        first_prompt="I pasted AKIAIOSFODNN7EXAMPLE here",
    )
    records = [_r(content="safe body")]
    r = render_session_file("s1", records, meta, cfg, tz=UTC)
    # The raw secret must not appear anywhere in the rendered MD
    assert "AKIAIOSFODNN7EXAMPLE" not in r.markdown
    # ...and must not appear in the filename either (slugified lowercase form)
    assert "akiaiosfodnn7example" not in str(r.relpath)


def test_render_session_file_redacts_cwd():
    """A secret-shaped substring in cwd must not leak to frontmatter/header."""
    cfg = Config()
    records = [_r(content="body", cwd="/home/x/project-ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")]
    r = render_session_file("s1", records, None, cfg, tz=UTC)
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in r.markdown
    # The REDACTED marker is what lands in the output instead
    assert "REDACTED:GITHUB_PAT_CLASSIC" in r.markdown


def test_render_session_file_empty_session_has_no_slug():
    cfg = Config()
    meta = SessionMeta(session_id="s1")  # no title, no prompt
    records = [_r(kind="assistant", content="", uuid="u1", tool_calls=[])]
    r = render_session_file("s1", records, meta, cfg, tz=UTC)
    # Filename should be <date>_<time>_<sid>.md with no slug suffix
    name = str(r.relpath).rsplit("/", 1)[-1]
    parts = name.rsplit(".md", 1)[0].split("_")
    assert len(parts) == 3  # date, time, sid


def test_render_daily_index_empty():
    cfg = Config()
    r = render_daily_index(date(2026, 4, 20), [], cfg, tz=UTC, machine="mbp")
    assert "No sessions touched this day" in r.markdown
    assert str(r.relpath) == "daily/mbp/2026-04-20.md"


def test_render_daily_index_lists_touching_sessions():
    cfg = Config()
    records = [
        _r(content="first prompt", uuid="u1",
           session_id="s1",
           timestamp=datetime(2026, 4, 20, 9, 0, tzinfo=UTC)),
        _r(content="second prompt", uuid="u2",
           session_id="s2", project="other",
           timestamp=datetime(2026, 4, 20, 14, 0, tzinfo=UTC)),
    ]
    sr1 = render_session_file("s1", records, None, cfg, tz=UTC)
    sr2 = render_session_file("s2", records, None, cfg, tz=UTC)

    idx = render_daily_index(
        date(2026, 4, 20),
        [sr1, sr2],
        cfg,
        tz=UTC,
        all_records=records,
        machine="mbp",
    )
    assert "s1"[:8] in idx.markdown
    assert "s2"[:8] in idx.markdown
    assert "| 09:00:00 |" in idx.markdown
    assert "| 14:00:00 |" in idx.markdown
    # Daily lives at daily/<machine>/<date>.md and sessions live at
    # sessions/<machine>/<source>/<project>/ — link path needs two
    # ../ to reach the data-repo root (ADR-0006).
    assert "[[../../sessions/unknown/claude/proj/" in idx.markdown
    assert "[[../../sessions/unknown/claude/other/" in idx.markdown


def test_render_daily_index_excludes_sessions_not_touching_that_day():
    cfg = Config()
    records_mon = [_r(timestamp=datetime(2026, 4, 20, 10, 0, tzinfo=UTC))]
    sr = render_session_file("s1", records_mon, None, cfg, tz=UTC)

    # Asking for Tue — session only touched Mon
    idx = render_daily_index(date(2026, 4, 21), [sr], cfg, tz=UTC)
    assert "No sessions touched this day" in idx.markdown


def test_render_daily_index_redaction_aggregate():
    cfg = Config()
    records = [
        _r(content=f"leak AKIAIOSFODNN7EXAMPLE once", uuid="u1",
           session_id="s1"),
        _r(content=f"leak AKIAIOSFODNN7EXAMPLE twice", uuid="u2",
           session_id="s2", project="p2",
           timestamp=datetime(2026, 4, 20, 11, 0, tzinfo=UTC)),
    ]
    sr1 = render_session_file("s1", records, None, cfg, tz=UTC)
    sr2 = render_session_file("s2", records, None, cfg, tz=UTC)

    idx = render_daily_index(
        date(2026, 4, 20),
        [sr1, sr2],
        cfg,
        tz=UTC,
        all_records=records,
    )
    # Two AKIA occurrences on this day
    assert "redacted across today" in idx.markdown
    assert "AWS_AKID=2" in idx.markdown


def test_resolve_tz_returns_a_timezone():
    assert resolve_tz(Config()) is not None


def test_frontmatter_toggle_session_file():
    cfg = Config()
    records = [_r(content="hello")]
    cfg.output.frontmatter.enabled = True
    with_fm = render_session_file("s1", records, None, cfg, tz=UTC)
    assert with_fm.markdown.lstrip().startswith("---")
    assert "session_id:" in with_fm.markdown

    cfg.output.frontmatter.enabled = False
    without_fm = render_session_file("s1", records, None, cfg, tz=UTC)
    assert not without_fm.markdown.lstrip().startswith("---")
    assert "session_id:" not in without_fm.markdown
    # Body content must still be present
    assert "hello" in without_fm.markdown


def test_frontmatter_toggle_daily_index():
    cfg = Config()
    records = [_r(content="x")]
    sr = render_session_file("s1", records, None, cfg, tz=UTC)

    cfg.output.frontmatter.enabled = True
    with_fm = render_daily_index(date(2026, 4, 20), [sr], cfg, tz=UTC, all_records=records)
    assert with_fm.markdown.lstrip().startswith("---")
    assert "date:" in with_fm.markdown

    cfg.output.frontmatter.enabled = False
    without_fm = render_daily_index(date(2026, 4, 20), [sr], cfg, tz=UTC, all_records=records)
    assert not without_fm.markdown.lstrip().startswith("---")
    assert "# 2026-04-20 — session index" in without_fm.markdown
