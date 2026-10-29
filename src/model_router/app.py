"""LLM gateway endpoint with per-tenant token limits and model failover.

POST /v1/chat/completions (OpenAI-compatible), authenticated with a tenant
API key:

  1. authenticate the tenant                -> 401
  2. validate the body, estimate its tokens  -> 400
  3. reserve tokens in the sliding window    -> 429 with Retry-After
  4. route: primary, then secondary on 429 / timeout / 5xx
  5. replace the reservation with real usage (or release it on failure)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .errors import gateway_error, upstream_error
from .limiter import (
    RateLimited,
    RequestTooLarge,
    Reservation,
    SlidingWindowLimiter,
    retry_after_header,
)
from .router import AllProvidersFailed, ModelRouter, Provider, RoutedResponse
from .tokens import estimate_tokens, reported_usage

logger = logging.getLogger("model_router")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Settings:
    tenant_keys: dict[str, str] = field(repr=False)  # sha256(api key) -> tenant id
    providers: tuple[Provider, ...]
    tokens_per_minute: int = 50_000
    attempt_timeout_s: float = 3.0
    db_path: str = "data/router.db"

    @staticmethod
    def hash_keys(keys: dict[str, str]) -> dict[str, str]:
        return {_hash(key): tenant for key, tenant in keys.items()}

    @classmethod
    def from_env(cls) -> Settings:
        keys = json.loads(os.environ["ROUTER_TENANT_KEYS"])
        providers = tuple(
            Provider(
                label=label,
                base_url=os.environ[f"ROUTER_{label.upper()}_URL"],
                api_key=os.environ.get(f"ROUTER_{label.upper()}_API_KEY") or None,
                model=os.environ.get(f"ROUTER_{label.upper()}_MODEL") or None,
            )
            for label in ("primary", "secondary")
        )
        return cls(
            tenant_keys=cls.hash_keys(keys),
            providers=providers,
            tokens_per_minute=int(os.environ.get("ROUTER_TOKENS_PER_MINUTE", "50000")),
            attempt_timeout_s=float(os.environ.get("ROUTER_ATTEMPT_TIMEOUT_MS", "3000")) / 1000,
            db_path=os.environ.get("ROUTER_DB_PATH", "data/router.db"),
        )


def _validate(body: Any) -> str | None:
    if not isinstance(body, dict):
        return "Request body must be a JSON object"
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages must be a non-empty array"
    if not all(isinstance(m, dict) and isinstance(m.get("role"), str) for m in messages):
        return "every message needs a role"
    max_tokens = body.get("max_tokens")
    if max_tokens is not None and (
        not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0
    ):
        return "max_tokens must be a positive integer"
    return None


def create_app(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    limiter: SlidingWindowLimiter | None = None,
) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        owns_client = getattr(app.state, "client", None) is None
        if owns_client:
            app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))
            app.state.router = ModelRouter(
                list(settings.providers), app.state.client, settings.attempt_timeout_s
            )
        if getattr(app.state, "limiter", None) is None:
            app.state.limiter = SlidingWindowLimiter(settings.db_path, settings.tokens_per_minute)
        yield
        await app.state.limiter.close()
        if owns_client:
            await app.state.client.aclose()

    async def chat_completions(request: Request) -> Response:
        request_id = uuid.uuid4().hex
        try:
            return await _handle(request, request_id)
        except Exception:
            # Last line of defence: nothing internal leaks, even on a bug.
            logger.exception("unhandled error request_id=%s", request_id)
            return gateway_error(500, "internal_error", "Internal gateway error", request_id)

    async def _handle(request: Request, request_id: str) -> Response:
        scheme, _, key = (request.headers.get("authorization") or "").partition(" ")
        tenant = (
            settings.tenant_keys.get(_hash(key.strip())) if scheme.lower() == "bearer" else None
        )
        if tenant is None:
            return gateway_error(401, "invalid_api_key", "Missing or invalid API key", request_id)

        try:
            body = json.loads(await request.body())
        except ValueError:
            return gateway_error(400, "invalid_json", "Request body must be JSON", request_id)
        problem = _validate(body)
        if problem:
            return gateway_error(400, "invalid_request", problem, request_id)

        limiter: SlidingWindowLimiter = request.app.state.limiter
        estimate = estimate_tokens(body)
        try:
            reservation = await limiter.reserve(tenant, estimate)
        except RequestTooLarge:
            return gateway_error(
                400, "request_too_large", "Request exceeds the per-minute token limit", request_id
            )
        except RateLimited as exc:
            return gateway_error(
                429,
                "tenant_rate_limited",
                "Token rate limit exceeded",
                request_id,
                {
                    "retry-after": retry_after_header(exc.retry_after_s),
                    "x-ratelimit-limit-tokens": str(exc.limit),
                    "x-ratelimit-remaining-tokens": str(max(0, exc.limit - exc.used)),
                },
            )

        router: ModelRouter = request.app.state.router
        try:
            routed = await router.send(body, request_id)
        except AllProvidersFailed as exc:
            await limiter.release(reservation)
            return upstream_error(exc, request_id)
        except BaseException:
            await limiter.release(reservation)
            raise

        headers = {
            "x-request-id": request_id,
            "x-gateway-provider": routed.provider,
            "x-gateway-attempts": str(routed.attempts),
            "x-ratelimit-limit-tokens": str(limiter.limit),
            "x-ratelimit-remaining-tokens": str(reservation.remaining),
        }
        if routed.stream is not None:
            return StreamingResponse(
                _relay(routed, limiter, reservation),
                media_type="text/event-stream",
                headers={**headers, "cache-control": "no-cache"},
            )
        if routed.payload is None:  # pragma: no cover - invariant
            raise RuntimeError("routed.payload missing")
        actual = reported_usage(routed.payload)
        await limiter.commit(reservation, actual if actual is not None else estimate)
        return JSONResponse(routed.payload, headers=headers)

    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
            Route("/healthz", healthz, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    if client is not None:
        app.state.client = client
        app.state.router = ModelRouter(list(settings.providers), client, settings.attempt_timeout_s)
    if limiter is not None:
        app.state.limiter = limiter
    return app


async def _relay(
    routed: RoutedResponse, limiter: SlidingWindowLimiter, reservation: Reservation
) -> AsyncIterator[bytes]:
    # Streams usually carry no usage figure, so the reservation is kept as the charge.
    if routed.stream is None:  # pragma: no cover - invariant
        raise RuntimeError("routed.stream missing")
    try:
        async for chunk in routed.stream:
            yield chunk
    finally:
        if routed.close is not None:
            await routed.close()
        await limiter.commit(reservation, reservation.tokens)
