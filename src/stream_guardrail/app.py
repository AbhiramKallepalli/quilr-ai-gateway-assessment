"""LLM gateway endpoint with response-side PII redaction.

POST /v1/chat/completions is OpenAI-compatible, so a client only changes its
base_url. The request is forwarded to the configured provider. Streamed
responses are redacted chunk by chunk while they flow; non-streamed responses
are redacted as a whole.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .pipeline import guard_sse_stream, redact_completion
from .sse import SSEEvent

logger = logging.getLogger("stream_guardrail")


@dataclass(frozen=True)
class Settings:
    upstream_base_url: str = "http://127.0.0.1:9100/v1"
    upstream_api_key: str | None = None
    upstream_timeout_s: float = 60.0

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            upstream_base_url=os.environ.get("GUARDRAIL_UPSTREAM_URL", cls.upstream_base_url),
            upstream_api_key=os.environ.get("GUARDRAIL_UPSTREAM_API_KEY") or None,
            upstream_timeout_s=float(os.environ.get("GUARDRAIL_UPSTREAM_TIMEOUT_S", "60")),
        )


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"type": "gateway_error", "code": code, "message": message}},
        status_code=status,
    )


def create_app(settings: Settings, upstream: httpx.AsyncClient | None = None) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        owns_client = getattr(app.state, "client", None) is None
        if owns_client:
            app.state.client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.upstream_timeout_s, connect=5.0)
            )
        yield
        if owns_client:
            await app.state.client.aclose()

    async def chat_completions(request: Request) -> Response:
        try:
            body = json.loads(await request.body())
        except ValueError:
            return _error(400, "invalid_json", "Request body must be JSON")
        if not isinstance(body, dict):
            return _error(400, "invalid_request", "Request body must be a JSON object")

        headers = {"content-type": "application/json", "accept-encoding": "identity"}
        if settings.upstream_api_key:
            headers["authorization"] = f"Bearer {settings.upstream_api_key}"

        client: httpx.AsyncClient = request.app.state.client
        upstream_request = client.build_request(
            "POST",
            f"{settings.upstream_base_url.rstrip('/')}/chat/completions",
            content=json.dumps(body).encode(),
            headers=headers,
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("upstream unreachable: %r", exc)
            return _error(502, "upstream_unavailable", "The model provider is unavailable")

        if upstream.status_code != 200:
            detail = await upstream.aread()
            await upstream.aclose()
            logger.warning("upstream returned %s: %s", upstream.status_code, detail[:500])
            if upstream.status_code == 429:
                return _error(429, "upstream_rate_limited", "The model provider is rate limiting")
            if upstream.status_code < 500:
                return _error(upstream.status_code, "upstream_rejected", "Request was rejected")
            return _error(502, "upstream_error", "The model provider returned an error")

        if not body.get("stream"):
            try:
                payload = json.loads(await upstream.aread())
            finally:
                await upstream.aclose()
            redact_completion(payload)
            return JSONResponse(payload)

        return StreamingResponse(
            _relay(upstream),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
            Route("/healthz", healthz, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    if upstream is not None:
        app.state.client = upstream
    return app


async def _relay(upstream: httpx.Response) -> AsyncIterator[bytes]:
    # If the client disconnects, Starlette stops iterating and the finally block
    # closes the upstream response, so we stop paying for tokens nobody reads.
    try:
        async for out in guard_sse_stream(upstream.aiter_raw()):
            yield out
    except httpx.HTTPError as exc:
        logger.warning("upstream stream failed: %r", exc)
        error = {
            "error": {
                "type": "gateway_error",
                "code": "upstream_stream_interrupted",
                "message": "The model provider stream was interrupted",
            }
        }
        yield SSEEvent(json.dumps(error)).encode()
    finally:
        await upstream.aclose()
