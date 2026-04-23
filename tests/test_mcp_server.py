"""MCP server tests — validates tool schemas and handler behavior.

We test the pure-Python handlers directly (no MCP runtime needed) plus
assert the tool schemas themselves are well-formed. Full stdio transport
testing is out of scope — the MCP SDK covers that.
"""

from __future__ import annotations

import json

import pytest

from claude_session_capture import mcp_server as M
from claude_session_capture import search as S


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "index.db"
    monkeypatch.setattr(S, "db_path", lambda: path)
    rows = [
        S.SessionIndexRow(
            id="s1", date="2026-04-20", project="alpha", cwd="/a/work",
            first_ts="2026-04-20T10:00:00", turn_count=12, redactions_total=2,
            content="the rate limit discussion with exponential backoff",
        ),
        S.SessionIndexRow(
            id="s2", date="2026-04-19", project="beta", cwd="/b", first_ts="",
            turn_count=5, redactions_total=0,
            content="scanner rewrite architecture",
        ),
        S.SessionIndexRow(
            id="s3", date="2026-04-18", project="alpha", cwd="/a/play",
            first_ts="2026-04-18T09:00:00", turn_count=1, redactions_total=0,
            content="short note",
        ),
    ]
    S.upsert_rows(rows, path=path)
    return path


def test_tool_schemas_are_wellformed():
    """Every declared tool has name, description, and a JSONSchema inputSchema."""
    names = {t["name"] for t in M.TOOLS_SCHEMA}
    assert names == set(M.HANDLERS.keys())  # tools ↔ handlers one-to-one
    for t in M.TOOLS_SCHEMA:
        assert t["name"]
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"
        assert "properties" in t["inputSchema"]


def test_search_sessions_handler_returns_json(db):
    result = M.handle_search_sessions({"query": "rate limit"})
    payload = json.loads(result)
    assert payload["count"] == 1
    r = payload["results"][0]
    assert r["session_id"] == "s1"
    assert r["project"] == "alpha"
    assert "[rate] [limit]" in r["snippet"] or "[rate limit]" in r["snippet"]


def test_search_sessions_filters_apply(db):
    # project filter
    only_alpha = json.loads(M.handle_search_sessions({"query": "rate", "project": "alpha"}))
    assert all(r["project"] == "alpha" for r in only_alpha["results"])

    # date range
    recent = json.loads(
        M.handle_search_sessions({"query": "rate OR scanner", "since": "2026-04-20"})
    )
    assert all(r["date"] >= "2026-04-20" for r in recent["results"])


def test_list_projects_handler(db):
    result = json.loads(M.handle_list_projects({}))
    by_name = {p["project"]: p for p in result}
    assert by_name["alpha"]["n"] == 2
    assert by_name["beta"]["n"] == 1


def test_list_recent_sessions_handler(db):
    result = json.loads(M.handle_list_recent_sessions({"limit": 2}))
    assert [r["session_id"] for r in result] == ["s1", "s2"]

    only_alpha = json.loads(M.handle_list_recent_sessions({"limit": 5, "project": "alpha"}))
    assert {r["session_id"] for r in only_alpha} == {"s1", "s3"}


def test_get_session_text_handler(db):
    found = json.loads(M.handle_get_session_text({"session_id": "s1"}))
    assert found["found"] is True
    assert "rate limit discussion" in found["content"]

    missing = json.loads(M.handle_get_session_text({"session_id": "nope"}))
    assert missing["found"] is False


def test_search_sessions_handles_invalid_fts_gracefully(db):
    """An invalid FTS query returns a structured error object, not a traceback."""
    result = json.loads(M.handle_search_sessions({"query": '"unbalanced'}))
    assert result.get("error") == "invalid_fts_query"
    assert "message" in result
    assert "hint" in result


def test_mcp_limit_accepts_non_numeric_gracefully(db):
    """An MCP client passing limit=\"abc\" must not crash the handler."""
    result = json.loads(M.handle_search_sessions({"query": "alpha", "limit": "abc"}))
    # Falls back to the clamp default; query still runs
    assert "error" not in result or result.get("error") != "invalid_fts_query"


def test_mcp_list_recent_accepts_non_numeric_limit(db):
    """Same for list_recent_sessions."""
    result = json.loads(M.handle_list_recent_sessions({"limit": "not-a-number"}))
    assert isinstance(result, list)


def test_get_session_text_with_date_disambiguator(db):
    found = json.loads(
        M.handle_get_session_text({"session_id": "s1", "date": "2026-04-20"})
    )
    assert found["found"] is True
    assert found["date"] == "2026-04-20"


def test_build_server_returns_mcp_server():
    """Smoke test that the runtime wiring doesn't crash on construction.

    The [mcp] extra is optional — skip this test gracefully if the SDK
    isn't installed. All other MCP tests exercise the handler layer
    directly and don't need the runtime.
    """
    pytest.importorskip("mcp")
    server = M.build_server()
    assert server is not None
    # The Server object has a name attribute from the SDK
    assert hasattr(server, "name")
