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
    (
        re.compile(
            r"(?<![\dA-Za-z])(?:\+?\d{1,3}[-.\s]?)?"
            r"(?:\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}|\(?0\d{2,3}\)?[-.\s]?\d{3}[-.\s]?\d{3,4})(?![\dA-Za-z])"
        ),
        "[REDACTED_PHONE]",
    ),
    (
        re.compile(
            r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12}|(?:[0-9]{4}[-\s]?){3}[0-9]{4})\b"
        ),
        "[REDACTED_CARD]",
    ),
    (
        re.compile(
            r"\b0\d{11}\b"
            r"|(?i:(?:cmnd|cccd|căn\s*cước|chứng\s*minh|national[\s_]?id)[^\d\n]{0,24})\b\d{9}\b"
        ),
        "[REDACTED_NATIONAL_ID]",
    ),
)

_CITATION_BLOCK_RE = re.compile(r"(\[CITATIONS\]\s*.*?\s*\[/CITATIONS\])", re.DOTALL)


def _redact_plain_text(text: str) -> str:
    redacted = text
    for pattern, replacement in _REDACTION_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_pii(text: str) -> str:
    """Return ``text`` with bearer tokens, API keys and common PII masked.

    Preserves ``[CITATIONS]...[/CITATIONS]`` structured JSON blocks intact so
    cached RAG responses never lose citation metadata or fail JSON parsing.
    Idempotent: applying it twice does not change the result.
    """
    if "[CITATIONS]" not in text or "[/CITATIONS]" not in text:
        return _redact_plain_text(text)
    parts = _CITATION_BLOCK_RE.split(text)
    out: list[str] = []
    for part in parts:
        if part.startswith("[CITATIONS]") and part.endswith("[/CITATIONS]"):
            out.append(part)
        else:
            out.append(_redact_plain_text(part))
    return "".join(out)
