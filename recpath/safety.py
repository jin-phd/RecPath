from __future__ import annotations

import re
from typing import Iterable


SECRET_PATTERNS = (
    (
        re.compile(r"(?i)\b((?:x-goog-api-key|api[-_]?key|key|access_token|refresh_token|token)=)[^&\s'\"]+"),
        r"\1REDACTED",
    ),
    (re.compile(r"(?i)\b(x-goog-api-key:\s*)\S+"), r"\1REDACTED"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "REDACTED_GOOGLE_KEY"),
    (re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"), "REDACTED_OPENAI_KEY"),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
        "REDACTED_PRIVATE_KEY",
    ),
)
_REGISTERED_SECRETS: set[str] = set()


def register_secrets(values: Iterable[str | None]) -> None:
    """Remember configured key values so they are redacted whatever their format."""
    for value in values:
        if value and len(value) >= 8:
            _REGISTERED_SECRETS.add(value)


def redact_secrets(text: object) -> str:
    """Remove API keys from any text that may be shown in the UI or saved to disk."""
    value = str(text)
    for secret in sorted(_REGISTERED_SECRETS, key=len, reverse=True):
        value = value.replace(secret, "REDACTED")
    for pattern, replacement in SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def error_summary(exc: BaseException, limit: int = 500) -> str:
    return redact_secrets(f"{type(exc).__name__}: {exc}")[:limit]
