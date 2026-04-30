"""Codex parser tests — schema dispatch, structural drops, priming skip."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_session_capture.codex_parser import (
    collect_codex_meta,
    default_codex_root,
    iter_codex_jsonls,
    parse_codex_file,
)
from tests.conftest import write_jsonl


@pytest.fixture
def fake_codex_root(tmp_path, monkeypatch):
    """tmp_path-rooted fake ~/.codex/sessions, env-redirected."""
    root = tmp_path / "codex_sessions"
    root.mkdir()
    monkeypatch.setenv("CODEX_SESSIONS_ROOT", str(root.resolve()))
    return root


@pytest.fixture(autouse=True)
def _reset_csc_logger():
    """Other tests (`test_state`) call ``setup_logging`` which sets
    ``propagate=False`` on the ``csc`` logger. Once that's set, pytest's
    ``caplog`` (which hooks the root logger) can't capture anything.
    Reset before every test in this file so caplog-based assertions
    work regardless of test ordering.
    """
    import logging
    csc = logging.getLogger("csc")
    saved_handlers = list(csc.handlers)
    saved_propagate = csc.propagate
    csc.handlers.clear()
    csc.propagate = True
    yield
    csc.handlers = saved_handlers
    csc.propagate = saved_propagate


# Reuses write_jsonl from conftest — same output (one dict per line, mode 0600).
write_codex = write_jsonl


def _meta(session_id="s1", cwd="/home/u/proj"):
    return {
        "type": "session_meta",
        "timestamp": "2026-04-22T10:00:00.000Z",
        "payload": {
            "id": session_id,
            "cwd": cwd,
            "originator": "codex_cli_rs",
            "cli_version": "0.114.0",
            "source": "cli",
            "model_provider": "openai",
            "git": {"branch": "main", "commit_hash": "abc", "repository_url": ""},
        },
    }


def _user_msg(text, ts="2026-04-22T10:00:01.000Z"):
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _user_msg_multi(texts, ts="2026-04-22T10:00:01.000Z"):
    """User message with multiple input_text blocks (priming + actual)."""
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": t} for t in texts],
        },
    }


def _assistant_msg(text, ts="2026-04-22T10:00:02.000Z"):
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _function_call(name, args_obj, call_id="call-1", ts="2026-04-22T10:00:03.000Z"):
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "function_call",
            "name": name,
            "call_id": call_id,
            "arguments": json.dumps(args_obj),
        },
    }


def _function_output(output, call_id="call-1", ts="2026-04-22T10:00:04.000Z"):
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        },
    }


# --- happy-path tests ----------------------------------------------------

def test_parse_clean_session(fake_codex_root):
    jsonl = fake_codex_root / "2026" / "04" / "22" / "rollout-test.jsonl"
    write_codex(
        jsonl,
        [
            _meta(session_id="sess-1", cwd="/home/u/myproj"),
            _user_msg("hello codex"),
            _assistant_msg("hello human"),
        ],
    )
    records = list(parse_codex_file(jsonl))
    assert len(records) == 2
    assert records[0].kind == "user"
    assert records[0].content == "hello codex"
    assert records[0].project == "myproj"
    assert records[0].source == "codex"
    assert records[1].kind == "assistant"
    assert records[1].content == "hello human"
    assert records[1].source == "codex"


def test_session_id_propagates_from_session_meta(fake_codex_root):
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(session_id="my-uuid-here"),
            _user_msg("hi"),
        ],
    )
    records = list(parse_codex_file(jsonl))
    assert records[0].session_id == "my-uuid-here"


def test_project_derived_from_cwd_path(fake_codex_root):
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(cwd="/Users/dan/work/the-thing"),
            _user_msg("hi"),
        ],
    )
    records = list(parse_codex_file(jsonl))
    assert records[0].project == "the-thing"


# --- system-priming filtering -------------------------------------------

@pytest.mark.parametrize(
    "priming_text",
    [
        "<environment_context><cwd>/path</cwd></environment_context>",
        "# AGENTS.md instructions for /home/x/y\n<INSTRUCTIONS>\n…",
        "<user_instructions>do the thing</user_instructions>",
        "<system>act professional</system>",
    ],
)
def test_priming_blocks_skipped(fake_codex_root, priming_text):
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            _user_msg_multi([priming_text, "the actual prompt"]),
        ],
    )
    records = list(parse_codex_file(jsonl))
    # Priming text must not appear; the real prompt must
    assert priming_text not in records[0].content
    assert "the actual prompt" in records[0].content


def test_first_prompt_uses_first_non_priming_block(fake_codex_root):
    """collect_codex_meta picks the first non-priming user input as first_prompt."""
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(session_id="s1"),
            _user_msg_multi(
                [
                    "<environment_context><cwd>/foo</cwd></environment_context>",
                    "real prompt here",
                ]
            ),
        ],
    )
    meta = collect_codex_meta(jsonl)
    assert meta["s1"].first_prompt == "real prompt here"


# --- event_msg.user_message redundancy guard ----------------------------

def test_event_msg_user_message_skipped(fake_codex_root):
    """event_msg.user_message must not produce a Record (response_item is canonical)."""
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            _user_msg("the real prompt"),
            {
                "type": "event_msg",
                "timestamp": "2026-04-22T10:00:01.500Z",
                "payload": {
                    "type": "user_message",
                    "message": "the real prompt",
                    "text_elements": [],
                    "images": [],
                    "local_images": [],
                },
            },
        ],
    )
    records = list(parse_codex_file(jsonl))
    assert len(records) == 1
    assert records[0].content == "the real prompt"


def test_event_msg_with_rich_payload_logs_warning(fake_codex_root, caplog):
    """If a future Codex version populates text_elements/images, we log it."""
    import logging
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            _user_msg("text-side prompt"),
            {
                "type": "event_msg",
                "timestamp": "2026-04-22T10:00:01.500Z",
                "payload": {
                    "type": "user_message",
                    "message": "text-side prompt",
                    "text_elements": [{"kind": "snippet"}],
                    "images": [],
                    "local_images": [{"path": "/tmp/x.png"}],
                },
            },
        ],
    )
    with caplog.at_level(logging.DEBUG, logger="csc"):
        list(parse_codex_file(jsonl))
    assert any("rich payload" in r.message for r in caplog.records)


def test_event_msg_other_types_skipped(fake_codex_root):
    """task_started, task_complete, token_count, agent_message — all skipped."""
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            {
                "type": "event_msg",
                "timestamp": "2026-04-22T10:00:00Z",
                "payload": {"type": "task_started", "turn_id": "t1"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-04-22T10:00:01Z",
                "payload": {"type": "agent_message", "message": "agent thinking"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-04-22T10:00:02Z",
                "payload": {"type": "token_count", "info": {"in": 100, "out": 50}},
            },
            _user_msg("the real one"),
        ],
    )
    records = list(parse_codex_file(jsonl))
    assert len(records) == 1
    assert records[0].content == "the real one"


# --- tool-call pairing ---------------------------------------------------

def test_tool_call_attaches_to_assistant(fake_codex_root):
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            _user_msg("run pwd please"),
            _assistant_msg("checking"),
            _function_call("exec_command", {"cmd": "pwd", "workdir": "/tmp"}),
            _function_output("/tmp\n"),
            _user_msg("thanks"),
        ],
    )
    records = list(parse_codex_file(jsonl))
    user1, asst, user2 = records
    # Tool call attached to the assistant message
    assert len(asst.tool_calls) == 1
    assert asst.tool_calls[0]["name"] == "exec_command"
    assert asst.tool_calls[0]["input"]["command"] == "pwd"
    assert asst.tool_calls[0]["dropped"] is False
    # Tool result attached to the next user message
    assert len(user2.tool_results) == 1
    assert user2.tool_results[0]["content"] == "/tmp\n"
    assert user2.tool_results[0]["dropped"] is False


def test_function_call_dropped_for_sensitive_command(fake_codex_root):
    """exec_command with `env` or `cat .env` must drop the matching output."""
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            _user_msg("show me env"),
            _assistant_msg("checking"),
            _function_call("exec_command", {"cmd": "env", "workdir": "/tmp"}),
            _function_output("AWS_SECRET=abc\nAPI_KEY=xyz"),
            _user_msg("ok"),
        ],
    )
    records = list(parse_codex_file(jsonl))
    asst = records[1]
    user2 = records[2]
    assert asst.tool_calls[0]["dropped"] is True
    assert user2.tool_results[0]["dropped"] is True
    assert user2.tool_results[0]["content"] == ""
    # raw secret strings must not appear anywhere
    assert "AWS_SECRET" not in str(user2.tool_results)
    assert "abc" not in str(user2.tool_results)


def test_function_call_dropped_for_sensitive_path(fake_codex_root):
    """A read of a credential file via Codex's read tool drops content."""
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            _user_msg("read this"),
            _assistant_msg("checking"),
            _function_call("read_file", {"file_path": "/home/u/.aws/credentials"}),
            _function_output("[default]\nkey=AKIAFAKE"),
            _user_msg("done"),
        ],
    )
    records = list(parse_codex_file(jsonl))
    asst = records[1]
    user2 = records[2]
    assert asst.tool_calls[0]["dropped"] is True
    assert user2.tool_results[0]["content"] == ""
    assert "AKIAFAKE" not in str(user2.tool_results)


# --- robustness ---------------------------------------------------------

def test_malformed_line_skipped(fake_codex_root):
    jsonl = fake_codex_root / "rollout-x.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("w") as f:
        f.write(json.dumps(_meta(session_id="s1")) + "\n")
        f.write("not valid json\n")
        f.write(json.dumps(_user_msg("after the bad line")) + "\n")
    os.chmod(jsonl, 0o600)
    records = list(parse_codex_file(jsonl))
    assert len(records) == 1
    assert records[0].content == "after the bad line"


def test_path_outside_root_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSIONS_ROOT", str(tmp_path / "ok"))
    (tmp_path / "ok").mkdir()
    stray = tmp_path / "stray.jsonl"
    stray.write_text("{}\n")
    os.chmod(stray, 0o600)
    with pytest.raises(ValueError, match="refusing"):
        list(parse_codex_file(stray))


def test_iter_codex_jsonls_walks_date_buckets(fake_codex_root):
    """Files under YYYY/MM/DD/ are picked up; non-rollout files ignored."""
    write_codex(fake_codex_root / "2026" / "04" / "22" / "rollout-a.jsonl",
                [_meta(session_id="a"), _user_msg("x")])
    write_codex(fake_codex_root / "2026" / "04" / "23" / "rollout-b.jsonl",
                [_meta(session_id="b"), _user_msg("y")])
    # Non-rollout file should be ignored
    other = fake_codex_root / "2026" / "04" / "23" / "other.jsonl"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(json.dumps(_meta(session_id="c")) + "\n")
    os.chmod(other, 0o600)

    paths = list(iter_codex_jsonls())
    assert len(paths) == 2
    assert all(p.name.startswith("rollout-") for p in paths)


