"""Filename + path generation tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_session_capture.config import Config
from ai_session_capture.layout import (
    SessionNaming,
    daily_index_relpath,
    sanitize_project,
    session_filename,
    session_relpath,
    slugify,
)
from datetime import date
from zoneinfo import ZoneInfo


UTC = timezone.utc


@pytest.mark.parametrize(
    "text,max_words,max_chars,expected",
    [
        (None, 5, 60, ""),
        ("", 5, 60, ""),
        ("   ", 5, 60, ""),
        ("!!", 5, 60, ""),
        ("Hello, World!", 5, 60, "hello-world"),
        ("How to handle rate limits in the OpenAI API", 5, 60, "how-to-handle-rate-limits"),
        # First sentence only
        ("First sentence. Second sentence.", 5, 60, "first-sentence"),
        # First line only (multi-paragraph)
        ("line one\nline two extras", 5, 60, "line-one"),
        # Hard char cap
        ("aaaaa bbbbb ccccc ddddd eeeee", 5, 15, "aaaaa-bbbbb-ccc"),
        # Unicode fallback — non-ASCII letters just get dropped (filename-safe)
        ("héllo mónde", 5, 60, "h-llo-m-nde"),
    ],
)
def test_slugify(text, max_words, max_chars, expected):
    # héllo → 'h' + 'llo' because we only keep [a-z0-9]; dash between.
    # Adjusting expectation for the fallback-ASCII case:
    if text == "héllo mónde":
        # In our regex, é is not [a-z0-9], so we get segments: h, llo, m, nde
        got = slugify(text, max_words, max_chars)
        assert got == "h-llo-m-nde"
        return
    assert slugify(text, max_words, max_chars) == expected


def test_sanitize_project_applies_alias():
    cfg = Config()
    cfg.projects.aliases = {"home-openclaw": "_scratch", "tmp": "_scratch"}
    assert sanitize_project("home-openclaw", cfg) == "_scratch"
    assert sanitize_project("tmp", cfg) == "_scratch"


def test_sanitize_project_lowers_and_strips_unsafe():
    cfg = Config()
    assert sanitize_project("My Project (staging)", cfg) == "my-project-staging"
    assert sanitize_project("WEIRD$$$chars###", cfg) == "weird-chars"


def test_sanitize_project_falls_back_on_empty():
    cfg = Config()
    assert sanitize_project("", cfg) == "_scratch"
    assert sanitize_project("!!!", cfg) == "_scratch"


def test_sanitize_project_caps_length():
    cfg = Config()
    cfg.session_files.project_name_max_len = 10
    out = sanitize_project("this-is-a-very-long-project-name", cfg)
    assert len(out) <= 10
    assert out == "this-is-a"  # dash-stripped from the trim


def test_session_filename_with_custom_title():
    cfg = Config()
    naming = SessionNaming(
        session_id="60651cdbad6d4eaf",
        project_raw="deep-value-scanner",
        first_ts=datetime(2026, 4, 20, 13, 21, tzinfo=UTC),
        custom_title="Sync and Ratelimit Lab",
        first_prompt="later prompt text — ignored",
    )
    fname = session_filename(naming, cfg, UTC)
    assert fname == "2026-04-20_13-21_60651cdb_sync-and-ratelimit-lab.md"


def test_session_filename_falls_back_to_first_prompt():
    cfg = Config()
    naming = SessionNaming(
        session_id="08c10c01abc",
        project_raw="scratch",
        first_ts=datetime(2026, 4, 20, 14, 47, tzinfo=UTC),
        custom_title=None,
        first_prompt="Catch up session context across days",
    )
    fname = session_filename(naming, cfg, UTC)
    # slug_max_words = 5, so we get 5 words from the first prompt
    assert fname == "2026-04-20_14-47_08c10c01_catch-up-session-context-across.md"


def test_session_filename_empty_session_no_slug():
    cfg = Config()
    naming = SessionNaming(
        session_id="00294742abc",
        project_raw="scratch",
        first_ts=datetime(2026, 4, 20, 21, 0, tzinfo=UTC),
        custom_title=None,
        first_prompt=None,
    )
    fname = session_filename(naming, cfg, UTC)
    assert fname == "2026-04-20_21-00_00294742.md"


def test_session_filename_missing_timestamp():
    cfg = Config()
    naming = SessionNaming(
        session_id="aaaa",
        project_raw="x",
        first_ts=None,
        custom_title=None,
        first_prompt=None,
    )
    fname = session_filename(naming, cfg, UTC)
    # Short session IDs pad to 8 chars with zeros to keep the uniqueness suffix
    # predictable in length.
    assert fname == "0000-00-00_00-00_aaaa0000.md"


def test_session_filename_uses_local_tz():
    cfg = Config()
    cet = ZoneInfo("Europe/Prague")
    naming = SessionNaming(
        session_id="deadbeef",
        project_raw="p",
        first_ts=datetime(2026, 4, 20, 22, 0, tzinfo=UTC),
        custom_title="night session",
        first_prompt=None,
    )
    fname = session_filename(naming, cfg, cet)
    # CEST is UTC+2 in April, so 22:00 UTC = 00:00 CEST on the next day
    assert fname.startswith("2026-04-21_00-00_deadbeef_")


def test_session_relpath_per_project_dirs():
    cfg = Config()
    naming = SessionNaming(
        session_id="60651cdb",
        project_raw="Deep Value Scanner",
        first_ts=datetime(2026, 4, 20, 13, 21, tzinfo=UTC),
        custom_title="testing",
        first_prompt=None,
    )
    rel = session_relpath(naming, cfg, UTC)
    # Layout: sessions/<source>/<project>/<file>.md (ADR-0005)
    assert str(rel) == (
        "sessions/claude/deep-value-scanner/"
        "2026-04-20_13-21_60651cdb_testing.md"
    )


def test_session_relpath_flat_when_disabled():
    cfg = Config()
    cfg.session_files.per_project_dirs = False
    naming = SessionNaming(
        session_id="60651cdb",
        project_raw="scratch",
        first_ts=datetime(2026, 4, 20, 13, 21, tzinfo=UTC),
        custom_title="note",
        first_prompt=None,
    )
    rel = session_relpath(naming, cfg, UTC)
    assert str(rel) == "sessions/claude/2026-04-20_13-21_60651cdb_note.md"


def test_session_relpath_uses_source_segment():
    cfg = Config()
    naming = SessionNaming(
        session_id="abcdef12",
        project_raw="some-project",
        first_ts=datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
        custom_title=None,
        first_prompt=None,
        source="codex",
    )
    rel = session_relpath(naming, cfg, UTC)
    assert str(rel).startswith("sessions/codex/some-project/")


def test_daily_index_relpath():
    assert str(daily_index_relpath(date(2026, 4, 20))) == "daily/2026-04-20.md"
