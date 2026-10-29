"""Incremental redaction over a text stream.

The usual approach holds back a fixed window (say the last 64 characters) so
a match split across chunks is never emitted half-way. That costs latency on
every chunk, including plain prose.

This redactor holds back only what could still be sensitive:

  * a complete match is redacted and emitted as soon as it is known to be final;
  * a possible match running into the end of the buffer ("john@acme.c",
    "4111 1111") is held until the next chunk decides it;
  * everything before that is emitted immediately.

For ordinary text that means only the trailing word is held. The held text is
bounded by MAX_MATCH_LENGTH, so memory per stream is constant no matter how
long the response is.

Invariant (property-tested): for any text and any way of splitting it into
chunks, joining the emitted pieces equals redact_text(text).
"""

from __future__ import annotations

from collections import Counter

from .patterns import (
    MAX_MATCH_LENGTH,
    MAX_SPACED_MATCH_LENGTH,
    PII_PATTERN,
    PII_TO_END,
    replacement,
)

# Characters of already-emitted text kept so look-behind assertions still see
# what came before the buffer.
_CONTEXT_CHARS = 1

# Every detector starts with one of these; other positions can be skipped quickly.
_MATCH_START_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+-"
)


class StreamRedactor:
    def __init__(self) -> None:
        self._pending = ""
        self._context = ""
        self.counts: Counter[str] = Counter()

    @property
    def pending(self) -> int:
        """Characters received but not yet emitted."""
        return len(self._pending)

    def feed(self, text: str) -> str:
        """Add text from the stream; return the part that is now safe to emit."""
        self._pending += text
        return self._drain(final=False)

    def flush(self) -> str:
        """End of stream: everything left is final."""
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> str:
        buf = self._context + self._pending
        pos = len(self._context)
        hold = len(buf) if final else _hold_point(buf, pos)
        out: list[str] = []

        while pos < hold:
            match = PII_PATTERN.search(buf, pos)
            if match is None or match.start() >= hold:
                break
            text, kind = replacement(match)
            out.append(buf[pos : match.start()])
            out.append(text)
            if kind:
                self.counts[kind] += 1
            pos = match.end()
            if not final and pos > hold:
                hold = _hold_point(buf, pos)

        safe_end = max(pos, hold)
        out.append(buf[pos:safe_end])
        self._context = buf[max(0, safe_end - _CONTEXT_CHARS) : safe_end]
        self._pending = buf[safe_end:]
        return "".join(out)


def _hold_point(buf: str, start: int) -> int:
    """First position from which the rest of the buffer could still become, or
    grow into, a match once more text arrives. Text before it is final."""
    # Only SSNs and card numbers can contain a space, and they are short, so a
    # possible match that reaches the end can't start much before the last space.
    last_space = max(buf.rfind(" "), buf.rfind("\n"), buf.rfind("\t"), buf.rfind("\r"))
    lower = min(last_space + 1, len(buf) - MAX_SPACED_MATCH_LENGTH)
    for p in range(max(start, lower, len(buf) - MAX_MATCH_LENGTH), len(buf)):
        if buf[p] in _MATCH_START_CHARS and PII_TO_END.match(buf, p, partial=True):
            return p
    return len(buf)
