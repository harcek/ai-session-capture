"""State, lock, and idempotency tests."""

from __future__ import annotations

import json

import pytest

from ai_session_capture import state as st


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect state_dir() to a tmp_path via XDG_STATE_HOME override."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(
        "CLAUDE_SESSION_CAPTURE_STATE_ROOT", str(tmp_path / "state" / "ai-session-capture")
    )
    # platformdirs reads XDG_STATE_HOME; our fallback reads ~/.local/state; we
    # force both to the tmp path by monkeypatching Path.home() too.
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return tmp_path


def test_atomic_write_creates_file_with_0600(tmp_path):
    path = tmp_path / "out.md"
    st.atomic_write_text(path, "hello")
    assert path.read_text() == "hello"
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


# --- resolve_machine_name -------------------------------------------------


def test_resolve_machine_name_from_config():
    """Explicit cfg.machine.name wins, lowercased + sanitized."""
    from ai_session_capture.config import Config
    cfg = Config()
    cfg.machine.name = "MBP-Daniel"
    assert st.resolve_machine_name(cfg) == "mbp-daniel"


def test_resolve_machine_name_strips_dot_local():
    """Hostnames like `mbp.local` collapse to `mbp` so a Mac that
    flips between `.local` and bare hostname doesn't fragment the
    archive."""
    from ai_session_capture.config import Config
    cfg = Config()
    cfg.machine.name = "Daniels-MBP.local"
    assert st.resolve_machine_name(cfg) == "daniels-mbp"


def test_resolve_machine_name_sanitizes_disallowed_chars():
    """Anything outside [a-z0-9_-] collapses to a single dash."""
    from ai_session_capture.config import Config
    cfg = Config()
    cfg.machine.name = "host name!!  with$weird/chars"
    assert st.resolve_machine_name(cfg) == "host-name-with-weird-chars"


def test_resolve_machine_name_falls_back_to_hostname(monkeypatch):
    """Empty cfg.machine.name → socket.gethostname()."""
    from ai_session_capture.config import Config
    monkeypatch.setattr("socket.gethostname", lambda: "WORKSTATION.local")
    cfg = Config()
    assert st.resolve_machine_name(cfg) == "workstation"


def test_resolve_machine_name_blank_falls_back_to_unknown(monkeypatch):
    """If both cfg and hostname are blank, return `unknown` so paths
    are never empty (path-traversal-adjacent footgun)."""
    from ai_session_capture.config import Config
    monkeypatch.setattr("socket.gethostname", lambda: "")
    cfg = Config()
    assert st.resolve_machine_name(cfg) == "unknown"


# --- migrate_archive_to_per_machine ---------------------------------------


def test_migrate_archive_moves_legacy_layout(tmp_path):
    """A v0.2.0 archive (sessions/<source>/ + flat daily/<date>.md)
    is reshaped into v0.3.0's per-machine subtrees."""
    from ai_session_capture.config import Config

    output = tmp_path / "out"
    sessions = output / "sessions"
    daily = output / "daily"
    (sessions / "claude" / "proj").mkdir(parents=True)
    (sessions / "claude" / "proj" / "session.md").write_text("hi")
    (sessions / "codex" / "proj2").mkdir(parents=True)
    (sessions / "codex" / "proj2" / "s.md").write_text("hi")
    daily.mkdir()
    (daily / "2026-04-29.md").write_text("legacy daily")
    (daily / "2026-04-30.md").write_text("legacy daily")

    cfg = Config()
    cfg.output.dir = str(output)
    st.migrate_archive_to_per_machine(cfg, "mbp")

    # Sessions: legacy <source>/ moved under <machine>/<source>/
    assert (sessions / "mbp" / "claude" / "proj" / "session.md").exists()
    assert (sessions / "mbp" / "codex" / "proj2" / "s.md").exists()
    assert not (sessions / "claude").exists()
    assert not (sessions / "codex").exists()
    # Daily: flat MDs moved under daily/<machine>/
    assert (daily / "mbp" / "2026-04-29.md").exists()
    assert (daily / "mbp" / "2026-04-30.md").exists()
    assert not (daily / "2026-04-29.md").exists()


