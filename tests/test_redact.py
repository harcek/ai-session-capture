"""Redaction pipeline tests — provider patterns, env assignments, FPs."""

from __future__ import annotations

import pytest

from ai_session_capture.redact import (
    RedactionReport,
    neutralize,
    redact,
)


@pytest.mark.parametrize(
    "label,sample",
    [
        # AWS AKIA/ASIA use all-A fill instead of AWS's own
        # `IOSFODNN7EXAMPLE` docs placeholder — the docs string still
        # pattern-matches strictly enough to trigger GitHub's secret
        # scanner, all-A's does not.
        ("AWS_AKID", "AKIA" + "A" * 16),
        ("AWS_AKID", "ASIA" + "A" * 16),
        ("GITHUB_PAT_CLASSIC", "ghp_" + "a" * 36),
        ("GITHUB_PAT_FINE", "github_pat_" + "a" * 82),
        ("GITHUB_OAUTH", "gho_" + "b" * 36),
        ("GITHUB_APP", "ghs_" + "c" * 36),
        ("GITHUB_APP", "ghu_" + "d" * 36),
        ("ANTHROPIC_KEY", "sk-ant-api03-" + "x" * 95),
        ("ANTHROPIC_KEY", "sk-ant-admin01-" + "y" * 95),
        ("OPENAI_KEY", "sk-" + "z" * 48),
        ("OPENAI_KEY", "sk-proj-" + "z" * 48),
        # Fixtures use repeated letters to match our permissive regex while
        # not resembling a high-entropy token — keeps GitHub push-protection
        # (and other secret scanners) from flagging the test file on commit.
        ("SLACK_TOKEN", "xoxb-" + "s" * 30),
        ("SLACK_TOKEN", "xoxp-" + "s" * 30),
        ("GOOGLE_API_KEY", "AIza" + "A" * 35),
        ("STRIPE_KEY", "sk_live_" + "a" * 24),
        ("STRIPE_KEY", "pk_test_" + "b" * 24),
    ],
)
def test_provider_pattern_redacted(label, sample):
    text = f"here is a credential: {sample} and then more text"
    redacted, report = redact(text)
    assert sample not in redacted
    assert label in redacted
    assert report.counts.get(label) == 1


def test_jwt_redacted():
    # Synthetic JWT: matches our regex (eyJ + 10+ chars + dot + 10+ +
    # dot + 10+) but doesn't decode as a real JWT. Avoids the
    # well-known jwt.io example that external scanners recognize.
    jwt = "eyJ" + "x" * 30 + "." + "y" * 30 + "." + "z" * 30
    text = f"Authorization: Bearer {jwt}"
    redacted, report = redact(text)
    assert jwt not in redacted
    assert "JWT" in redacted
    assert report.counts.get("JWT") == 1


def test_ssh_private_key_block_redacted():
    text = (
        "here is the key\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAA\n"
        "AAABAAABFwAAAAdzc2gtcnNhAAAAAwEAAQAAAQEAs...\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
        "more text"
    )
    redacted, report = redact(text)
    assert "b3BlbnNzaC" not in redacted
    assert "BEGIN OPENSSH PRIVATE KEY" not in redacted
    assert "SSH_PRIVATE_KEY" in redacted
    assert report.counts.get("SSH_PRIVATE_KEY") == 1


def test_pem_private_key_block_redacted():
    text = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvwIBADANBgkqhkiG9w0BAQEFAASCBKkwggSlAgEA\n"
        "-----END PRIVATE KEY-----\n"
    )
    redacted, report = redact(text)
    assert "MIIEvwIBAD" not in redacted
    assert "PEM_PRIVATE_KEY" in redacted


def test_db_url_with_creds_redacted():
    text = "DATABASE_URL=postgres://user:hunter2@db.internal:5432/mydb"
    redacted, report = redact(text)
    assert "hunter2" not in redacted
    # the URL itself should be redacted by DB_URL_WITH_CREDS (first wins)
    # OR by ENV_ASSIGN (second pass); either way, hunter2 must be gone.
    assert ("DB_URL_WITH_CREDS" in redacted) or ("ENV_ASSIGN" in redacted)


@pytest.mark.parametrize(
    "line",
    [
        "DB_PASSWORD=hunter2",
        "export API_KEY=sk-test-something-long-enough",
        "AWS_SECRET_ACCESS_KEY=abcdef1234567890",
        "GITHUB_TOKEN=ghp_dummyvalue_long_enough_to_pass",
        "SLACK_SESSION_COOKIE=deadbeefcafebabe",
        'DATABASE_URL="postgres://u:p@h/d"',
    ],
)
def test_env_assignment_sensitive_redacted(line):
    text = f"some log output\n{line}\nmore"
    redacted, report = redact(text)
    # The value must not survive intact. We check by asserting that the
    # specific secret-looking substrings are gone.
    for bad in ["hunter2", "sk-test-something-long-enough", "abcdef1234567890",
                "ghp_dummyvalue_long_enough_to_pass", "deadbeefcafebabe"]:
        if bad in line:
            assert bad not in redacted, f"leaked: {bad}"
    assert report.total() >= 1


@pytest.mark.parametrize(
    "line",
    [
        "LOG_LEVEL=info",
        "NODE_ENV=production",
        "PORT=3000",
        "DEBUG=true",
    ],
)
def test_env_assignment_benign_kept(line):
    text = f"{line}\n"
    redacted, report = redact(text)
    # Benign env lines MUST survive unchanged — this is the false-positive
    # guardrail for the env pattern.
    assert redacted == line + "\n"
    assert report.total() == 0


def test_git_sha_not_false_positive():
    """40-char hex git SHAs should not trigger any current pattern."""
    text = "commit abc123def456789012345678901234567890abcd"
    redacted, report = redact(text)
    assert report.total() == 0
    assert redacted == text


def test_zero_width_chars_stripped():
    # Smuggled bidi + zero-width between characters in a plausible prompt
    sneaky = "delete\u202eeverything\u200b"
    clean = neutralize(sneaky)
    assert clean == "deleteeverything"


def test_redact_is_idempotent():
    """Running redact on already-redacted text is a no-op."""
    text = "AWS key: AKIAIOSFODNN7EXAMPLE"
    r1, _ = redact(text)
    r2, report2 = redact(r1)
    assert r1 == r2
    assert report2.total() == 0


def test_report_counts_accumulate():
    """Passing an existing report accumulates counts across redact() calls."""
    shared = RedactionReport()
    redact("AKIAIOSFODNN7EXAMPLE", shared)
    redact("ghp_" + "a" * 36, shared)
    redact("AKIAXXXXXXXXXXXXXXXX", shared)
    assert shared.counts.get("AWS_AKID") == 2
    assert shared.counts.get("GITHUB_PAT_CLASSIC") == 1


def test_placeholder_format_is_stable():
    """Same secret → same hash suffix (lets you correlate repeat occurrences)."""
    text_a = "AKIAIOSFODNN7EXAMPLE and AKIAIOSFODNN7EXAMPLE"
    red_a, _ = redact(text_a)
    # Both occurrences should produce the same placeholder string
    parts = [p for p in red_a.split(" ") if p.startswith("[REDACTED:")]
    assert len(parts) == 2
    assert parts[0] == parts[1]


def test_anthropic_key_precedence_over_openai():
    """sk-ant-... must be labeled ANTHROPIC_KEY, not caught by OpenAI's pattern."""
    key = "sk-ant-api03-" + "x" * 95
    text = f"ANTHROPIC_API_KEY={key}"
    redacted, report = redact(text)
    assert "ANTHROPIC_KEY" in redacted
    assert key not in redacted
