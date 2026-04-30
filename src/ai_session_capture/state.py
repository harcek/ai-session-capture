"""State, locking, atomic writes, idempotency gate.

All runtime state — cursors, lockfile, rotating run log, last-error
sentinel — lives under XDG_STATE_HOME. The output directory (the data
repo) is *not* state; it's user data and lives under XDG_DATA_HOME.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


def state_dir() -> Path:
    """XDG state dir: ``~/.local/state/ai-session-capture/``.

    Migration: a pre-v0.2.0 install used
    ``~/.local/state/claude-session-capture/``. If the legacy dir
    exists and the new one doesn't, ``rename`` it in place so cursors
    and the run log carry over.
    """
    try:
        from platformdirs import user_state_path

        d = user_state_path("ai-session-capture")
        legacy = user_state_path("claude-session-capture")
    except ImportError:
        base = Path.home() / ".local" / "state"
        d = base / "ai-session-capture"
        legacy = base / "claude-session-capture"
    if legacy.exists() and not d.exists():
        legacy.rename(d)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def migrate_data_dir(cfg) -> None:
    """Rename the legacy data dir on default-config installs.

    Pre-v0.2.0 the default output dir was
    ``~/.local/share/claude-sessions``. If the user is still on the
    default config (now ``~/.local/share/ai-sessions``) and only the
    legacy dir exists, move it so the existing archive is preserved.
    Custom output dirs are left untouched — they're the user's call.
    """
    DEFAULT_NEW = "~/.local/share/ai-sessions"
    LEGACY = "~/.local/share/claude-sessions"
    if cfg.output.dir != DEFAULT_NEW:
        return
    new_path = Path(DEFAULT_NEW).expanduser()
    legacy = Path(LEGACY).expanduser()
    if not new_path.exists() and legacy.exists():
        legacy.rename(new_path)


def migrate_archive_to_per_machine(cfg, machine: str) -> None:
    """Reshape a v0.2.0 archive into the v0.3.0 per-machine layout.

    v0.2.0 wrote to ``sessions/<source>/<project>/`` and
    ``daily/<date>.md``. v0.3.0 writes to
    ``sessions/<machine>/<source>/<project>/`` and
    ``daily/<machine>/<date>.md``. On first run after upgrade we
    detect a v0.2.0 shape (``sessions/claude/`` or ``sessions/codex/``
    sitting at the data dir root, or any ``daily/*.md`` file at the
    daily dir root) and move it under this machine's segment.

    Idempotent: re-running is a no-op once the shapes are migrated.
    Custom output dirs receive the same treatment — the migration
    operates on whatever ``cfg.output.dir`` resolves to.
    """
    output = Path(cfg.output.dir).expanduser()
    if not output.exists():
        return

    sessions = output / "sessions"
    if sessions.exists():
        # Move every legacy <source> dir directly under sessions/ into
        # sessions/<machine>/<source>/. We treat "all known source
        # values" as v0.2.0 leftovers — if the user happens to have a
        # machine named the same as a source, the per-machine target
        # subtree already exists and the rename is skipped.
        for legacy_source in ("claude", "codex"):
            legacy_dir = sessions / legacy_source
            if not legacy_dir.is_dir():
                continue
            machine_dir = sessions / machine
            machine_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = machine_dir / legacy_source
            if target.exists():
                continue
            legacy_dir.rename(target)

    daily = output / "daily"
    if daily.is_dir():
        # Any *.md sitting flat in daily/ is a v0.2.0 daily index.
        flat_dailies = [p for p in daily.iterdir() if p.is_file() and p.suffix == ".md"]
        if flat_dailies:
            machine_daily = daily / machine
            machine_daily.mkdir(mode=0o700, parents=True, exist_ok=True)
            for md in flat_dailies:
                target = machine_daily / md.name
                if target.exists():
                    continue
                md.rename(target)


_MACHINE_NAME_RE = re.compile(r"[^a-z0-9_-]+")


def resolve_machine_name(cfg) -> str:
    """Return a stable machine identity for this run.

    ``cfg.machine.name`` wins if set (after sanitization). Empty
    falls back to ``socket.gethostname()``. The resolved name is
    lowercased, has any trailing ``.local`` stripped, and any
    character outside ``[a-z0-9_-]`` collapsed to ``-``. An entirely
    blank result becomes ``"unknown"`` so paths are never empty.
    """
    import socket

    raw = (cfg.machine.name or socket.gethostname() or "").strip().lower()
    if raw.endswith(".local"):
        raw = raw[: -len(".local")]
    cleaned = _MACHINE_NAME_RE.sub("-", raw).strip("-")
    return cleaned or "unknown"


def _cursor_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / "cursor.json"


def _load_cursor(root: Path | None = None) -> dict[str, str]:
    path = _cursor_path(root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cursor(cursor: dict[str, str], root: Path | None = None) -> None:
    atomic_write_text(_cursor_path(root), json.dumps(cursor, indent=2, sort_keys=True))


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically, 0o600 mode, no symlinks."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".tmp-",
        suffix=path.suffix or ".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def flock_exclusive(path: Path):
    """Non-blocking exclusive file lock — raises if another run is active."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RuntimeError(f"another run holds the lock at {path}") from e
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def write_at(
    output_dir: Path,
    relpath: str | Path,
    md_text: str,
    *,
    cursor_key: str | None = None,
    cursor_root: Path | None = None,
) -> bool:
    """Write ``relpath`` under ``output_dir`` only when content hash differs.

    ``relpath`` is a POSIX-style path inside ``output_dir`` — the renderer
    provides these (``sessions/<project>/<file>.md``,
    ``daily/<date>.md``). ``cursor_key`` defaults to ``str(relpath)`` so
    the idempotency gate naturally namespaces session vs. daily files
    under different keys.

    Returns True if the file was (over)written, False if the write was
    skipped because the content hash in ``cursor.json`` already matches.
    The file-exists check on the target guards against cursor/actual drift
    — if someone deleted the MD, we re-write regardless of the cursor.
    """
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    target = output_dir / str(relpath)
    key = cursor_key or str(relpath)

    new_hash = content_hash(md_text)
    cursor = _load_cursor(cursor_root)
    if cursor.get(key) == new_hash and target.exists():
        return False

    atomic_write_text(target, md_text)
    cursor[key] = new_hash
    _save_cursor(cursor, cursor_root)
    return True


def write_last_error(message: str) -> Path:
    """Record the most recent failure as a sentinel file. Overwrites prior."""
    path = state_dir() / "last-error"
    atomic_write_text(
        path, f"{datetime.utcnow().isoformat()}Z\n{message}\n"
    )
    return path


def clear_last_error() -> None:
    path = state_dir() / "last-error"
    try:
        path.unlink()
    except FileNotFoundError:
        pass


_LEVEL_NAMES = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def set_log_level(level: str) -> None:
    """Set the csc logger's level from a config string.

    Unknown level names fall back to INFO — silently, so a typo in
    ``cfg.logging.level`` can't wedge a headless 06:00 run.
    """
    logging.getLogger("csc").setLevel(
        _LEVEL_NAMES.get((level or "info").lower(), logging.INFO)
    )


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure logging once: rotating file in state_dir + stderr if TTY.

    Bootstraps at DEBUG when ``verbose=True``, otherwise INFO. Callers that
    want to honor ``cfg.logging.level`` should call :func:`set_log_level`
    after config has been loaded — ``setup_logging`` can't take level
    directly because it's called before the config is available.
    """
    logger = logging.getLogger("csc")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    log_path = state_dir() / "run.log"
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    try:
        os.chmod(log_path, 0o600)
    except OSError:
        pass

    if os.isatty(2):
        stderr_handler = logging.StreamHandler()
        stderr_handler.setFormatter(fmt)
        logger.addHandler(stderr_handler)

    logger.propagate = False
    return logger


def notify_failure(title: str, message: str) -> None:
    """Best-effort desktop notification. Silent on failure / headless envs."""
    if os.isatty(2):
        # Interactive run — stderr already got the traceback, don't double-notify.
        return
    try:
        if platform.system() == "Darwin":
            # AppleScript string-quoting: escape double quotes and backslashes.
            safe = message.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{safe}" with title "{title}"',
                ],
                timeout=5,
                check=False,
                capture_output=True,
            )
        elif platform.system() == "Linux":
            subprocess.run(
                ["notify-send", title, message],
                timeout=5,
                check=False,
                capture_output=True,
            )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
