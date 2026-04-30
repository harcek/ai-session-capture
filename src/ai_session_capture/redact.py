"""Secret redaction + prompt-injection neutralization.

Second line of defense after ``parser.py``'s structural drops. Regex-scrubs
anything that looks like a credential, replacing the match with
``[REDACTED:LABEL:hash6]`` where ``hash6`` is the first six hex chars of
sha256(match) — lets you tell distinct secrets apart without exposing any.

The pipeline is intentionally aggressive by default: false positives are
cheap (the hash suffix makes them visible and reviewable), real-secret
false negatives are expensive (they ship to git).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


# --- report type -----------------------------------------------------------

@dataclass
class RedactionReport:
    """Counts of redactions, keyed by pattern label. No plaintext kept."""

    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, label: str) -> None:
        self.counts[label] = self.counts.get(label, 0) + 1

    def total(self) -> int:
        return sum(self.counts.values())

    def merge(self, other: RedactionReport) -> None:
        for k, v in other.counts.items():
            self.counts[k] = self.counts.get(k, 0) + v


# --- helpers ---------------------------------------------------------------

def _hash6(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:6]


def _placeholder(label: str, match: str) -> str:
    return f"[REDACTED:{label}:{_hash6(match)}]"


# --- provider-specific patterns -------------------------------------------
# Order matters: apply the longest / most specific first.

_MULTILINE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "SSH_PRIVATE_KEY",
        re.compile(
            r"-----BEGIN (OPENSSH|RSA|EC|DSA|PGP) PRIVATE KEY-----"
            r"[\s\S]+?"
            r"-----END \1 PRIVATE KEY-----",
        ),
    ),
    (
        "PEM_PRIVATE_KEY",
        re.compile(
            r"-----BEGIN (ENCRYPTED )?PRIVATE KEY-----"
            r"[\s\S]+?"
            r"-----END (ENCRYPTED )?PRIVATE KEY-----",
        ),
    ),
]

_SINGLE_LINE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Anthropic before OpenAI: the Anthropic prefix `sk-ant-` would otherwise
    # be swallowed by the broader OpenAI `sk-` pattern.
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-(?:api03|admin01)-[A-Za-z0-9\-_]{80,}\b")),
    ("OPENAI_KEY", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b")),
    ("AWS_AKID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GITHUB_PAT_CLASSIC", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("GITHUB_PAT_FINE", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b")),
    ("GITHUB_OAUTH", re.compile(r"\bgho_[A-Za-z0-9]{36}\b")),
    ("GITHUB_APP", re.compile(r"\b(?:ghu|ghs|ghr)_[A-Za-z0-9]{36}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[abporsj]-[A-Za-z0-9-]{10,}\b")),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("STRIPE_KEY", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9a-zA-Z]{24,}\b")),
    # JWT: three dot-separated base64url segments, first segment must start
    # with `eyJ` (decoded `{"`). Stricter than length alone to keep FPs down.
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    # Database URLs with embedded credentials.
    (
        "DB_URL_WITH_CREDS",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis(?:s)?|amqp)"
            r"://[^\s:@/]+:[^\s@/]+@[^\s/]+",
        ),
    ),
]


# `.env`-style KEY=value assignments, only redacted when the key name hints
# at sensitivity. Matches `export FOO=bar`, `FOO=bar`, `FOO="bar"`, etc.
_ENV_ASSIGN = re.compile(
    r"""
    ^[ \t]*
    (?:export[ \t]+)?
    ([A-Z][A-Z0-9_]{2,})            # key (group 1)
    [ \t]*=[ \t]*
    (?:"([^"]{4,})"|'([^']{4,})'|([^\s#]{4,}))   # quoted or bare value (2/3/4)
    """,
    re.MULTILINE | re.VERBOSE,
)
_SENSITIVE_KEY_NAME = re.compile(
    r"(?i)(?:pass(?:word|wd)?|secret|token|apikey|api_key|access_key|"
    r"private_key|credential|auth|session|cookie|dsn|database_url|"
    r"conn(?:_str|_string)?)",
)


# Invisible / bidi control chars — prompt-injection neutralization.
# Keeps ordinary whitespace (\u00A0 non-breaking space excluded on purpose;
# legitimate use cases dominate there).
_INVISIBLE_CHARS = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]",
)


def neutralize(text: str) -> str:
    """Strip zero-width and bidi-override characters commonly used for
    prompt-injection tricks. Leaves normal whitespace alone."""
    return _INVISIBLE_CHARS.sub("", text)


def _make_sub(label: str, report: RedactionReport):
    def _sub(match: re.Match[str]) -> str:
        report.bump(label)
        return _placeholder(label, match.group(0))

    return _sub


def _env_sub(report: RedactionReport):
    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        value = match.group(2) or match.group(3) or match.group(4) or ""
        if not _SENSITIVE_KEY_NAME.search(key):
            return match.group(0)
        # Skip values already handled by an earlier, more specific pattern —
        # prevents double-redaction (e.g., ANTHROPIC_API_KEY=sk-ant-...
        # gets ANTHROPIC_KEY in the first pass; we don't want to bury that
        # label under a generic ENV_ASSIGN wrapper here).
        if value.startswith("[REDACTED:"):
            return match.group(0)
        report.bump("ENV_ASSIGN")
        full = match.group(0)
        return full.replace(value, _placeholder("ENV_ASSIGN", value))

    return _sub


def redact(text: str, report: RedactionReport | None = None) -> tuple[str, RedactionReport]:
    """Neutralize + redact ``text``. Returns ``(redacted_text, report)``.

    Running ``redact`` on output from a prior ``redact`` is a no-op — the
    placeholder format doesn't match any of the patterns.
    """
    if report is None:
        report = RedactionReport()
    if not text:
        return "", report

    text = neutralize(text)

    for label, pat in _MULTILINE_PATTERNS:
        text = pat.sub(_make_sub(label, report), text)

    for label, pat in _SINGLE_LINE_PATTERNS:
        text = pat.sub(_make_sub(label, report), text)

    text = _ENV_ASSIGN.sub(_env_sub(report), text)

    return text, report
