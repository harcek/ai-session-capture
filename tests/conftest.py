"""Shared pytest fixtures.

Centralizes the bit of machinery we need for every test: a tmp_path-rooted
fake ``~/.claude/projects`` directory, accessible both as a ``Path`` and
via the ``CLAUDE_PROJECTS_ROOT`` env var so the parser's path-traversal
guard accepts files written under it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def fake_projects_root(tmp_path, monkeypatch):
    """Create a tmp projects-root dir and point the parser at it."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_ROOT", str(root.resolve()))
    return root


def write_jsonl(path: Path, messages: list[dict]) -> Path:
    """Serialize a list of message dicts to a JSONL file at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")
    os.chmod(path, 0o600)
    return path
