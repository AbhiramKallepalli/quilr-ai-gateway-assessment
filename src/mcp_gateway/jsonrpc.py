"""JSON-RPC 2.0 wire-format parsing for the gateway.

The gateway makes a security decision from the parsed body and then forwards
the original bytes, so the parse has to be unambiguous: if this parser and
the downstream server could read the same bytes differently, the decision
would not be about the request that actually runs. Duplicate object keys are
the classic way to cause that, so they are rejected outright.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Implementation-defined server errors (-32000 to -32099).
UNAUTHORIZED_TOOL_CALL = -32001
UNAUTHENTICATED = -32002
DOWNSTREAM_UNAVAILABLE = -32003
BATCH_REJECTED = -32004

JsonRpcId = str | int | None


class ProtocolError(Exception):
    def __init__(self, code: int, message: str, request_id: JsonRpcId = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


def error_response(
    request_id: JsonRpcId, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key {key!r}")
        obj[key] = value
    return obj


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def loads_strict(body: bytes) -> Any:
    try:
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(PARSE_ERROR, "Parse error") from exc


@dataclass(frozen=True)
class Message:
    raw: dict[str, Any]
    id: JsonRpcId
    method: str | None

    @property
    def is_request(self) -> bool:
        return self.method is not None and "id" in self.raw

    @property
    def is_notification(self) -> bool:
        return self.method is not None and "id" not in self.raw


def _valid_id(value: Any) -> bool:
    # MCP narrows JSON-RPC ids to strings and integers. bool is an int subclass.
    return (
        value is None
        or isinstance(value, str)
        or (isinstance(value, int) and not isinstance(value, bool))
    )


def parse_message(obj: Any) -> Message:
    """Validate one JSON-RPC message (request, notification or client response)."""
    if not isinstance(obj, dict):
        raise ProtocolError(INVALID_REQUEST, "Invalid Request: message must be an object")

    raw_id = obj.get("id")
    request_id = raw_id if _valid_id(raw_id) else None

    if obj.get("jsonrpc") != "2.0":
        raise ProtocolError(INVALID_REQUEST, "Invalid Request: jsonrpc must be '2.0'", request_id)
    if not _valid_id(raw_id):
        raise ProtocolError(INVALID_REQUEST, "Invalid Request: id must be a string or integer")

    if "method" in obj:
        method = obj["method"]
        if not isinstance(method, str) or not method:
            raise ProtocolError(
                INVALID_REQUEST, "Invalid Request: method must be a non-empty string", request_id
            )
        params = obj.get("params")
        if params is not None and not isinstance(params, (dict, list)):
            raise ProtocolError(
                INVALID_REQUEST, "Invalid Request: params must be an object or array", request_id
            )
        return Message(raw=obj, id=request_id, method=method)

    # A client may also send responses (to server-initiated requests such as sampling).
    if "id" in obj and ("result" in obj or "error" in obj):
        return Message(raw=obj, id=request_id, method=None)

    raise ProtocolError(INVALID_REQUEST, "Invalid Request: missing method", request_id)
