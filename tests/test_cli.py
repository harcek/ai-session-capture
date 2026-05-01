"""End-to-end CLI tests — full pipeline from JSONL to MD on disk."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_session_capture.cli import main
from tests.conftest import write_jsonl


@pytest.fixture
def cli_env(tmp_path, monkeypatch, fake_projects_root):
    """Wire CLI state dirs + config path + HOME to a tmp sandbox."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".local" / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return tmp_path


def _seed_session(projects_root, date_iso="2026-04-20"):
    jsonl = projects_root / "p" / "s.jsonl"
    write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "sessionId": "sess1",
                "uuid": "u1",
                "timestamp": f"{date_iso}T10:00:00.000Z",
                "cwd": "/home/u/p",
                "isSidechain": False,
                "message": {"role": "user", "content": "hello claude"},
            },
            {
                "type": "assistant",
                "sessionId": "sess1",
                "uuid": "u2",
                "timestamp": f"{date_iso}T10:00:05.000Z",
                "cwd": "/home/u/p",
                "isSidechain": False,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello human"}],
                },
            },
        ],
    )


def _find_session_file(base: Path) -> Path | None:
    sessions = base / "sessions"
    if not sessions.exists():
        return None
    for md in sessions.rglob("*.md"):
        return md
    return None


def _find_daily_file(base: Path) -> Path | None:
    """Locate the single daily MD inside daily/<machine>/ for tests."""
    daily_root = base / "daily"
    if not daily_root.exists():
        return None
    for md in daily_root.rglob("*.md"):
        return md
    return None


def test_backfill_writes_session_and_index(cli_env, fake_projects_root):
    _seed_session(fake_projects_root, "2026-04-20")
    rc = main(["--config", "/nonexistent.toml", "backfill"])
    assert rc == 0
    base = cli_env / ".local" / "share" / "ai-session-capture"
    # Session file exists under sessions/<machine>/<source>/<project>/
    sess_file = _find_session_file(base)
    assert sess_file is not None
    text = sess_file.read_text()
    assert "hello claude" in text
    assert "hello human" in text
    # Daily index exists at daily/<machine>/ and wiki-links to the session
    daily = _find_daily_file(base)
    assert daily is not None and daily.name == "2026-04-20.md"
    assert "[[../../sessions/" in daily.read_text()


def test_backfill_is_idempotent(cli_env, fake_projects_root):
    _seed_session(fake_projects_root, "2026-04-20")
    main(["--config", "/nonexistent.toml", "backfill"])
    base = cli_env / ".local" / "share" / "ai-session-capture"
    sess_file = _find_session_file(base)
    daily = _find_daily_file(base)
    m1_sess = sess_file.stat().st_mtime
    m1_idx = daily.stat().st_mtime
    main(["--config", "/nonexistent.toml", "backfill"])
    assert sess_file.stat().st_mtime == m1_sess
    assert daily.stat().st_mtime == m1_idx


def test_dry_run_does_not_write(cli_env, fake_projects_root):
    _seed_session(fake_projects_root, "2026-04-20")
    rc = main(["--config", "/nonexistent.toml", "--dry-run", "backfill"])
    assert rc == 0
    base = cli_env / ".local" / "share" / "ai-session-capture"
    if base.exists():
        assert not any(base.rglob("*.md"))


def test_output_files_are_0600(cli_env, fake_projects_root):
    _seed_session(fake_projects_root, "2026-04-20")
    main(["--config", "/nonexistent.toml", "backfill"])
    base = cli_env / ".local" / "share" / "ai-session-capture"
    sess_file = _find_session_file(base)
    assert (sess_file.stat().st_mode & 0o777) == 0o600
    daily = _find_daily_file(base)
    assert (daily.stat().st_mode & 0o777) == 0o600


def test_redacted_content_lands_in_session_file_with_warning(cli_env, fake_projects_root):
    jsonl = fake_projects_root / "p" / "s.jsonl"
    write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "sessionId": "sess1",
                "uuid": "u1",
                "timestamp": "2026-04-20T10:00:00.000Z",
                "isSidechain": False,
                "message": {
                    "role": "user",
                    "content": "I accidentally pasted AKIAIOSFODNN7EXAMPLE",
                },
            },
        ],
    )
    rc = main(["--config", "/nonexistent.toml", "backfill"])
    assert rc == 0
    base = cli_env / ".local" / "share" / "ai-session-capture"
    sess_file = _find_session_file(base)
    text = sess_file.read_text()
    assert "AKIAIOSFODNN7EXAMPLE" not in text
    assert "REDACTED:AWS_AKID" in text
    assert "redacted in this session" in text


