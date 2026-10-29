"""Redaction for OpenAI-compatible chat completion responses, streamed or not.

Streaming path, per upstream read:
  bytes -> SSEParser -> chunk JSON -> StreamRedactor per choice -> re-encoded SSE

Nothing waits for the full response. Each chunk is forwarded as soon as its
safe text is known, and a chunk whose content is entirely held back is skipped
rather than sent empty. Held text is released by the next chunk, by the
choice's finish_reason, or by [DONE].
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

from .patterns import redact_text
from .redactor import StreamRedactor
from .sse import SSEEvent, SSEParser

logger = logging.getLogger("stream_guardrail")

_CHUNK_META_FIELDS = ("id", "object", "created", "model", "system_fingerprint")


class StreamGuard:
    """Rewrites one chat.completion.chunk stream. One instance per response."""

    def __init__(self) -> None:
        self._redactors: dict[int, StreamRedactor] = {}
        self._meta: dict[str, Any] = {"object": "chat.completion.chunk"}
        self.done = False

    @property
    def counts(self) -> Counter[str]:
        total: Counter[str] = Counter()
        for redactor in self._redactors.values():
            total.update(redactor.counts)
        return total

    def process(self, event: SSEEvent) -> bytes:
        if event.data.strip() == "[DONE]":
            self.done = True
            return self.flush() + event.encode()
        try:
            chunk = json.loads(event.data)
        except ValueError:
            # Unscannable content is dropped, not forwarded.
            logger.warning("dropping non-JSON SSE event from upstream")
            return b""
        if not isinstance(chunk, dict) or not isinstance(chunk.get("choices"), list):
            return event.encode()  # e.g. a usage-only chunk

        self._meta.update({k: chunk[k] for k in _CHUNK_META_FIELDS if k in chunk})
        keep = bool(chunk.get("usage")) or not chunk["choices"]
        for choice in chunk["choices"]:
            if isinstance(choice, dict) and self._process_choice(choice):
                keep = True
        if not keep:
            return b""
        return SSEEvent(json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))).encode()

    def _process_choice(self, choice: dict[str, Any]) -> bool:
        """Redact one choice in place. Returns False if nothing is left worth sending."""
        index = choice.get("index", 0)
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            delta = {}
        finished = choice.get("finish_reason") is not None
        content = delta.get("content")

        if isinstance(content, str):
            redactor = self._redactors.setdefault(index, StreamRedactor())
            safe = redactor.feed(content)
            if finished:
                safe += redactor.flush()
            delta["content"] = safe
        elif finished and index in self._redactors:
            held = self._redactors[index].flush()
            if held:
                delta["content"] = held
                choice["delta"] = delta

        return finished or any(v not in ("", None) for v in delta.values())

    def flush(self) -> bytes:
        """Release held text for every choice, as one extra chunk."""
        choices = []
        for index, redactor in sorted(self._redactors.items()):
            held = redactor.flush()
            if held:
                choices.append({"index": index, "delta": {"content": held}, "finish_reason": None})
        if not choices:
            return b""
        chunk = {**self._meta, "choices": choices}
        return SSEEvent(json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))).encode()


async def guard_sse_stream(upstream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    parser = SSEParser()
    guard = StreamGuard()
    async for raw in upstream:
        for event in parser.feed(raw):
            out = guard.process(event)
            if out:
                yield out
    for event in parser.close():
        out = guard.process(event)
        if out:
            yield out
    if not guard.done:
        tail = guard.flush()
        if tail:
            yield tail
    if guard.counts:
        logger.info("redacted stream: %s", dict(guard.counts))


def redact_completion(payload: Any) -> Counter[str]:
    """Redact a non-streaming chat.completion response in place."""
    counts: Counter[str] = Counter()
    choices = payload.get("choices") if isinstance(payload, dict) else None
    for choice in choices or []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            message["content"], found = redact_text(message["content"])
            counts.update(found)
    return counts
