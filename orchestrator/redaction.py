"""One redaction routine for everything that may be printed, stored, or shown (phase 3).

``llm.scrub`` and ``runner.redact`` delegate here. Values of every ``kind: secret`` name in
the credential registry are replaced, then credential-shaped tokens, then ``key=value``
assignments whose key looks like a secret. ``FILE_SCAN_PATTERNS`` is the narrower set the
local secret scan applies to repository files (it never prints a match).
"""

from __future__ import annotations

import os
import re

from . import credentials

MARK = "[redacted]"
_MIN_VALUE_CHARS = 4

TOKEN_PATTERNS = [
    # An OpenAI key: "sk-" at a token boundary followed by a key's length. Live run 20: the
    # unbounded form turned "task-execution" into "ta[redacted]" inside the stored stack, and
    # CloudFormation rejected the role name.
    r"(?<![A-Za-z0-9_\-])sk-[A-Za-z0-9_\-]{20,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bASIA[0-9A-Z]{16}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    r"\bAIza[0-9A-Za-z_\-]{35}\b",
    r"\bya29\.[A-Za-z0-9_\-]{20,}",
    r"\bxox[baprs]-[A-Za-z0-9\-]{10,}",
    r"hawk\.[A-Za-z0-9]{8,}\.[A-Za-z0-9_\-]{8,}",
    r"snyk_uat\.[A-Za-z0-9._\-]+",
    r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "api" + r"key_[A-Za-z0-9_]+",
]
# A shell default expansion such as "${HAWK_API_KEY:-}" or ': "${SNYK_TOKEN:=}"' is a reference
# by name, not an assigned value (live run 20's sweep: the stored dast.sh had lost its "-}" to the
# mark, which would have been a bad substitution at run time).
ASSIGNMENT_PATTERN = r"(?i)((?:api[_-]?key|token|secret|password|passwd)\s*[=:]\s*)(?!credentials\(|\$|\{\{|secrets\.|[-=?+}])[^\s\"',;]+"

FILE_SCAN_PATTERNS = [
    r"(?<![A-Za-z0-9_\-])sk-[A-Za-z0-9]{20,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bASIA[0-9A-Z]{16}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    r"\bAIza[0-9A-Za-z_\-]{35}\b",
    r"\bxox[baprs]-[A-Za-z0-9\-]{10,}",
    r"hawk\.[A-Za-z0-9]{8,}\.[A-Za-z0-9_\-]{8,}",
    r"snyk_uat\.[A-Za-z0-9._\-]+",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]

_TOKENS = [re.compile(pattern) for pattern in TOKEN_PATTERNS]
_ASSIGNMENT = re.compile(ASSIGNMENT_PATTERN)
_FILE_SCAN = [re.compile(pattern) for pattern in FILE_SCAN_PATTERNS]
# Values the vault exported (phase 8). Registered verbatim so a config value such as a
# Jenkins URL with an embedded token is scrubbed too, whatever its registry kind.
_REGISTERED: set[str] = set()


def secret_env_names() -> list[str]:
    return credentials.secret_names()


def register_values(values) -> int:
    """Remember values that must never appear in output. Returns how many are registered."""
    for value in values or []:
        if isinstance(value, str) and len(value.strip()) >= _MIN_VALUE_CHARS:
            _REGISTERED.add(value.strip())
    return len(_REGISTERED)


def forget_values() -> None:
    _REGISTERED.clear()


def redact(text: str, limit: int | None = None) -> str:
    """Remove credential material; optionally truncate to ``limit`` characters."""
    cleaned = text or ""
    for value in sorted(_REGISTERED, key=len, reverse=True):
        cleaned = cleaned.replace(value, MARK)
    for name in secret_env_names():
        value = os.getenv(name, "")
        if value and len(value) >= _MIN_VALUE_CHARS:
            cleaned = cleaned.replace(value, MARK)
    for pattern in _TOKENS:
        cleaned = pattern.sub(MARK, cleaned)
    cleaned = _ASSIGNMENT.sub(lambda match: match.group(1) + MARK, cleaned)
    if limit is not None and len(cleaned) > limit:
        return cleaned[:limit] + "\n…[truncated]"
    return cleaned


def file_has_secret(text: str) -> bool:
    """True when repository text contains a credential-shaped token (no match is returned)."""
    return any(pattern.search(text) for pattern in _FILE_SCAN)
