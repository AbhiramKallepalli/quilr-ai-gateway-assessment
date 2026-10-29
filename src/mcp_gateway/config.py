"""Gateway settings, loaded and validated once at startup.

A bad token file or an unknown role should stop the gateway from starting,
not surface later as a confusing authorization failure.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class Role(StrEnum):
    ADMIN = "admin"
    VIEWER = "viewer"


@dataclass(frozen=True)
class Principal:
    subject: str
    role: Role


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_tokens(data: Any) -> dict[str, Principal]:
    """Build the token table from {"<token>": {"subject": "...", "role": "admin|viewer"}}.

    Tokens are kept as SHA-256 digests, so the raw secrets are not held in memory
    after startup and lookups don't compare secrets directly.
    """
    if not isinstance(data, dict) or not data:
        raise ValueError("token config must be a non-empty JSON object")
    table: dict[str, Principal] = {}
    for token, entry in data.items():
        if not isinstance(token, str) or len(token) < 16:
            raise ValueError("every token must be a string of at least 16 characters")
        if not isinstance(entry, dict) or "subject" not in entry or "role" not in entry:
            raise ValueError("every token needs a 'subject' and a 'role'")
        table[hash_token(token)] = Principal(
            subject=str(entry["subject"]), role=Role(entry["role"])
        )
    return table


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    downstream_url: str
    tokens: dict[str, Principal] = field(repr=False)
    # Off by default: the spec asks for tools/list to be forwarded transparently.
    # On, viewers simply never see admin_* tools in discovery.
    hide_unauthorized_tools: bool = False
    max_body_bytes: int = 1_000_000
    downstream_timeout_s: float = 30.0

    @classmethod
    def from_env(cls) -> Settings:
        raw = os.environ.get("GATEWAY_TOKENS")
        path = os.environ.get("GATEWAY_TOKENS_FILE")
        if path:
            raw = Path(path).read_text(encoding="utf-8")
        if not raw:
            raise RuntimeError("set GATEWAY_TOKENS (JSON) or GATEWAY_TOKENS_FILE")
        return cls(
            downstream_url=os.environ.get("GATEWAY_DOWNSTREAM_URL", "http://127.0.0.1:9000/mcp"),
            tokens=parse_tokens(json.loads(raw)),
            hide_unauthorized_tools=_flag("GATEWAY_HIDE_UNAUTHORIZED_TOOLS"),
            max_body_bytes=int(os.environ.get("GATEWAY_MAX_BODY_BYTES", "1000000")),
            downstream_timeout_s=float(os.environ.get("GATEWAY_DOWNSTREAM_TIMEOUT_S", "30")),
        )