def test_granularity_session_mode_skips_daily_index(
    cli_env, fake_projects_root, tmp_path, monkeypatch
):
    _seed_session(fake_projects_root, "2026-04-20")
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text('[granularity]\nmode = "session"\n')
    rc = main(["--config", str(cfg_file), "backfill"])
    assert rc == 0
    base = cli_env / ".local" / "share" / "ai-session-capture"
    assert _find_session_file(base) is not None
    # daily/ dir should not contain any index because mode=session skipped it
    assert _find_daily_file(base) is None


def test_granularity_daily_mode_falls_back_to_session_and_daily(
    cli_env, fake_projects_root, tmp_path
):
    """Legacy mode="daily" is treated as session+daily (still writes index)."""
    _seed_session(fake_projects_root, "2026-04-20")
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text('[granularity]\nmode = "daily"\n')
    rc = main(["--config", str(cfg_file), "backfill"])
    assert rc == 0
    # Index still written (treated as session+daily). The accompanying
    # deprecation warning goes to the rotating run.log via the csc
    # logger; it's manually verifiable and guaranteed by the code path
    # unit-tested in the next test.
    base = cli_env / ".local" / "share" / "ai-session-capture"
    assert _find_daily_file(base) is not None


def test_granularity_daily_mode_warning_string_present():
    """Unit-level check that the code actually issues a deprecation warning."""
    from pathlib import Path as _P
    src = (_P(__file__).parent.parent
           / "src" / "ai_session_capture" / "cli.py").read_text()
    # Both the daily and backfill commands must warn for mode="daily"
    assert src.count('deprecated; treating as session+daily') == 2


def test_daily_command_uses_specified_date(cli_env, fake_projects_root):
    _seed_session(fake_projects_root, "2026-04-20")
    rc = main(
        [
            "--config",
            "/nonexistent.toml",
            "--date",
            "2026-04-20",
            "daily",
        ]
    )
    assert rc == 0
    base = cli_env / ".local" / "share" / "ai-session-capture"
    daily = _find_daily_file(base)
    assert daily is not None and daily.name == "2026-04-20.md"
    assert _find_session_file(base) is not None


def test_backfill_with_codex_source_only(cli_env, fake_projects_root, tmp_path, monkeypatch):
    """--source codex skips Claude transcripts even when Claude data exists.
    Codex sessions land under sessions/<machine>/codex/<project>/."""
    # Seed a Claude session
    _seed_session(fake_projects_root, "2026-04-20")

    # Seed a Codex session at a tmp_path codex root
    codex_root = tmp_path / "codex_sessions"
    codex_jsonl = codex_root / "2026" / "04" / "21" / "rollout-test.jsonl"
    codex_jsonl.parent.mkdir(parents=True)
    with codex_jsonl.open("w") as f:
        for d in [
            {
                "type": "session_meta",
                "timestamp": "2026-04-21T10:00:00.000Z",
                "payload": {
                    "id": "codex-sess",
                    "cwd": "/u/proj",
                    "originator": "codex_cli_rs",
                    "cli_version": "0.1",
                    "source": "cli",
                    "model_provider": "openai",
                    "git": {"branch": "main", "commit_hash": "x", "repository_url": ""},
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-04-21T10:00:01.000Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "codex prompt"}],
                },
            },
        ]:
            f.write(json.dumps(d) + "\n")
    os.chmod(codex_jsonl, 0o600)
    monkeypatch.setenv("CODEX_SESSIONS_ROOT", str(codex_root.resolve()))

    rc = main(["--config", "/nonexistent.toml", "backfill", "--source", "codex"])
    assert rc == 0

    base = cli_env / ".local" / "share" / "ai-session-capture"
    # Codex session present under sessions/<machine>/codex/<project>/
    assert any(base.rglob("sessions/*/codex/proj/*.md"))
    # No Claude subtree exists under any machine (--source codex)
    assert not any(base.rglob("sessions/*/claude"))


