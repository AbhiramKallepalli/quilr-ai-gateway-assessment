import asyncio
import os
import subprocess
import sys
import textwrap

import pytest

from model_router.limiter import RateLimited, RequestTooLarge, SlidingWindowLimiter

from .conftest import FakeClock


@pytest.fixture
async def limiter(tmp_path, clock):
    lim = SlidingWindowLimiter(tmp_path / "limits.db", 50_000, clock=clock)
    yield lim
    await lim.close()


async def test_tokens_accumulate_until_the_limit(limiter):
    await limiter.reserve("alpha", 30_000)
    await limiter.reserve("alpha", 20_000)
    with pytest.raises(RateLimited):
        await limiter.reserve("alpha", 1)


async def test_tenants_are_isolated(limiter):
    await limiter.reserve("alpha", 50_000)
    await limiter.reserve("beta", 50_000)


async def test_window_slides_rather_than_resetting(limiter, clock: FakeClock):
    await limiter.reserve("alpha", 30_000)  # t = 0
    clock.advance(40)
    await limiter.reserve("alpha", 20_000)  # t = 40
    clock.advance(19.999)  # t = 59.999: both still inside the window
    with pytest.raises(RateLimited):
        await limiter.reserve("alpha", 1)
    clock.advance(0.001)  # t = 60: the first 30k leaves the window
    await limiter.reserve("alpha", 30_000)
    with pytest.raises(RateLimited):
        await limiter.reserve("alpha", 1)


async def test_retry_after_is_when_enough_usage_expires(limiter, clock: FakeClock):
    await limiter.reserve("alpha", 10_000)  # t = 0
    clock.advance(10)
    await limiter.reserve("alpha", 40_000)  # t = 10
    clock.advance(5)  # t = 15
    with pytest.raises(RateLimited) as small:
        await limiter.reserve("alpha", 5_000)  # needs the t=0 entry gone: at t=60
    assert small.value.retry_after_s == pytest.approx(45)
    with pytest.raises(RateLimited) as large:
        await limiter.reserve("alpha", 20_000)  # needs both gone: at t=70
    assert large.value.retry_after_s == pytest.approx(55)


async def test_expired_rows_are_evicted_from_disk(limiter, clock: FakeClock):
    for _ in range(5):
        await limiter.reserve("alpha", 1_000)
    clock.advance(61)
    await limiter.reserve("beta", 1)

    def count_rows() -> int:
        return limiter._connection().execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]

    assert await limiter._run(count_rows) == 1


async def test_commit_replaces_estimate_with_actual_usage(limiter):
    reservation = await limiter.reserve("alpha", 40_000)
    await limiter.commit(reservation, 1_200)
    assert await limiter.usage("alpha") == 1_200
    await limiter.reserve("alpha", 48_800)


async def test_release_returns_the_tokens(limiter):
    reservation = await limiter.reserve("alpha", 50_000)
    await limiter.release(reservation)
    assert await limiter.usage("alpha") == 0


async def test_request_larger_than_the_window_is_rejected_outright(limiter):
    with pytest.raises(RequestTooLarge):
        await limiter.reserve("alpha", 50_001)


async def test_concurrent_reservations_never_overshoot(limiter):
    results = await asyncio.gather(
        *(limiter.reserve("alpha", 2_000) for _ in range(50)), return_exceptions=True
    )
    granted = [r for r in results if not isinstance(r, Exception)]
    assert len(granted) == 25
    assert await limiter.usage("alpha") == 50_000


async def test_state_survives_a_restart(tmp_path, clock):
    first = SlidingWindowLimiter(tmp_path / "persist.db", 50_000, clock=clock)
    await first.reserve("alpha", 45_000)
    await first.close()

    second = SlidingWindowLimiter(tmp_path / "persist.db", 50_000, clock=clock)
    try:
        with pytest.raises(RateLimited):
            await second.reserve("alpha", 10_000)
    finally:
        await second.close()


WORKER = textwrap.dedent(
    """
    import sys
    from model_router.limiter import RateLimited, SlidingWindowLimiter
    limiter = SlidingWindowLimiter(sys.argv[1], 50_000)
    granted = 0
    for _ in range(30):
        try:
            limiter.reserve_sync("alpha", 2_000)
            granted += 1
        except RateLimited:
            pass
    print(granted)
    """
)


def test_separate_processes_share_one_limit(tmp_path):
    """Two gateway processes on the same database file still enforce one limit."""
    db = str(tmp_path / "shared.db")
    env = dict(os.environ)
    workers = [
        subprocess.Popen([sys.executable, "-c", WORKER, db], stdout=subprocess.PIPE, env=env)
        for _ in range(2)
    ]
    granted = [int(w.communicate(timeout=60)[0]) for w in workers]
    assert sum(granted) == 25
