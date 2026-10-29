"""Sensitive-data detectors: email addresses, US SSNs and payment card numbers.

All detectors live in one alternation so that whole-text and streaming
redaction walk the text with exactly the same leftmost-match rules. Every
pattern is length-bounded, which caps how much text the streaming redactor
ever has to hold back.

Uses the third-party `regex` module for its partial matching: it can tell
whether the end of a buffer *might* become a match once more text arrives.
"""

from __future__ import annotations

from collections import Counter

import regex

REDACTED = "[REDACTED]"

EMAIL = (
    r"(?P<EMAIL>[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,4}[A-Za-z]{2,24}"
    r"(?![A-Za-z0-9-]))"
)
# Area 000, 666 and 900-999, group 00 and serial 0000 are never issued.
SSN = (
    r"(?P<SSN>(?<![0-9])(?!000|666|9[0-9]{2})[0-9]{3}(?P<sep>[- ])(?!00)[0-9]{2}"
    r"(?P=sep)(?!0000)[0-9]{4}(?![0-9]))"
)
# 13-19 digits, optionally separated by single spaces or dashes. Luhn is checked afterwards.
CARD = r"(?P<CARD>(?<![0-9])[0-9](?:[ -]?[0-9]){12,18}(?![0-9]))"

PII_PATTERN = regex.compile(f"{EMAIL}|{SSN}|{CARD}")
# Same alternation anchored to the end of the buffer: used to ask "could the text
# starting here still grow into a longer match?" (see StreamRedactor).
PII_TO_END = regex.compile(f"(?:{EMAIL}|{SSN}|{CARD})\\Z")

# Longest text any single pattern can match; the redactor never holds more than this.
MAX_MATCH_LENGTH = 64 + 1 + 4 * 64 + 24
# Longest match that can contain whitespace: a 19-digit card with a separator between every digit.
MAX_SPACED_MATCH_LENGTH = 19 + 18


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = ord(ch) - 48
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def replacement(match: regex.Match[str]) -> tuple[str, str | None]:
    """Return (text to emit, detector name or None if the match is not sensitive)."""
    kind = match.lastgroup if match.lastgroup != "sep" else "SSN"
    if kind == "CARD":
        digits = "".join(ch for ch in match.group() if ch.isdigit())
        if not luhn_valid(digits):
            return match.group(), None  # card-shaped but not a card number
    return REDACTED, kind


def redact_text(text: str) -> tuple[str, Counter[str]]:
    """Redact a complete piece of text."""
    from .redactor import StreamRedactor

    redactor = StreamRedactor()
    return redactor.feed(text) + redactor.flush(), redactor.counts