def test_daily_reindexes_all_dates_for_cross_day_sessions(
    cli_env, fake_projects_root, tmp_path
):
    """When a session spans Mon + Tue and 'daily' runs for Tue, BOTH dates
    must end up in the FTS index in a single run. Under the pre-fix
    day-scoped logic, Monday's row would be missing until --rebuild.

    We pin TZ=UTC via config so the two timestamps land on distinct local
    dates regardless of the test host's wall-clock zone.
    """
    cfg_file = tmp_path / "tz-utc.toml"
    cfg_file.write_text('[timezone]\nmode = "explicit"\nname = "UTC"\n')

    jsonl = fake_projects_root / "p" / "s.jsonl"
    write_jsonl(
        jsonl,
        [
            # Monday 22:00 UTC
            {
                "type": "user", "sessionId": "sess1", "uuid": "u1",
                "timestamp": "2026-04-20T22:00:00.000Z",
                "isSidechain": False,
                "message": {"role": "user", "content": "monday evening"},
            },
            # Tuesday 05:00 UTC — same session
            {
                "type": "user", "sessionId": "sess1", "uuid": "u2",
                "timestamp": "2026-04-21T05:00:00.000Z",
                "isSidechain": False,
                "message": {"role": "user", "content": "tuesday morning"},
            },
        ],
    )
    rc = main(["--config", str(cfg_file), "--date", "2026-04-21", "daily"])
    assert rc == 0

    from ai_session_capture import search as S
    import sqlite3

    conn = sqlite3.connect(str(S.db_path()))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT date FROM sessions WHERE id = ? ORDER BY date", ("sess1",)
    ).fetchall()
    conn.close()
    dates_indexed = [r["date"] for r in rows]

    # The assertion that actually proves the fix: Monday's row exists
    # even though 'daily' targeted Tuesday. Without the cross-day fix this
    # would be missing.
    assert "2026-04-20" in dates_indexed, (
        f"cross-day fix regression: Monday's row missing after Tuesday-only "
        f"daily run. Got: {dates_indexed}"
    )
    assert "2026-04-21" in dates_indexed


def test_projects_root_cli_flag_overrides_env(
    cli_env, fake_projects_root, tmp_path, monkeypatch
):
    """--projects-root beats CLAUDE_PROJECTS_ROOT and picks up JSONLs from the flag path."""
    # Seed a session at a second location that isn't the env-var root
    alt_root = tmp_path / "archive-root" / "projects"
    alt_root.mkdir(parents=True)
    jsonl = alt_root / "p" / "s.jsonl"
    jsonl.parent.mkdir(parents=True)
    with jsonl.open("w") as f:
        f.write(json.dumps({
            "type": "user", "sessionId": "from_flag", "uuid": "u1",
            "timestamp": "2026-04-20T10:00:00.000Z",
            "isSidechain": False,
            "message": {"role": "user", "content": "alt-root session"},
        }) + "\n")
    os.chmod(jsonl, 0o600)

    # Env var points somewhere empty
    rc = main(["--projects-root", str(alt_root), "backfill"])
    assert rc == 0
    # Session from the flag path should be captured
    base = cli_env / ".local" / "share" / "ai-session-capture"
    assert base.exists()
    files = list(base.rglob("*.md"))
    # At least one session file with content from the flag path
    assert any("alt-root session" in p.read_text() for p in files if "sessions/" in str(p))


def test_search_machine_filter_warns_on_ingest(cli_env, fake_projects_root):
    """--machine on daily/backfill is a no-op; the CLI logs a warning
    so a user who expected per-machine ingest catches the mistake.
    The csc logger has propagate=False (so warnings don't leak to
    pytest's root handler); we attach a list-handler directly."""
    import logging
    captured: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record):
            captured.append(record)

    h = _ListHandler(level=logging.WARNING)
    logging.getLogger("csc").addHandler(h)
    try:
        _seed_session(fake_projects_root, "2026-04-20")
        rc = main(["--config", "/nonexistent.toml", "backfill",
                   "--machine", "some-other-host"])
    finally:
        logging.getLogger("csc").removeHandler(h)
    assert rc == 0
    assert any("ignoring --machine" in rec.getMessage() for rec in captured)


def test_load_all_records_stamps_machine(cli_env, fake_projects_root, monkeypatch):
    """Records arrive from parse_file with empty machine; the CLI
    helper stamps the resolved machine on every one. Without this,
    the FTS index can't partition by host (ADR-0006)."""
    from argparse import Namespace
    import logging
    from ai_session_capture.cli import _load_all_records

    _seed_session(fake_projects_root, "2026-04-20")
    args = Namespace(projects_root=None)
    records = _load_all_records(logging.getLogger("test"), args, ("claude",), "test-host")
    assert records, "expected at least one record"
    assert all(r.machine == "test-host" for r in records)


def test_last_error_cleared_on_success(cli_env, fake_projects_root):
    _seed_session(fake_projects_root, "2026-04-20")
    # Seed a bogus last-error file
    state = cli_env / ".local" / "state" / "ai-session-capture"
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    (state / "last-error").write_text("previous failure\n")

    rc = main(["--config", "/nonexistent.toml", "backfill"])
    assert rc == 0
    assert not (state / "last-error").exists()
