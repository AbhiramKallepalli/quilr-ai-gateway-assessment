"""Primary/secondary model routing with failover.

Each attempt runs under asyncio.timeout(). For a normal completion the clock
covers the whole response; for a stream it covers the time to the first
chunk, after which the stream is committed to that provider (a half-sent
answer can't be retried elsewhere).

The timeout and the response race safely: asyncio.timeout either lets the
block finish or cancels it and raises TimeoutError, never both. Cancelling
the block also cancels the in-flight HTTP request, so a slow primary isn't
left running in the background once the secondary has been asked.

Fail over on: 429, timeouts, connection errors and 5xx. Other 4xx responses
mean the request itself is wrong and would fail on any provider, so they are
not retried.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("model_router")

FAILOVER_KINDS = frozenset({"rate_limited", "timeout", "connection", "server_error"})


@dataclass(frozen=True)
class Provider:
    label: str  # shown to clients in x-gateway-provider; never the URL
    base_url: str
    api_key: str | None = None
    model: str | None = None  # overrides the requested model, e.g. a cheaper backup


class UpstreamFailure(Exception):
    def __init__(
        self,
        kind: str,
        provider: str,
        status: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(f"{provider}: {kind}" + (f" ({status})" if status else ""))
        self.kind = kind
        self.provider = provider
        self.status = status
        self.retry_after_s = retry_after_s


class AllProvidersFailed(Exception):
    def __init__(self, last: UpstreamFailure, attempts: int) -> None:
        super().__init__(str(last))
        self.last = last
        self.attempts = attempts


@dataclass
class RoutedResponse:
    provider: str
    attempts: int
    payload: dict[str, Any] | None = None
    stream: AsyncIterator[bytes] | None = None
    close: Callable[[], Awaitable[None]] | None = None


@dataclass
class ModelRouter:
    providers: list[Provider]
    client: httpx.AsyncClient
    attempt_timeout_s: float = 3.0
    _cooldown_until: dict[str, float] = field(default_factory=dict)

    async def send(self, body: dict[str, Any], request_id: str) -> RoutedResponse:
        attempts = 0
        last: UpstreamFailure | None = None
        for provider in self._candidates():
            attempts += 1
            try:
                routed = await self._attempt(provider, body, request_id)
            except UpstreamFailure as failure:
                logger.warning("attempt failed request_id=%s %s", request_id, failure)
                last = failure
                if failure.kind == "rate_limited":
                    self._cool_down(provider, failure.retry_after_s)
                if failure.kind not in FAILOVER_KINDS:
                    break
                continue
            routed.attempts = attempts
            return routed
        if last is None:  # pragma: no cover - invariant
            raise RuntimeError("last missing")
        raise AllProvidersFailed(last, attempts)

    def _candidates(self) -> list[Provider]:
        # Providers cooling down after a 429 go last rather than being skipped,
        # so a request is still attempted if every provider is cooling.
        now = time.monotonic()
        ready = [p for p in self.providers if self._cooldown_until.get(p.label, 0) <= now]
        cooling = [p for p in self.providers if p not in ready]
        return ready + cooling

    def _cool_down(self, provider: Provider, retry_after_s: float | None) -> None:
        seconds = min(max(retry_after_s or 1.0, 1.0), 60.0)
        self._cooldown_until[provider.label] = time.monotonic() + seconds

    async def _attempt(
        self, provider: Provider, body: dict[str, Any], request_id: str
    ) -> RoutedResponse:
        payload = {**body, "model": provider.model} if provider.model else body
        headers = {"content-type": "application/json", "x-request-id": request_id}
        if provider.api_key:
            headers["authorization"] = f"Bearer {provider.api_key}"
        request = self.client.build_request(
            "POST",
            f"{provider.base_url.rstrip('/')}/chat/completions",
            content=json.dumps(payload).encode(),
            headers=headers,
        )
        streaming = bool(body.get("stream"))
        response: httpx.Response | None = None
        try:
            async with asyncio.timeout(self.attempt_timeout_s):
                response = await self.client.send(request, stream=True)
                if response.status_code != 200:
                    await response.aread()
                    raise self._failure_for(provider, response)
                if streaming:
                    chunks = response.aiter_raw()
                    first = await anext(chunks, b"")
                else:
                    content = await response.aread()
        except TimeoutError:
            await _close(response)
            raise UpstreamFailure("timeout", provider.label) from None
        except httpx.HTTPError as exc:
            await _close(response)
            raise UpstreamFailure("connection", provider.label) from exc
        except UpstreamFailure:
            await _close(response)
            raise

        if streaming:
            return RoutedResponse(
                provider.label, 0, stream=_prepend(first, chunks), close=response.aclose
            )
        await response.aclose()
        try:
            parsed = json.loads(content)
        except ValueError:
            raise UpstreamFailure("server_error", provider.label, 200) from None
        if not isinstance(parsed, dict):
            raise UpstreamFailure("server_error", provider.label, 200)
        return RoutedResponse(provider.label, 0, payload=parsed)

    @staticmethod
    def _failure_for(provider: Provider, response: httpx.Response) -> UpstreamFailure:
        status = response.status_code
        if status == 429:
            return UpstreamFailure(
                "rate_limited",
                provider.label,
                status,
                _parse_retry_after(response.headers.get("retry-after")),
            )
        if status >= 500:
            return UpstreamFailure("server_error", provider.label, status)
        return UpstreamFailure("rejected", provider.label, status)


async def _close(response: httpx.Response | None) -> None:
    if response is not None:
        await response.aclose()


async def _prepend(first: bytes, rest: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    if first:
        yield first
    async for chunk in rest:
        yield chunk


def _parse_retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
