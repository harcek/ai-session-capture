"""Parser behavior tests: structural drops, path safety, malformed input."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_session_capture.parser import (
    is_sensitive_bash,
    is_sensitive_path,
    parse_file,
)
from tests.conftest import write_jsonl


@pytest.mark.parametrize(
    "command",
    [
        "env",
        "printenv",
        "printenv FOO",
        "cat .env",
        "cat ./.env.production",
        "cat ~/.netrc",
        "cat ~/.ssh/id_rsa",
        "aws configure list",
        "aws sts get-caller-identity",
        "gh auth token",
        "kubectl get secret my-secret -o yaml",
        "doppler secrets get FOO",
        "vault kv get secret/foo",
    ],
)
def test_sensitive_bash_detected(command):
    assert is_sensitive_bash(command)


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "echo hello",
        "grep 'env' README.md",  # mentions env but isn't running it
        "python envtool.py",
    ],
)
def test_benign_bash_not_flagged(command):
    assert not is_sensitive_bash(command)


@pytest.mark.parametrize(
    "path",
    [
        "/home/user/.env",
        "/home/user/.env.production",
        "/home/user/.aws/credentials",
        "/home/user/.ssh/id_rsa",
        "/home/user/.ssh/id_ed25519",
        "/home/user/.netrc",
        "/etc/ssl/certs/server.pem",
        "/home/user/.kube/config",
        "/home/user/credentials.json",
        "/home/user/secrets.yaml",
    ],
)
def test_sensitive_path_detected(path):
    assert is_sensitive_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/home/user/src/main.py",
        "/home/user/README.md",
        "/home/user/envelope.txt",
        "/home/user/keyboard_layout.json",
    ],
)
def test_benign_path_not_flagged(path):
    assert not is_sensitive_path(path)


def test_parse_clean_conversation(fake_projects_root):
    jsonl = fake_projects_root / "myproject" / "session-1.jsonl"
    write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "sessionId": "s1",
                "uuid": "u1",
                "timestamp": "2026-04-20T10:00:00.000Z",
                "cwd": "/home/user/myproject",
                "isSidechain": False,
                "message": {"role": "user", "content": "hi claude"},
            },
            {
                "type": "assistant",
                "sessionId": "s1",
                "uuid": "u2",
                "timestamp": "2026-04-20T10:00:05.000Z",
                "cwd": "/home/user/myproject",
                "isSidechain": False,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello back"}],
                },
            },
        ],
    )
    records = list(parse_file(jsonl))
    assert len(records) == 2
    assert records[0].kind == "user"
    assert records[0].content == "hi claude"
    assert records[1].kind == "assistant"
    assert records[1].content == "hello back"
    assert records[0].project == "myproject"


def test_structural_drop_bash_env(fake_projects_root):
    """A Bash tool_use of `env` must cause the matching tool_result to be blanked."""
    jsonl = fake_projects_root / "p" / "s.jsonl"
    write_jsonl(
        jsonl,
        [
            {
                "type": "assistant",
                "sessionId": "s1",
                "uuid": "u1",
                "timestamp": "2026-04-20T10:00:00.000Z",
                "isSidechain": False,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_env_1",
                            "name": "Bash",
                            "input": {"command": "env"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "sessionId": "s1",
                "uuid": "u2",
                "timestamp": "2026-04-20T10:00:01.000Z",
                "isSidechain": False,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_env_1",
                            "content": "SECRET_KEY=abc123\nAWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
                        }
                    ],
                },
            },
        ],
    )
    records = list(parse_file(jsonl))
    assistant_rec = records[0]
    user_rec = records[1]
    assert assistant_rec.tool_calls[0]["dropped"] is True
    assert user_rec.tool_results[0]["dropped"] is True
    assert user_rec.tool_results[0]["content"] == ""
    assert "SECRET_KEY" not in str(user_rec.tool_results)
    assert "AKIAIOSFODNN7EXAMPLE" not in str(user_rec.tool_results)


def test_structural_drop_read_dotenv(fake_projects_root):
    """A Read of a .env file must cause the matching tool_result to be blanked."""
    jsonl = fake_projects_root / "p" / "s.jsonl"
    write_jsonl(
        jsonl,
        [
            {
                "type": "assistant",
                "sessionId": "s1",
                "uuid": "u1",
                "timestamp": "2026-04-20T10:00:00.000Z",
                "isSidechain": False,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_read_1",
                            "name": "Read",
                            "input": {"file_path": "/home/user/project/.env"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "sessionId": "s1",
                "uuid": "u2",
                "timestamp": "2026-04-20T10:00:01.000Z",
                "isSidechain": False,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_read_1",
                            "content": "DB_PASSWORD=hunter2\nAPI_KEY=sk-real-key",
                        }
                    ],
                },
            },
        ],
    )
    records = list(parse_file(jsonl))
    assert records[1].tool_results[0]["dropped"] is True
    assert records[1].tool_results[0]["content"] == ""
    assert "hunter2" not in str(records[1].tool_results)


def test_malformed_line_is_skipped(fake_projects_root):
    """A syntactically invalid line must not prevent other lines from parsing."""
    jsonl = fake_projects_root / "p" / "s.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("w") as f:
        f.write('{"type":"user","sessionId":"s","uuid":"u1","message":{"role":"user","content":"first"}}\n')
        f.write("not valid json\n")
        f.write('{"type":"user","sessionId":"s","uuid":"u2","message":{"role":"user","content":"third"}}\n')
    import os

    os.chmod(jsonl, 0o600)

    records = list(parse_file(jsonl))
    assert [r.content for r in records] == ["first", "third"]


def test_default_projects_root_precedence(tmp_path, monkeypatch):
    """Env var (test/dev) > CLAUDE_CONFIG_DIR/projects > ~/.claude/projects.

    See ADR-0004 — the TOML config knob was removed; the root is derived
    from Claude Code's own canonicals plus a test-only env override.
    """
    from claude_session_capture.parser import default_projects_root

    env_root = tmp_path / "from_env"
    env_root.mkdir()
    claude_cfg_dir = tmp_path / "alt_claude"
    (claude_cfg_dir / "projects").mkdir(parents=True)

    # 1. CLAUDE_PROJECTS_ROOT wins (test/dev hook)
    monkeypatch.setenv("CLAUDE_PROJECTS_ROOT", str(env_root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_cfg_dir))
    assert default_projects_root() == env_root.resolve()

    # 2. Without CLAUDE_PROJECTS_ROOT, CLAUDE_CONFIG_DIR/projects wins
    monkeypatch.delenv("CLAUDE_PROJECTS_ROOT")
    assert default_projects_root() == (claude_cfg_dir / "projects").resolve()

    # 3. Neither env var → default
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    expected = Path("~/.claude/projects").expanduser().resolve()
    assert default_projects_root() == expected


def test_path_outside_root_rejected(tmp_path, monkeypatch):
    """Opening a JSONL outside the configured root must raise."""
    monkeypatch.setenv("CLAUDE_PROJECTS_ROOT", str(tmp_path / "projects"))
    (tmp_path / "projects").mkdir()
    stray = tmp_path / "outside.jsonl"
    stray.write_text("{}\n")

    import os
    os.chmod(stray, 0o600)

    with pytest.raises(ValueError, match="refusing"):
        list(parse_file(stray))


def test_collect_session_meta_custom_title(fake_projects_root):
    jsonl = fake_projects_root / "p" / "s.jsonl"
    write_jsonl(
        jsonl,
        [
            {"type": "custom-title", "sessionId": "s1", "customTitle": "old-name"},
            {
                "type": "user",
                "sessionId": "s1",
                "uuid": "u1",
                "timestamp": "2026-04-20T10:00:00.000Z",
                "isSidechain": False,
                "message": {"role": "user", "content": "hi there"},
            },
            {"type": "custom-title", "sessionId": "s1", "customTitle": "new-name"},
            {"type": "agent-name", "sessionId": "s1", "agentName": "agent-x"},
        ],
    )
    from claude_session_capture.parser import collect_session_meta

    meta = collect_session_meta(jsonl)
    assert "s1" in meta
    assert meta["s1"].custom_title == "new-name"
    assert meta["s1"].agent_name == "agent-x"
    assert meta["s1"].first_prompt == "hi there"


def test_collect_session_meta_skips_slash_command_wrappers(fake_projects_root):
    """First-prompt detection must skip <local-command-*> mechanical content."""
    jsonl = fake_projects_root / "p" / "s.jsonl"
    write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "sessionId": "s1",
                "uuid": "u1",
                "message": {
                    "role": "user",
                    "content": "<local-command-stdout>Session resumed</local-command-stdout>",
                },
            },
            {
                "type": "user",
                "sessionId": "s1",
                "uuid": "u2",
                "message": {
                    "role": "user",
                    "content": "<command-name>/resume</command-name>",
                },
            },
            {
                "type": "user",
                "sessionId": "s1",
                "uuid": "u3",
                "message": {"role": "user", "content": "actual first prompt here"},
            },
        ],
    )
    from claude_session_capture.parser import collect_session_meta

    meta = collect_session_meta(jsonl)
    assert meta["s1"].first_prompt == "actual first prompt here"


def test_collect_session_meta_empty_session(fake_projects_root):
    """A session with no user prompts yields SessionMeta with all None."""
    jsonl = fake_projects_root / "p" / "s.jsonl"
    write_jsonl(
        jsonl,
        [
            {
                "type": "assistant",
                "sessionId": "s1",
                "uuid": "u1",
                "message": {"role": "assistant", "content": []},
            }
        ],
    )
    from claude_session_capture.parser import collect_session_meta

    meta = collect_session_meta(jsonl)
    assert meta["s1"].custom_title is None
    assert meta["s1"].first_prompt is None


def test_collect_session_meta_handles_list_content(fake_projects_root):
    jsonl = fake_projects_root / "p" / "s.jsonl"
    write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "sessionId": "s1",
                "uuid": "u1",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "this is the first prompt"},
                    ],
                },
            },
        ],
    )
    from claude_session_capture.parser import collect_session_meta

    meta = collect_session_meta(jsonl)
    assert meta["s1"].first_prompt == "this is the first prompt"


def test_sidechain_flag_preserved(fake_projects_root):
    """Sub-agent / sidechain messages are yielded but flagged for downstream filtering."""
    jsonl = fake_projects_root / "p" / "s.jsonl"
    write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "sessionId": "s1",
                "uuid": "u1",
                "timestamp": "2026-04-20T10:00:00.000Z",
                "isSidechain": True,
                "message": {"role": "user", "content": "sub-agent prompt"},
            },
        ],
    )
    records = list(parse_file(jsonl))
    assert len(records) == 1
    assert records[0].is_sidechain is True