def test_default_codex_root_precedence(tmp_path, monkeypatch):
    """CODEX_SESSIONS_ROOT > CODEX_HOME/sessions > ~/.codex/sessions"""
    explicit = tmp_path / "from_env"
    explicit.mkdir()
    home_dir = tmp_path / "alt_codex"
    (home_dir / "sessions").mkdir(parents=True)

    monkeypatch.setenv("CODEX_SESSIONS_ROOT", str(explicit))
    monkeypatch.setenv("CODEX_HOME", str(home_dir))
    assert default_codex_root() == explicit.resolve()

    monkeypatch.delenv("CODEX_SESSIONS_ROOT")
    assert default_codex_root() == (home_dir / "sessions").resolve()

    monkeypatch.delenv("CODEX_HOME")
    assert default_codex_root() == Path("~/.codex/sessions").expanduser().resolve()


# --- edge cases surfaced by the post-commit review --------------------


def test_uuid_unique_within_same_timestamp(fake_codex_root):
    """Multiple records sharing a timestamp must each get a distinct uuid."""
    same_ts = "2026-04-22T10:00:00.000Z"
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(session_id="s1"),
            _user_msg("first", ts=same_ts),
            _assistant_msg("response", ts=same_ts),
            _user_msg("second", ts=same_ts),
        ],
    )
    records = list(parse_codex_file(jsonl))
    uuids = [r.uuid for r in records]
    assert len(uuids) == len(set(uuids)), f"uuid collision: {uuids}"


def test_reasoning_attaches_to_buffered_assistant(fake_codex_root):
    """`reasoning` records populate the assistant record's thinking list
    BEFORE the assistant record is yielded — no late mutation."""
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            _user_msg("ask"),
            _assistant_msg("thinking out loud"),
            {
                "type": "response_item",
                "timestamp": "2026-04-22T10:00:02.500Z",
                "payload": {
                    "type": "reasoning",
                    "summary": ["step 1: consider X", "step 2: consider Y"],
                    "content": None,
                    "encrypted_content": None,
                },
            },
            _user_msg("next"),
        ],
    )
    records = list(parse_codex_file(jsonl))
    assistant = next(r for r in records if r.kind == "assistant")
    assert assistant.thinking == ["step 1: consider X", "step 2: consider Y"]


