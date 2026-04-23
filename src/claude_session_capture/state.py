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
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


def state_dir() -> Path:
    """XDG state dir: ``~/.local/state/claude-session-capture/``."""
    try:
        from platformdirs import user_state_path

        d = user_state_path("claude-session-capture")
    except ImportError:
        d = Path.home() / ".local" / "state" / "claude-session-capture"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


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
