"""File layout + filename generation for the session+daily output scheme.

The renderer asks here for "where does this session's file go?" and "what
does today's daily-index file path look like?" — we keep the naming logic
centralized so slug rules, project aliases, and filesystem-safety
sanitization live in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from .config import Config


_SLUG_WORD_RX = re.compile(r"[a-z0-9]+")
_PROJECT_UNSAFE_RX = re.compile(r"[^a-z0-9._-]")
_COLLAPSE_DASHES_RX = re.compile(r"-+")


def slugify(text: str | None, max_words: int, max_chars: int) -> str:
    """Pick the first ``max_words`` alphanumeric words, lowercase, dash-join.

    ``None`` / empty / all-punctuation input returns ``""``. First line
    only — we don't want multi-paragraph prompts bleeding into filenames.
    """
    if not text:
        return ""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    first_line = first_line.split(".")[0]  # first sentence, if any
    words = _SLUG_WORD_RX.findall(first_line.lower())
    if not words:
        return ""
    slug = "-".join(words[:max_words])
    return slug[:max_chars].rstrip("-")


def sanitize_project(name: str, cfg: Config) -> str:
    """Apply alias, then lowercase, replace unsafe chars, trim, cap length."""
    if not name:
        return cfg.session_files.fallback_project
    alias = cfg.projects.aliases.get(name)
    if alias is not None:
        name = alias
    lowered = name.lower()
    safe = _PROJECT_UNSAFE_RX.sub("-", lowered)
    safe = _COLLAPSE_DASHES_RX.sub("-", safe).strip("-.")
    safe = safe[: cfg.session_files.project_name_max_len]
    safe = safe.rstrip("-.")
    return safe or cfg.session_files.fallback_project


@dataclass
class SessionNaming:
    """Inputs the layout uses to decide a session file's path."""

    session_id: str
    project_raw: str
    first_ts: datetime | None
    custom_title: str | None
    first_prompt: str | None
    source: str = "claude"


def session_filename(naming: SessionNaming, cfg: Config, tz: ZoneInfo) -> str:
    """Return the ``<date>_<time>_<uuid-short>[_<slug>].md`` filename.

    - Date/time pinned to the session's first timestamp (local TZ). When no
      timestamp is available (malformed JSONL, empty session), we fall back
      to ``0000-00-00_00-00`` — it sorts first and is unambiguously "unknown."
    - The 8-char session-id short is always present for uniqueness, even
      when a slug is available.
    - Slug prefers the custom title (from ``/rename``), then the first
      substantive user prompt. Empty/missing slug is omitted; the file
      still has a unique, sortable name.
    """
    if naming.first_ts is not None:
        local = naming.first_ts.astimezone(tz)
        date_part = local.strftime("%Y-%m-%d")
        time_part = local.strftime("%H-%M")
    else:
        date_part = "0000-00-00"
        time_part = "00-00"

    sid_short = (naming.session_id[:8] if naming.session_id else "unknown").ljust(8, "0")

    slug_source = naming.custom_title or naming.first_prompt
    slug = slugify(
        slug_source,
        max_words=cfg.session_files.slug_max_words,
        max_chars=cfg.session_files.slug_max_chars,
    )

    parts = [date_part, time_part, sid_short]
    if slug:
        parts.append(slug)
    return "_".join(parts) + ".md"


def session_relpath(naming: SessionNaming, cfg: Config, tz: ZoneInfo) -> PurePosixPath:
    """Full relative path under the output dir for a session file.

    Layout: ``sessions/<source>/<project>/<file>.md`` (with the
    ``<project>/`` segment elided when ``per_project_dirs = false``).
    The ``<source>/`` segment lets per-source archives be wiped or
    rebuilt independently — see ADR-0005.
    """
    project = sanitize_project(naming.project_raw, cfg)
    source = naming.source or "claude"
    fname = session_filename(naming, cfg, tz)
    base = PurePosixPath("sessions") / source
    if cfg.session_files.per_project_dirs:
        return base / project / fname
    return base / fname


def daily_index_relpath(d: date) -> PurePosixPath:
    """``daily/YYYY-MM-DD.md`` — flat, no per-project subdirs."""
    return PurePosixPath("daily") / f"{d.isoformat()}.md"