def test_migrate_archive_is_idempotent(tmp_path):
    """Re-running the migration on an already-migrated archive
    leaves it untouched (no-op)."""
    from ai_session_capture.config import Config

    output = tmp_path / "out"
    sessions = output / "sessions"
    daily = output / "daily"
    (sessions / "mbp" / "claude" / "proj").mkdir(parents=True)
    (sessions / "mbp" / "claude" / "proj" / "session.md").write_text("hi")
    (daily / "mbp").mkdir(parents=True)
    (daily / "mbp" / "2026-04-29.md").write_text("daily")

    cfg = Config()
    cfg.output.dir = str(output)
    st.migrate_archive_to_per_machine(cfg, "mbp")
    st.migrate_archive_to_per_machine(cfg, "mbp")  # no-op second pass

    assert (sessions / "mbp" / "claude" / "proj" / "session.md").exists()
    assert (daily / "mbp" / "2026-04-29.md").exists()


def test_migrate_archive_no_output_dir(tmp_path):
    """Missing output dir is a no-op rather than an error — first run
    on a fresh machine has no archive yet."""
    from ai_session_capture.config import Config

    cfg = Config()
    cfg.output.dir = str(tmp_path / "does-not-exist")
    st.migrate_archive_to_per_machine(cfg, "mbp")  # must not raise


# --- migrate_data_dir (data-dir default rename chain) ---------------------


def test_migrate_data_dir_v02_to_current(tmp_path, monkeypatch):
    """A v0.2.0 user with `~/.local/share/ai-sessions/` is moved to
    `~/.local/share/ai-session-capture/` on first v0.3.x run."""
    from ai_session_capture.config import Config

    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = tmp_path / ".local" / "share" / "ai-sessions"
    legacy.mkdir(parents=True)
    (legacy / "marker").write_text("v0.2 archive")

    cfg = Config()  # default output.dir = ~/.local/share/ai-session-capture
    st.migrate_data_dir(cfg)

    new = tmp_path / ".local" / "share" / "ai-session-capture"
    assert new.exists()
    assert (new / "marker").read_text() == "v0.2 archive"
    assert not legacy.exists()


def test_migrate_data_dir_v01_skips_to_current(tmp_path, monkeypatch):
    """A pre-v0.2.0 user with `~/.local/share/claude-sessions/` is
    moved straight to `ai-session-capture` (single rename, no
    intermediate `ai-sessions` stop)."""
    from ai_session_capture.config import Config

    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = tmp_path / ".local" / "share" / "claude-sessions"
    legacy.mkdir(parents=True)
    (legacy / "marker").write_text("v0.1 archive")

    cfg = Config()
    st.migrate_data_dir(cfg)

    new = tmp_path / ".local" / "share" / "ai-session-capture"
    assert new.exists()
    assert (new / "marker").read_text() == "v0.1 archive"
    assert not legacy.exists()


def test_migrate_data_dir_skips_when_target_exists(tmp_path, monkeypatch):
    """If both legacy and new dirs exist, the new dir wins — never
    clobber an in-place archive."""
    from ai_session_capture.config import Config

    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = tmp_path / ".local" / "share" / "ai-sessions"
    new = tmp_path / ".local" / "share" / "ai-session-capture"
    legacy.mkdir(parents=True)
    new.mkdir(parents=True)
    (legacy / "old-marker").write_text("legacy")
    (new / "new-marker").write_text("current")

    cfg = Config()
    st.migrate_data_dir(cfg)

    assert (new / "new-marker").exists()
    assert (legacy / "old-marker").exists()  # legacy left alone


def test_migrate_data_dir_skips_custom_output_dir(tmp_path, monkeypatch):
    """A user who set a non-default `output.dir` is fully opted out
    of the shim — their custom path is none of the migration's
    business."""
    from ai_session_capture.config import Config

    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = tmp_path / ".local" / "share" / "ai-sessions"
    legacy.mkdir(parents=True)
    (legacy / "marker").write_text("v0.2")

    cfg = Config()
    cfg.output.dir = str(tmp_path / "custom" / "place")
    st.migrate_data_dir(cfg)

    # legacy untouched; custom path not auto-created
    assert (legacy / "marker").exists()
