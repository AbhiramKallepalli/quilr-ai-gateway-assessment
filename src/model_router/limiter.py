"""Token-aware sliding-window rate limiter backed by an on-disk SQLite database.

Each request is recorded as a row (tenant, timestamp, tokens). A tenant's usage
is the sum of its rows from the last 60 seconds; older rows are evicted on
every check. This is a sliding *log* rather than fixed buckets, so a tenant
can't burst 2x the limit across a bucket boundary.

Reserve, then reconcile:
  1. Before calling a model, reserve the *estimated* tokens. The check and the
     insert run in one BEGIN IMMEDIATE transaction, so concurrent requests,
     even from several gateway processes sharing the file, cannot all pass
     a check that only one of them should pass.
  2. When the response arrives, replace the estimate with the provider's
     reported usage. If no provider produced a completion, the reservation is
     released.

All SQLite work runs on one dedicated thread so the event loop never blocks
on disk I/O and the connection is only ever used from a single thread.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant  TEXT    NOT NULL,
    ts_ms   INTEGER NOT NULL,
    tokens  INTEGER NOT NULL CHECK (tokens >= 0),
    state   TEXT    NOT NULL CHECK (state IN ('reserved', 'committed'))
);
CREATE INDEX IF NOT EXISTS token_usage_tenant_ts ON token_usage (tenant, ts_ms);
CREATE INDEX IF NOT EXISTS token_usage_ts ON token_usage (ts_ms);
"""


class RateLimited(Exception):
    def __init__(self, retry_after_s: float, used: int, limit: int) -> None:
        super().__init__(f"token limit reached ({used}/{limit})")
        self.retry_after_s = retry_after_s
        self.used = used
        self.limit = limit


class RequestTooLarge(Exception):
    """The request alone needs more tokens than the window allows; it can never pass."""


@dataclass(frozen=True)
class Reservation:
    id: int
    tenant: str
    tokens: int
    remaining: int


class SlidingWindowLimiter:
    def __init__(
        self,
        db_path: str | Path,
        limit_tokens: int,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if limit_tokens <= 0:
            raise ValueError("limit_tokens must be positive")
        self.db_path = str(db_path)
        self.limit = limit_tokens
        self.window_ms = int(window_s * 1000)
        self._clock = clock
        self._conn: sqlite3.Connection | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rate-limiter")

    # -- async API used by the gateway ------------------------------------------

    async def reserve(self, tenant: str, tokens: int) -> Reservation:
        return await self._run(self.reserve_sync, tenant, tokens)

    async def commit(self, reservation: Reservation, actual_tokens: int) -> None:
        await self._run(self.commit_sync, reservation, actual_tokens)

    async def release(self, reservation: Reservation) -> None:
        await self._run(self.release_sync, reservation)

    async def usage(self, tenant: str) -> int:
        return await self._run(self.usage_sync, tenant)

    async def close(self) -> None:
        await self._run(self._close_sync)
        self._executor.shutdown(wait=True)

    async def _run(self, fn: Callable[..., object], *args: object):  # type: ignore[no-untyped-def]
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    # -- synchronous core (always called on the limiter thread) -----------------

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            # isolation_level=None: we issue BEGIN/COMMIT ourselves.
            conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
            conn.execute("PRAGMA busy_timeout=5000")
            # Switching to WAL needs a brief exclusive lock and doesn't wait on the
            # busy handler, so two processes starting together can collide here.
            for attempt in range(50):
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript(SCHEMA)
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or attempt == 49:
                        raise
                    time.sleep(0.02)
            conn.execute("PRAGMA synchronous=NORMAL")
            self._conn = conn
        return self._conn

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def reserve_sync(self, tenant: str, tokens: int) -> Reservation:
        if tokens > self.limit:
            raise RequestTooLarge(f"request needs {tokens} tokens, limit is {self.limit}/window")
        conn = self._connection()
        now = self._now_ms()
        cutoff = now - self.window_ms
        conn.execute("BEGIN IMMEDIATE")  # take the write lock before reading usage
        try:
            conn.execute("DELETE FROM token_usage WHERE ts_ms <= ?", (cutoff,))
            used = conn.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant = ? AND ts_ms > ?",
                (tenant, cutoff),
            ).fetchone()[0]
            if used + tokens > self.limit:
                retry_after = self._retry_after_ms(conn, tenant, cutoff, used, tokens, now)
                conn.execute("COMMIT")  # keep the eviction
                raise RateLimited(retry_after / 1000, used, self.limit)
            cursor = conn.execute(
                "INSERT INTO token_usage (tenant, ts_ms, tokens, state) "
                "VALUES (?, ?, ?, 'reserved')",
                (tenant, now, tokens),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        if cursor.lastrowid is None:  # pragma: no cover - invariant
            raise RuntimeError("cursor.lastrowid missing")
        return Reservation(cursor.lastrowid, tenant, tokens, self.limit - used - tokens)

    def _retry_after_ms(
        self, conn: sqlite3.Connection, tenant: str, cutoff: int, used: int, tokens: int, now: int
    ) -> int:
        """Time until enough of the oldest usage leaves the window for this request to fit."""
        must_free = used + tokens - self.limit
        freed = 0
        rows = conn.execute(
            "SELECT ts_ms, tokens FROM token_usage WHERE tenant = ? AND ts_ms > ? ORDER BY ts_ms",
            (tenant, cutoff),
        )
        for ts_ms, row_tokens in rows:
            freed += row_tokens
            if freed >= must_free:
                return max(1, ts_ms + self.window_ms - now)
        return self.window_ms

    def commit_sync(self, reservation: Reservation, actual_tokens: int) -> None:
        self._connection().execute(
            "UPDATE token_usage SET tokens = ?, state = 'committed' WHERE id = ?",
            (max(0, int(actual_tokens)), reservation.id),
        )

    def release_sync(self, reservation: Reservation) -> None:
        self._connection().execute("DELETE FROM token_usage WHERE id = ?", (reservation.id,))

    def usage_sync(self, tenant: str) -> int:
        cutoff = self._now_ms() - self.window_ms
        return (
            self._connection()
            .execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant = ? AND ts_ms > ?",
                (tenant, cutoff),
            )
            .fetchone()[0]
        )

    def _close_sync(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def retry_after_header(seconds: float) -> str:
    return str(max(1, math.ceil(seconds)))
