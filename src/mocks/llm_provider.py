"""A mock OpenAI-compatible chat completions provider.

Controls, passed in the request body (a real provider would ignore them):
  mock_response   text to return (default: echo the last user message)
  mock_chunk_size characters per streamed delta (default 4)
  mock_delay_ms   pause between streamed chunks (default 0)
  mock_status     return this HTTP status instead of a completion
  mock_delay_first_ms  pause before the first byte (used by the router tests)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


def _text_for(body: dict[str, Any]) -> str:
    if isinstance(body.get("mock_response"), str):
        return body["mock_response"]
    for message in reversed(body.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            return str(message.get("content", ""))
    return "Hello from the mock provider."


def _usage(body: dict[str, Any], text: str) -> dict[str, int]:
    prompt = sum(len(str(m.get("content", ""))) for m in body.get("messages") or []) // 4
    completion = max(1, len(text) // 4)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def create_mock_provider(name: str = "mock-provider") -> Starlette:
    async def chat_completions(request: Request) -> Response:
        body = await request.json()
        request.app.state.calls += 1
        if body.get("mock_delay_first_ms"):
            await asyncio.sleep(body["mock_delay_first_ms"] / 1000)
        if body.get("mock_status"):
            return JSONResponse(
                {"error": {"message": f"{name} simulated failure", "trace": "Traceback ..."}},
                status_code=int(body["mock_status"]),
            )

        text = _text_for(body)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model = body.get("model", "mock-model")

        if not body.get("stream"):
            return JSONResponse(
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "provider": name,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": text},
                        }
                    ],
                    "usage": _usage(body, text),
                }
            )

        size = max(1, int(body.get("mock_chunk_size", 4)))
        delay = float(body.get("mock_delay_ms", 0)) / 1000

        def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload)}\n\n".encode()

        async def events() -> AsyncIterator[bytes]:
            yield chunk({"role": "assistant", "content": ""})
            for start in range(0, len(text), size):
                if delay:
                    await asyncio.sleep(delay)
                yield chunk({"content": text[start : start + size]})
            yield chunk({}, finish="stop")
            yield b"data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/v1/chat/completions", chat_completions, methods=["POST"])])
    app.state.calls = 0
    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        create_mock_provider(os.environ.get("MOCK_PROVIDER_NAME", "mock-provider")),
        host=os.environ.get("MOCK_HOST", "127.0.0.1"),
        port=int(os.environ.get("MOCK_PORT", "9100")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
