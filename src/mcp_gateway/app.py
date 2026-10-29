"""MCP security gateway: an authenticating, authorizing reverse proxy for MCP over HTTP.

Request path for POST /mcp:
  1. authenticate the Bearer token            -> HTTP 401 if missing or unknown
  2. parse the JSON-RPC body strictly          -> -32700 / -32600 / -32602
  3. authorize every message in the body       -> -32001 for a denied tools/call
  4. forward the original bytes unchanged, or answer locally without calling downstream
  5. write one audit event per decision

Denied calls are answered with HTTP 200 and a JSON-RPC error rather than 403:
MCP clients treat non-2xx responses as transport failures, so a 403 would hide
the -32001 code from the agent. Malformed bodies get HTTP 400, as the official
MCP server does.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .audit import audit
from .auth import AuthError, authenticate
from .config import Principal, Settings
from .jsonrpc import (
    BATCH_REJECTED,
    DOWNSTREAM_UNAVAILABLE,
    INVALID_REQUEST,
    UNAUTHENTICATED,
    UNAUTHORIZED_TOOL_CALL,
    JsonRpcId,
    Message,
    ProtocolError,
    error_response,
    loads_strict,
    parse_message,
)
from .policy import authorize, filter_tool_list

logger = logging.getLogger("mcp_gateway")

# Allowlists, so nothing the client sends (Authorization, cookies, a spoofed
# X-Gateway-Role) reaches the downstream server by accident.
FORWARDED_REQUEST_HEADERS = (
    "content-type",
    "accept",
    "mcp-session-id",
    "mcp-protocol-version",
    "last-event-id",
)
FORWARDED_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "cache-control")


def _downstream_headers(request: Request, principal: Principal, request_id: str) -> dict[str, str]:
    headers = {k: v for k in FORWARDED_REQUEST_HEADERS if (v := request.headers.get(k))}
    headers.update(
        {
            # Trusted identity for the downstream server, set only after auth passed.
            "x-gateway-subject": principal.subject,
            "x-gateway-role": principal.role.value,
            "x-request-id": request_id,
            # Raw passthrough of the response body requires an uncompressed upstream.
            "accept-encoding": "identity",
        }
    )
    return headers


def _response_headers(upstream: httpx.Response, request_id: str) -> dict[str, str]:
    headers = {k: v for k in FORWARDED_RESPONSE_HEADERS if (v := upstream.headers.get(k))}
    headers["x-request-id"] = request_id
    return headers


def _jsonrpc_error(
    request_id: str,
    rpc_id: JsonRpcId,
    code: int,
    message: str,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        error_response(rpc_id, code, message),
        status_code=status,
        headers={"x-request-id": request_id, **(headers or {})},
    )


def create_app(settings: Settings, downstream: httpx.AsyncClient | None = None) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        owns_client = getattr(app.state, "client", None) is None
        if owns_client:
            app.state.client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.downstream_timeout_s, connect=5.0)
            )
        yield
        if owns_client:
            await app.state.client.aclose()

    async def _authenticate(request: Request, request_id: str) -> Principal | Response:
        try:
            return authenticate(request.headers.get("authorization"), settings.tokens)
        except AuthError as exc:
            audit(
                "auth_failed",
                request_id=request_id,
                reason=str(exc),
                client=request.client.host if request.client else None,
            )
            return _jsonrpc_error(
                request_id,
                None,
                UNAUTHENTICATED,
                "Authentication required",
                status=401,
                headers={"www-authenticate": 'Bearer realm="mcp-gateway"'},
            )

    async def _read_body(request: Request, request_id: str) -> bytes | Response:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
            return _jsonrpc_error(request_id, None, INVALID_REQUEST, "Request body too large", 413)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > settings.max_body_bytes:
                return _jsonrpc_error(
                    request_id, None, INVALID_REQUEST, "Request body too large", 413
                )
        return body

    async def _forward(
        request: Request,
        method: str,
        principal: Principal,
        request_id: str,
        body: bytes | None = None,
        rpc_id: JsonRpcId = None,
        filter_tools: bool = False,
    ) -> Response:
        client: httpx.AsyncClient = request.app.state.client
        upstream_request = client.build_request(
            method,
            settings.downstream_url,
            content=body,
            headers=_downstream_headers(request, principal, request_id),
        )
        started = time.perf_counter()
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            # Details go to the log, never to the client.
            logger.warning("downstream request failed: %r", exc, extra={"request_id": request_id})
            audit("downstream_error", request_id=request_id, error=type(exc).__name__)
            return _jsonrpc_error(
                request_id, rpc_id, DOWNSTREAM_UNAVAILABLE, "Downstream MCP server unavailable", 502
            )

        audit(
            "forwarded",
            request_id=request_id,
            http_method=method,
            status=upstream.status_code,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )

        if filter_tools:
            return await _filtered_tool_list(upstream, principal, request_id)
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=_response_headers(upstream, request_id),
            background=BackgroundTask(upstream.aclose),
        )

    async def _filtered_tool_list(
        upstream: httpx.Response, principal: Principal, request_id: str
    ) -> Response:
        try:
            raw = await upstream.aread()
        finally:
            await upstream.aclose()
        content_type = upstream.headers.get("content-type", "")
        try:
            if content_type.startswith("application/json"):
                raw = json.dumps(filter_tool_list(principal, json.loads(raw))).encode()
            elif content_type.startswith("text/event-stream"):
                lines = []
                for line in raw.decode("utf-8").split("\n"):
                    if line.startswith("data:"):
                        payload = filter_tool_list(principal, json.loads(line[5:]))
                        line = "data: " + json.dumps(payload)
                    lines.append(line)
                raw = "\n".join(lines).encode()
        except ValueError:
            logger.warning("could not parse tools/list response for filtering; passing through")
        return Response(
            raw, status_code=upstream.status_code, headers=_response_headers(upstream, request_id)
        )

    async def mcp_post(request: Request) -> Response:
        request_id = uuid.uuid4().hex
        principal = await _authenticate(request, request_id)
        if isinstance(principal, Response):
            return principal

        body = await _read_body(request, request_id)
        if isinstance(body, Response):
            return body

        try:
            payload = loads_strict(body)
        except ProtocolError as exc:
            audit(
                "rejected", request_id=request_id, subject=principal.subject, reason="parse_error"
            )
            return _jsonrpc_error(request_id, None, exc.code, exc.message, 400)

        is_batch = isinstance(payload, list)
        items = payload if is_batch else [payload]
        if is_batch and not items:
            return _jsonrpc_error(
                request_id, None, INVALID_REQUEST, "Invalid Request: empty batch", 400
            )

        # Decide every message before anything is forwarded.
        messages: list[Message | None] = []
        local_errors: dict[int, dict[str, Any]] = {}
        for index, item in enumerate(items):
            try:
                message = parse_message(item)
                decision = authorize(principal, message)
            except ProtocolError as exc:
                messages.append(None)
                local_errors[index] = error_response(exc.request_id, exc.code, exc.message)
                audit(
                    "rejected",
                    request_id=request_id,
                    subject=principal.subject,
                    reason=exc.message,
                    jsonrpc_id=exc.request_id,
                )
                continue
            messages.append(message)
            audit(
                "decision",
                request_id=request_id,
                subject=principal.subject,
                role=principal.role.value,
                method=message.method,
                tool=decision.tool,
                jsonrpc_id=message.id,
                decision="allow" if decision.allowed else "deny",
                reason=decision.reason,
            )
            if not decision.allowed:
                local_errors[index] = error_response(
                    message.id,
                    UNAUTHORIZED_TOOL_CALL,
                    "Unauthorized Tool Call",
                    {"tool": decision.tool, "required_role": "admin"},
                )

        if local_errors:
            return _answer_locally(items, messages, local_errors, is_batch, request_id)

        single = messages[0] if not is_batch else None
        filter_tools = bool(
            settings.hide_unauthorized_tools
            and single is not None
            and single.method == "tools/list"
        )
        return await _forward(
            request,
            "POST",
            principal,
            request_id,
            body=body,
            rpc_id=single.id if single else None,
            filter_tools=filter_tools,
        )

    def _answer_locally(
        items: list[Any],
        messages: list[Message | None],
        local_errors: dict[int, dict[str, Any]],
        is_batch: bool,
        request_id: str,
    ) -> Response:
        headers = {"x-request-id": request_id}
        if not is_batch:
            error = local_errors[0]
            message = messages[0]
            if message is not None and message.is_notification:
                return Response(status_code=202, headers=headers)  # notifications get no reply
            status = 200 if error["error"]["code"] == UNAUTHORIZED_TOOL_CALL else 400
            return JSONResponse(error, status_code=status, headers=headers)

        # A batch is all-or-nothing: if any message is rejected, none of it is
        # forwarded. Partially executing a batch the client sent as one unit is
        # worse than refusing it.
        responses = []
        for index, message in enumerate(messages):
            if index in local_errors:
                if message is None or not message.is_notification:
                    responses.append(local_errors[index])
            elif message is not None and message.is_request:
                responses.append(
                    error_response(
                        message.id,
                        BATCH_REJECTED,
                        "Batch rejected: it contains an invalid or unauthorized call",
                    )
                )
        if not responses:
            return Response(status_code=202, headers=headers)
        return JSONResponse(responses, status_code=200, headers=headers)

    async def mcp_stream(request: Request) -> Response:
        # GET opens the server-to-client SSE stream, DELETE ends a session.
        # Neither carries a tool call, so they only need authentication.
        request_id = uuid.uuid4().hex
        principal = await _authenticate(request, request_id)
        if isinstance(principal, Response):
            return principal
        return await _forward(request, request.method, principal, request_id)

    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/mcp", mcp_post, methods=["POST"]),
            Route("/mcp", mcp_stream, methods=["GET", "DELETE"]),
            Route("/healthz", healthz, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    if downstream is not None:
        app.state.client = downstream
    return app
