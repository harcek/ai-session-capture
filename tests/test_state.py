"""State, lock, and idempotency tests."""

from __future__ import annotations

import json

import pytest

from claude_session_capture import state as st


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect state_dir() to a tmp_path via XDG_STATE_HOME override."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(
        "CLAUDE_SESSION_CAPTURE_STATE_ROOT", str(tmp_path / "state" / "claude-session-capture")
    )
    # platformdirs reads XDG_STATE_HOME; our fallback reads ~/.local/state; we
    # force both to the tmp path by monkeypatching Path.home() too.
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return tmp_path


def test_atomic_write_creates_file_with_0600(tmp_path):
    path = tmp_path / "out.md"
    st.atomic_write_text(path, "hello")
    assert path.read_text() == "hello"
    import stat

    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_atomic_write_overwrites_cleanly(tmp_path):
    path = tmp_path / "out.md"
    st.atomic_write_text(path, "first")
    st.atomic_write_text(path, "second")
    assert path.read_text() == "second"


def test_atomic_write_leaves_no_tmp_on_success(tmp_path):
    path = tmp_path / "out.md"
    st.atomic_write_text(path, "hi")
    # No stray .tmp-* files beside it
    stragglers = [p for p in tmp_path.iterdir() if p.name.startswith(".tmp-")]
    assert stragglers == []


def test_write_at_first_call_writes(tmp_path):
    out = tmp_path / "out"
    cur = tmp_path / "cursor"
    cur.mkdir()
    assert st.write_at(out, "daily/2026-04-20.md", "hello world", cursor_root=cur) is True
    assert (out / "daily" / "2026-04-20.md").read_text() == "hello world"


def test_write_at_second_call_skips(tmp_path):
    out = tmp_path / "out"
    cur = tmp_path / "cursor"
    cur.mkdir()
    path = "daily/2026-04-20.md"
    st.write_at(out, path, "hello world", cursor_root=cur)
    mtime_before = (out / path).stat().st_mtime
    wrote = st.write_at(out, path, "hello world", cursor_root=cur)
    assert wrote is False
    mtime_after = (out / path).stat().st_mtime
    assert mtime_before == mtime_after


def test_write_at_rewrites_when_content_differs(tmp_path):
    out = tmp_path / "out"
    cur = tmp_path / "cursor"
    cur.mkdir()
    path = "sessions/proj/file.md"
    st.write_at(out, path, "first", cursor_root=cur)
    wrote = st.write_at(out, path, "second", cursor_root=cur)
    assert wrote is True
    assert (out / path).read_text() == "second"


def test_write_at_rewrites_when_target_deleted(tmp_path):
    out = tmp_path / "out"
    cur = tmp_path / "cursor"
    cur.mkdir()
    path = "sessions/proj/file.md"
    st.write_at(out, path, "text", cursor_root=cur)
    (out / path).unlink()
    wrote = st.write_at(out, path, "text", cursor_root=cur)
    assert wrote is True


def test_cursor_keyed_by_relative_path(tmp_path):
    """Namespacing via path ensures session files and daily indexes don't collide."""
    out = tmp_path / "out"
    cur = tmp_path / "cursor"
    cur.mkdir()
    st.write_at(out, "sessions/proj/a.md", "A content", cursor_root=cur)
    st.write_at(out, "daily/2026-04-20.md", "Day content", cursor_root=cur)
    data = json.loads((cur / "cursor.json").read_text())
    assert "sessions/proj/a.md" in data
    assert "daily/2026-04-20.md" in data


def test_write_at_creates_parent_dirs(tmp_path):
    out = tmp_path / "out"
    cur = tmp_path / "cursor"
    cur.mkdir()
    st.write_at(
        out, "sessions/deep-value-scanner/2026-04-20_x.md", "body", cursor_root=cur
    )
    assert (out / "sessions" / "deep-value-scanner" / "2026-04-20_x.md").exists()


def test_flock_blocks_second_acquirer(tmp_path):
    lock = tmp_path / "run.lock"
    with st.flock_exclusive(lock):
        with pytest.raises(RuntimeError, match="another run holds"):
            with st.flock_exclusive(lock):
                pass  # pragma: no cover


def test_content_hash_stable():
    assert st.content_hash("hello") == st.content_hash("hello")
    assert st.content_hash("hello") != st.content_hash("hello!")


def test_set_log_level_maps_strings_to_levels():
    """set_log_level maps config strings to python levels; bogus → INFO."""
    import logging

    logging.getLogger("csc").handlers.clear()
    st.setup_logging(verbose=False)

    st.set_log_level("debug")
    assert logging.getLogger("csc").level == logging.DEBUG
    st.set_log_level("warn")
    assert logging.getLogger("csc").level == logging.WARNING
    st.set_log_level("error")
    assert logging.getLogger("csc").level == logging.ERROR
    st.set_log_level("nonsense")
    assert logging.getLogger("csc").level == logging.INFO


def test_setup_logging_verbose_forces_debug():
    import logging

    logging.getLogger("csc").handlers.clear()
    logger = st.setup_logging(verbose=True)
    assert logger.level == logging.DEBUG