def test_developer_role_skipped(fake_codex_root):
    """response_item.message,role=developer is system-prompt-ish; skip."""
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            {
                "type": "response_item",
                "timestamp": "2026-04-22T10:00:00.000Z",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "system instruction"}],
                },
            },
            _user_msg("real prompt"),
        ],
    )
    records = list(parse_codex_file(jsonl))
    assert len(records) == 1
    assert records[0].kind == "user"
    assert "system instruction" not in records[0].content


def test_orphan_function_call_dropped(fake_codex_root, caplog):
    """A function_call without a preceding assistant must be dropped + logged."""
    import logging
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            _user_msg("hi"),
            # function_call before any assistant — orphan
            _function_call("exec_command", {"cmd": "ls"}, call_id="orphan-1"),
            _assistant_msg("response"),
        ],
    )
    with caplog.at_level(logging.DEBUG, logger="csc"):
        records = list(parse_codex_file(jsonl))
    assert all(not r.tool_calls for r in records)
    # Tighten substring so the assertion isn't satisfied by an
    # "orphan function_call_output" log line in a different test.
    assert any("orphan function_call (no preceding" in r.message for r in caplog.records)


def test_orphan_function_call_output_dropped(fake_codex_root, caplog):
    """A function_call_output without a registered call_id is dropped + logged."""
    import logging
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            _user_msg("hi"),
            _assistant_msg("response"),
            # output references unknown call_id
            _function_output("some output", call_id="never-registered"),
            _user_msg("next"),
        ],
    )
    with caplog.at_level(logging.DEBUG, logger="csc"):
        records = list(parse_codex_file(jsonl))
    # No tool_results should land on the second user record
    last_user = [r for r in records if r.kind == "user"][-1]
    assert last_user.tool_results == []
    assert any("orphan function_call_output" in r.message for r in caplog.records)


def test_eof_with_unattached_results_logs_only(fake_codex_root, caplog):
    """Pending tool_results at EOF (no trailing user) are dropped + logged."""
    import logging
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            _user_msg("hi"),
            _assistant_msg("doing it"),
            _function_call("exec_command", {"cmd": "ls"}),
            _function_output("file1\nfile2"),
            # no trailing user message — session ended after tool ran
        ],
    )
    with caplog.at_level(logging.DEBUG, logger="csc"):
        records = list(parse_codex_file(jsonl))
    # Assistant still yielded, tool_calls attached
    assert any(r.kind == "assistant" and r.tool_calls for r in records)
    # No leftover user record fabricated
    assert sum(r.kind == "user" for r in records) == 1
    assert any("unattached tool_result" in r.message for r in caplog.records)


def test_payload_missing_or_non_dict_skipped(fake_codex_root):
    """Records with missing or non-dict payload don't crash the parser."""
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            {"type": "response_item", "timestamp": "2026-04-22T10:00:00Z"},  # no payload
            {
                "type": "response_item",
                "timestamp": "2026-04-22T10:00:01Z",
                "payload": ["not", "a", "dict"],
            },
            _user_msg("real prompt"),
        ],
    )
    records = list(parse_codex_file(jsonl))
    assert len(records) == 1
    assert records[0].content == "real prompt"


def test_empty_session_meta_yields_no_meta(fake_codex_root):
    """A session_meta with no `id` field doesn't create a SessionMeta entry."""
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            {"type": "session_meta", "timestamp": "2026-04-22T10:00:00Z", "payload": {}},
            _user_msg("hi"),
        ],
    )
    meta = collect_codex_meta(jsonl)
    assert meta == {}


def test_argv_list_command_form(fake_codex_root):
    """A function_call with argv-list `cmd` joins to a string for redaction."""
    jsonl = fake_codex_root / "rollout-x.jsonl"
    write_codex(
        jsonl,
        [
            _meta(),
            _user_msg("show env"),
            _assistant_msg("checking"),
            # printenv with args is recognized by SENSITIVE_BASH (\b word
            # boundary). The argv-list form must still be redacted.
            _function_call("exec_command", {"cmd": ["printenv", "PATH"]}),
            _function_output("/usr/bin:/usr/local/bin"),
            _user_msg("ok"),
        ],
    )
    records = list(parse_codex_file(jsonl))
    asst = next(r for r in records if r.kind == "assistant")
    assert asst.tool_calls[0]["input"]["command"] == "printenv PATH"
    assert asst.tool_calls[0]["dropped"] is True
    last_user = [r for r in records if r.kind == "user"][-1]
    assert last_user.tool_results[0]["content"] == ""
