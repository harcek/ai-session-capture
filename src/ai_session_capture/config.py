"""Config: strict-enough TOML loading with dataclass defaults.

No pydantic — at this size, a dozen dataclasses plus a two-line
``from_dict`` beat the dep. If a user puts a typo in their TOML, we
silently ignore the unknown field rather than blowing up a headless
06:00 run; the defaults keep the tool usable either way.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path


@dataclass
class GranularityConfig:
    # "session+daily" (default): per-session MD files + a daily index that
    # backlinks to sessions touching that day. "daily" = legacy single-file-
    # per-day layout (deprecated). "session" = per-session only, no index.
    mode: str = "session+daily"


@dataclass
class SessionFilesConfig:
    slug_max_words: int = 5
    slug_max_chars: int = 60
    project_name_max_len: int = 48
    fallback_project: str = "_scratch"
    per_project_dirs: bool = True


@dataclass
class ProjectsConfig:
    """Aliases from cwd-derived names to display names.

    Example TOML::

        [projects.aliases]
        home-openclaw = "_scratch"
        tmp = "_scratch"
        openclaw-workspace-projects-deep-value-scanner = "deep-value-scanner"
    """

    aliases: dict[str, str] = field(default_factory=dict)


@dataclass
class ContentConfig:
    # Real toggles (exercised by render + search + tests).
    tool_calls: str = "summary"  # "off" | "summary" | "full"
    tool_results: str = "summary"  # "off" | "summary" | "full"
    sidechain: str = "summary"  # "off" | "summary" | "full"
    slash_commands: bool = True


@dataclass
class FormattingConfig:
    max_message_chars: int = 8000  # 0 disables truncation


@dataclass
class FrontmatterConfig:
    enabled: bool = True


@dataclass
class OutputConfig:
    dir: str = "~/.local/share/ai-session-capture"
    frontmatter: FrontmatterConfig = field(default_factory=FrontmatterConfig)


@dataclass
class RedactionConfig:
    enabled: bool = True


@dataclass
class TimezoneConfig:
    mode: str = "auto"  # "auto" | "explicit"
    name: str = ""  # IANA name used when mode != "auto"


@dataclass
class LoggingConfig:
    # "error" | "warn" | "info" | "debug". Honored by setup_logging.
    level: str = "info"


@dataclass
class MachineConfig:
    # Stable identity for this machine inside the multi-machine archive.
    # Empty → resolve_machine_name() falls back to a sanitized
    # socket.gethostname(). Set explicitly when the hostname is
    # unstable (e.g. ".local" suffix flips, OS-managed renames).
    name: str = ""


@dataclass
class Config:
    granularity: GranularityConfig = field(default_factory=GranularityConfig)
    content: ContentConfig = field(default_factory=ContentConfig)
    formatting: FormattingConfig = field(default_factory=FormattingConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    redaction: RedactionConfig = field(default_factory=RedactionConfig)
    timezone: TimezoneConfig = field(default_factory=TimezoneConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    session_files: SessionFilesConfig = field(default_factory=SessionFilesConfig)
    projects: ProjectsConfig = field(default_factory=ProjectsConfig)
    machine: MachineConfig = field(default_factory=MachineConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        cfg = cls()
        for section_field in fields(cfg):
            section_obj = getattr(cfg, section_field.name)
            if not is_dataclass(section_obj):
                continue
            section_data = data.get(section_field.name, {})
            if not isinstance(section_data, dict):
                continue
            _merge_into(section_obj, section_data)
        return cfg

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        if path is None or not path.exists():
            return cls()
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls.from_dict(data)


def _merge_into(target, data: dict) -> None:
    """Write known fields from ``data`` into ``target`` (a dataclass).

    Unknown fields are ignored. Nested dataclass fields (e.g.,
    ``output.frontmatter``) recurse so `[output.frontmatter] enabled = false`
    in TOML flows into the right nested object.
    """
    valid = {f.name: f for f in fields(target)}
    for k, v in data.items():
        if k not in valid:
            continue
        current = getattr(target, k)
        if is_dataclass(current) and isinstance(v, dict):
            _merge_into(current, v)
        else:
            setattr(target, k, v)


def default_config_path() -> Path:
    """XDG config path: ``~/.config/ai-session-capture/config.toml``."""
    try:
        from platformdirs import user_config_path

        return user_config_path("ai-session-capture") / "config.toml"
    except ImportError:
        return Path.home() / ".config" / "ai-session-capture" / "config.toml"
