"""MCP server — exposes the local session archive to Claude Code.

Runs as a stdio MCP server so it drops cleanly into any Claude Code
settings ``mcpServers`` config. Every tool is a read-only query over
the SQLite FTS index built by ``search.py``; no writes, no shell-outs,
no network.

Requires the optional ``mcp`` dependency:
    pip install claude-session-capture[mcp]

Run:
    claude-session-capture mcp-serve

Wire into Claude Code (``~/.claude/settings.json``):

    {
      "mcpServers": {
        "claude-sessions": {
          "command": "claude-session-capture",
          "args": ["mcp-serve"]
        }
      }
    }
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from . import search as search_mod


SERVER_NAME = "claude-session-capture"
SERVER_VERSION = "0.2.0"


TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "search_sessions",
        "description": (
            "Search the user's personal Claude Code session archive using "
            "SQLite FTS5. Supports phrase queries (\"rate limiting\"), "
            "AND/OR/NOT, and prefix wildcards (foo*). Returns matched "
            "sessions with date, project, and a highlighted snippet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "FTS5 query string.",
                },
                "project": {
                    "type": "string",
                    "description": "Optional project name to filter by.",
                },
                "since": {
                    "type": "string",
                    "description": "Optional inclusive start date YYYY-MM-DD.",
                },
                "until": {
                    "type": "string",
                    "description": "Optional inclusive end date YYYY-MM-DD.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 20).",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_projects",
        "description": (
            "List distinct projects in the archive with session counts and "
            "date ranges. Useful for discovering what's been captured."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_recent_sessions",
        "description": (
            "List the most-recent captured sessions, newest first, optionally "
            "filtered by project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
                "project": {"type": "string"},
            },
        },
    },
    {
        "name": "get_session_text",
        "description": (
            "Retrieve the full redacted text of a specific session by id. "
            "Pin to a date if the session spans multiple days."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "date": {
                    "type": "string",
                    "description": "Optional YYYY-MM-DD date disambiguator.",
                },
            },
            "required": ["session_id"],
        },
    },
]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


# --- handlers (pure functions so tests don't need an MCP runtime) ---------

def handle_search_sessions(args: dict) -> str:
    try:
        results = search_mod.search(
            args["query"],
            project=args.get("project"),
            since=_parse_date(args.get("since")),
            until=_parse_date(args.get("until")),
            # Pass raw — search._clamp_limit handles TypeError/ValueError
            # from non-numeric or missing values. Pre-int()-ing here would
            # crash on limit="abc".
            limit=args.get("limit", search_mod.SEARCH_LIMIT_DEFAULT),
        )
    except search_mod.FTSSyntaxError as e:
        return json.dumps(
            {
                "error": "invalid_fts_query",
                "message": str(e),
                "hint": (
                    'use double-quoted phrases ("rate limit"), AND/OR/NOT, '
                    "or prefix wildcards (foo*)."
                ),
            }
        )
    payload = [
        {
            "session_id": r.session_id,
            "date": r.date,
            "project": r.project,
            "cwd": r.cwd,
            "first_ts": r.first_ts,
            "turn_count": r.turn_count,
            "redactions_total": r.redactions_total,
            "snippet": r.snippet,
        }
        for r in results
    ]
    return json.dumps({"count": len(payload), "results": payload}, indent=2)


def handle_list_projects(args: dict) -> str:
    return json.dumps(search_mod.list_projects(), indent=2)


def handle_list_recent_sessions(args: dict) -> str:
    return json.dumps(
        search_mod.list_recent(
            # Pass raw; list_recent → _clamp_limit handles coercion
            limit=args.get("limit", 10),
            project=args.get("project"),
        ),
        indent=2,
    )


def handle_get_session_text(args: dict) -> str:
    got = search_mod.get_session_text(
        args["session_id"],
        date_str=args.get("date"),
    )
    if got is None:
        return json.dumps({"found": False})
    return json.dumps({"found": True, **got}, indent=2)


HANDLERS = {
    "search_sessions": handle_search_sessions,
    "list_projects": handle_list_projects,
    "list_recent_sessions": handle_list_recent_sessions,
    "get_session_text": handle_get_session_text,
}


# --- MCP runtime wiring ---------------------------------------------------

def build_server():
    """Construct the MCP Server with registered tool handlers.

    Kept as a separate function so tests can drive the handlers directly
    (via ``HANDLERS``) without paying for an MCP runtime.
    """
    from mcp.server import Server
    from mcp import types

    server: Any = Server(SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [types.Tool(**t) for t in TOOLS_SCHEMA]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        handler = HANDLERS.get(name)
        if handler is None:
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps({"error": f"unknown tool: {name}"}),
                )
            ]
        try:
            text = handler(arguments or {})
        except Exception as exc:  # noqa: BLE001
            text = json.dumps({"error": type(exc).__name__, "message": str(exc)})
        return [types.TextContent(type="text", text=text)]

    return server


def run_stdio() -> int:
    """Block on an MCP stdio session. Returns exit code."""
    import asyncio

    from mcp.server.stdio import stdio_server

    server = build_server()

    async def _run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 130
    return 0
