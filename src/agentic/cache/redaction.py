"""Dependency-inverted PII redaction for cache payloads.

The cache layer must never persist raw LLM responses or retrieved document
text: Redis is a shared, long-lived store outside the guardrail boundary. This
module mirrors the patterns used by ``safety.guardrails.GuardrailManager.mask_pii``
with a local implementation so the cache package does not import the safety
subsystem (which would invert the layering and pull in policy loading).
"""

import re
from typing import Final

# Ordered most-specific first so a bearer token is not partially eaten by a
# generic numeric pattern.
_REDACTION_PATTERNS: Final[
    tuple[tuple[re.Pattern[str], str], ...]
] = (
    (re.compile(r"\bbearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE), "[REDACTED_BEARER]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "[REDACTED_SECRET]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[REDACTED_SECRET]"),
    (re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+"), "[REDACTED_EMAIL]"),
    (re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[REDACTED_PHONE]"),
    (re.compile(r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13})\b"), "[REDACTED_CARD]"),
    (re.compile(r"\b0\d{11}\b"), "[REDACTED_NATIONAL_ID]"),
)


def redact_pii(text: str) -> str:
    """Return ``text`` with bearer tokens, API keys and common PII masked.

    Idempotent: applying it twice does not change the result.
    """
    redacted = text
    for pattern, replacement in _REDACTION_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted
