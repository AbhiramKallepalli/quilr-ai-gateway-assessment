"""Authorization decisions for MCP traffic.

Only tools/call is gated. Discovery, lifecycle and notifications pass through,
because the gateway is not the authority on which methods exist downstream.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from .config import Principal, Role
from .jsonrpc import INVALID_PARAMS, Message, ProtocolError

ADMIN_TOOL_PREFIX = "admin_"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    tool: str | None = None


def normalize_tool_name(name: str) -> str:
    # NFKC folds look-alikes such as full-width "ａｄｍｉｎ_" into plain ASCII,
    # casefold handles "ADMIN_", strip handles padding.
    return unicodedata.normalize("NFKC", name).strip().casefold()


def is_admin_tool(name: str) -> bool:
    return normalize_tool_name(name).startswith(ADMIN_TOOL_PREFIX)


def _tool_name(message: Message) -> str:
    params = message.raw.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise ProtocolError(
            INVALID_PARAMS, "Invalid params: tools/call requires params.name", message.id
        )
    # Control and format characters (e.g. zero-width spaces) have no place in a
    # tool name and are a common way to dodge prefix checks.
    if any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in name):
        raise ProtocolError(
            INVALID_PARAMS, "Invalid params: tool name contains control characters", message.id
        )
    return name


def authorize(principal: Principal, message: Message) -> Decision:
    if message.method != "tools/call":
        return Decision(True, "method is not gated")
    name = _tool_name(message)
    if is_admin_tool(name) and principal.role is not Role.ADMIN:
        return Decision(False, "admin tool requires the admin role", name)
    return Decision(True, "allowed", name)


def filter_tool_list(principal: Principal, payload: Any) -> Any:
    """Drop admin_* tools from a tools/list result for non-admin callers."""
    if principal.role is Role.ADMIN or not isinstance(payload, dict):
        return payload
    tools = payload.get("result", {}).get("tools")
    if isinstance(tools, list):
        payload["result"]["tools"] = [
            t for t in tools if not (isinstance(t, dict) and is_admin_tool(str(t.get("name", ""))))
        ]
    return payload
