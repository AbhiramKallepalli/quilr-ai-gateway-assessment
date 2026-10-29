"""Measure what the streaming redactor costs.

Run: uv run python scripts/bench_guardrail.py

Reports throughput, per-chunk overhead (the latency the guardrail adds to each
delta, and so to time-to-first-token), and how much text was held back.
"""

from __future__ import annotations

import random
import statistics
import time

from stream_guardrail.redactor import StreamRedactor

PROSE = (
    "The quarterly review covered onboarding, renewals and the support backlog. "
    "Most accounts are healthy and the team expects steady growth next quarter. "
)
SECRETS = ["jane.doe@example.com", "123-45-6789", "4111 1111 1111 1111"]


def build_text(size: int, seed: int = 7) -> str:
    rng = random.Random(seed)  # noqa: S311 - benchmark data, not security
    parts, length = [], 0
    while length < size:
        piece = rng.choice(SECRETS) + " " if rng.random() < 0.05 else PROSE
        parts.append(piece)
        length += len(piece)
    return "".join(parts)


def run(text: str, chunk_size: int) -> None:
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
    redactor = StreamRedactor()
    per_chunk, held = [], []
    started = time.perf_counter()
    for piece in chunks:
        t0 = time.perf_counter()
        redactor.feed(piece)
        per_chunk.append(time.perf_counter() - t0)
        held.append(redactor.pending)
    redactor.flush()
    elapsed = time.perf_counter() - started

    per_chunk_us = sorted(t * 1e6 for t in per_chunk)
    print(
        f"chunk={chunk_size:>3} chars | {len(text) / elapsed / 1e6:6.2f} MB/s | "
        f"per chunk p50={statistics.median(per_chunk_us):6.1f}us "
        f"p99={per_chunk_us[int(len(per_chunk_us) * 0.99)]:7.1f}us | "
        f"held avg={statistics.mean(held):5.1f} max={max(held)} chars | "
        f"redacted={sum(redactor.counts.values())}"
    )


if __name__ == "__main__":
    text = build_text(1_000_000)
    print(f"{len(text):,} characters of mixed prose and PII")
    for size in (4, 16, 64):
        run(text, size)
