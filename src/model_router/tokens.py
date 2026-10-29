"""Token estimation for rate limiting before the provider has answered.

A rough ~4 characters per token for the prompt, plus the completion budget the
caller asked for (max_tokens). It deliberately leans high: the estimate is
only a reservation and is replaced by the provider's real usage figure once
the response arrives. A tokenizer such as tiktoken would tighten it per model.
"""

from __future__ import annotations

import json
import math
from typing import Any

DEFAULT_COMPLETION_TOKENS = 1024
PER_MESSAGE_OVERHEAD = 4


def _content_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if content is None:
        return 0
    return len(json.dumps(content))  # multi-part content, tool calls, ...


def estimate_tokens(body: dict[str, Any]) -> int:
    messages = body.get("messages") or []
    chars = sum(_content_chars(m.get("content")) for m in messages if isinstance(m, dict))
    prompt = math.ceil(chars / 4) + PER_MESSAGE_OVERHEAD * len(messages)
    requested = body.get("max_tokens", body.get("max_completion_tokens"))
    completion = (
        requested
        if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0
        else DEFAULT_COMPLETION_TOKENS
    )
    return prompt + completion


def reported_usage(payload: Any) -> int | None:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    return total if isinstance(total, int) and total >= 0 else None
