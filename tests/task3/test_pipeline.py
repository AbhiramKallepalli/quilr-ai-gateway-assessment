import asyncio
import json
from collections.abc import AsyncIterator

from stream_guardrail.pipeline import guard_sse_stream, redact_completion
from stream_guardrail.sse import SSEParser


def chunk(content=None, index=0, finish=None, role=None) -> bytes:
    delta = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": index, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


async def from_list(items: list[bytes]) -> AsyncIterator[bytes]:
    for item in items:
        yield item


async def run(items: list[bytes]) -> list[dict | str]:
    out = b"".join([b async for b in guard_sse_stream(from_list(items))])
    parser = SSEParser()
    events = parser.feed(out) + parser.close()
    return [e.data if e.data == "[DONE]" else json.loads(e.data) for e in events]


def text_of(events, index=0) -> str:
    return "".join(
        c["delta"].get("content") or ""
        for e in events
        if isinstance(e, dict)
        for c in e["choices"]
        if c["index"] == index
    )


async def test_pii_split_across_chunks_is_redacted():
    events = await run(
        [
            chunk(role="assistant", content=""),
            chunk("Card: 4111 11"),
            chunk("11 1111 1"),
            chunk("111, email bo"),
            chunk("b@acme.io."),
            chunk(finish="stop"),
            b"data: [DONE]\n\n",
        ]
    )
    assert text_of(events) == "Card: [REDACTED], email [REDACTED]."
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"
    assert events[-1] == "[DONE]"
    assert events[-2]["choices"][0]["finish_reason"] == "stop"
    assert "4111" not in json.dumps(events)


async def test_held_text_is_released_by_finish_reason():
    events = await run([chunk("call me 555"), chunk("-0100", finish="stop"), b"data: [DONE]\n\n"])
    assert text_of(events) == "call me 555-0100"


async def test_held_text_is_released_if_stream_ends_without_done():
    events = await run([chunk("ends with 123-45-67"), chunk("89")])
    assert text_of(events) == "ends with [REDACTED]"


async def test_choices_are_redacted_independently():
    events = await run(
        [
            chunk("a@b", index=0),
            chunk("x@y", index=1),
            chunk(".io", index=0),
            chunk(" hi", index=1),
            b"data: [DONE]\n\n",
        ]
    )
    assert text_of(events, 0) == "[REDACTED]"
    assert text_of(events, 1) == "x@y hi"


async def test_non_json_events_are_dropped():
    events = await run([b"data: not json 123-45-6789\n\n", chunk("ok"), b"data: [DONE]\n\n"])
    assert text_of(events) == "ok"
    assert "123-45-6789" not in json.dumps(events)


async def test_output_is_streamed_not_buffered():
    """Each upstream chunk produces output before the next one is sent."""
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def upstream() -> AsyncIterator[bytes]:
        while (item := await queue.get()) is not None:
            yield item

    stream = guard_sse_stream(upstream())
    await queue.put(chunk("First sentence. "))
    first = await asyncio.wait_for(anext(stream), timeout=1)
    assert b"First sentence. " in first

    await queue.put(chunk("Second one. "))
    second = await asyncio.wait_for(anext(stream), timeout=1)
    assert b"Second one. " in second

    await queue.put(None)
    assert [b async for b in stream] == []


def test_non_streaming_completion_is_redacted():
    payload = {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "SSN 123-45-6789"}}]
    }
    counts = redact_completion(payload)
    assert payload["choices"][0]["message"]["content"] == "SSN [REDACTED]"
    assert counts["SSN"] == 1
