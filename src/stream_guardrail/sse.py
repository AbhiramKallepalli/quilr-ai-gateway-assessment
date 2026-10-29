"""Incremental Server-Sent Events parsing.

Network reads don't line up with events: one read can hold half an event, or
end in the middle of a multi-byte UTF-8 character. The parser keeps only the
unfinished tail between reads.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass


@dataclass(frozen=True)
class SSEEvent:
    data: str
    event: str | None = None

    def encode(self) -> bytes:
        lines = [f"event: {self.event}"] if self.event else []
        lines += [f"data: {line}" for line in self.data.split("\n")]
        return ("\n".join(lines) + "\n\n").encode("utf-8")


class SSEParser:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer = ""
        self._data: list[str] = []
        self._event: str | None = None

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        self._buffer += self._decoder.decode(chunk)
        return self._parse_lines(final=False)

    def close(self) -> list[SSEEvent]:
        self._buffer += self._decoder.decode(b"", final=True)
        events = self._parse_lines(final=True)
        if self._buffer:
            self._take_line(self._buffer)
            self._buffer = ""
        if self._data:
            events.append(self._dispatch())
        return events

    def _parse_lines(self, *, final: bool) -> list[SSEEvent]:
        events = []
        while True:
            cut = self._next_line_end(final)
            if cut is None:
                return events
            end, width = cut
            line = self._buffer[:end]
            self._buffer = self._buffer[end + width :]
            if line == "":
                if self._data:
                    events.append(self._dispatch())
                self._event = None
            else:
                self._take_line(line)

    def _next_line_end(self, final: bool) -> tuple[int, int] | None:
        for i, ch in enumerate(self._buffer):
            if ch == "\n":
                return i, 1
            if ch == "\r":
                if i + 1 < len(self._buffer):
                    return i, 2 if self._buffer[i + 1] == "\n" else 1
                # A trailing \r may be the first half of \r\n.
                return (i, 1) if final else None
        return None

    def _take_line(self, line: str) -> None:
        if line.startswith(":"):
            return  # comment / keep-alive
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        elif field == "event":
            self._event = value

    def _dispatch(self) -> SSEEvent:
        event = SSEEvent(data="\n".join(self._data), event=self._event)
        self._data = []
        self._event = None
        return event
