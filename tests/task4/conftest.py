from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from mocks.llm_provider import create_mock_provider
from model_router.app import Settings, create_app
from model_router.limiter import SlidingWindowLimiter
from model_router.router import Provider

TENANT_KEY = "sk-tenant-alpha-0123456789"
OTHER_TENANT_KEY = "sk-tenant-beta-0123456789"


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class HostRouter(httpx.AsyncBaseTransport):
    """Sends each request to the in-process app registered for its host."""

    def __init__(self, apps: dict[str, Starlette]) -> None:
        self._transports = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transports[request.url.host].handle_async_request(request)


class Gateway:
    def __init__(
        self,
        client: httpx.AsyncClient,
        primary: Starlette,
        secondary: Starlette,
        limiter: SlidingWindowLimiter,
    ) -> None:
        self.client = client
        self.primary = primary
        self.secondary = secondary
        self.limiter = limiter

    async def complete(self, key: str = TENANT_KEY, **body: Any) -> httpx.Response:
        payload = {
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 100,
            **body,
        }
        return await self.client.post(
            "/v1/chat/completions", json=payload, headers={"authorization": f"Bearer {key}"}
        )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def make_gateway(tmp_path, clock) -> AsyncIterator[Any]:
    created: list[tuple[httpx.AsyncClient, SlidingWindowLimiter]] = []

    async def factory(
        primary: Starlette | None = None,
        secondary: Starlette | None = None,
        limit: int = 50_000,
        timeout_s: float = 0.3,
    ) -> Gateway:
        primary = primary or create_mock_provider("primary-model")
        secondary = secondary or create_mock_provider("secondary-model")
        upstream = httpx.AsyncClient(
            transport=HostRouter({"primary": primary, "secondary": secondary})
        )
        settings = Settings(
            tenant_keys=Settings.hash_keys({TENANT_KEY: "alpha", OTHER_TENANT_KEY: "beta"}),
            providers=(
                Provider("primary", "http://primary/v1", api_key="sk-primary-secret"),
                Provider("secondary", "http://secondary/v1", model="backup-model"),
            ),
            tokens_per_minute=limit,
            attempt_timeout_s=timeout_s,
        )
        limiter = SlidingWindowLimiter(tmp_path / "router.db", limit, clock=clock)
        app = create_app(settings, client=upstream, limiter=limiter)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
        created.append((client, limiter))
        return Gateway(client, primary, secondary, limiter)

    yield factory
    for client, limiter in created:
        await client.aclose()
        await limiter.close()
