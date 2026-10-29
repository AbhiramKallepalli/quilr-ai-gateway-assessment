"""Customer records MCP server over stdio.

Tools:
  get_customer_record(customer_id)
  trigger_refund(customer_id, amount, reason, idempotency_key?)

Error contract:
  - Arguments that break the schema, or an unknown tool name, are a protocol
    error: JSON-RPC -32602 (Invalid params) with per-field details in error.data.
  - A valid call that fails for business reasons (unknown customer, refund over
    the balance) is a normal tool result with isError=true, so the model can
    read the reason and decide what to do next.
  - Anything unexpected is logged to stderr and returned as -32603 with a
    generic message, so internals never reach the client.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, ValidationError

from .logging_setup import configure_logging
from .schemas import GetCustomerRecordInput, TriggerRefundInput, describe_validation_error
from .store import CustomerStore, StoreError
from .transport import reserve_stdout_for_protocol

logger = logging.getLogger("customer_mcp")

TOOL_INPUTS: dict[str, type[BaseModel]] = {
    "get_customer_record": GetCustomerRecordInput,
    "trigger_refund": TriggerRefundInput,
}

TOOLS = [
    types.Tool(
        name="get_customer_record",
        description="Look up a customer's account record by customer ID.",
        inputSchema=GetCustomerRecordInput.model_json_schema(),
        annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    ),
    types.Tool(
        name="trigger_refund",
        description=(
            "Issue a refund to a customer. Pass an idempotency_key when retrying so the "
            "refund is not issued twice."
        ),
        inputSchema=TriggerRefundInput.model_json_schema(),
        annotations=types.ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    ),
]


def _invalid_params(message: str, data: Any = None) -> McpError:
    return McpError(types.ErrorData(code=types.INVALID_PARAMS, message=message, data=data))


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> types.ServerResult:
    return types.ServerResult(
        types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))],
            structuredContent=payload,
            isError=is_error,
        )
    )


def _debug_noise(tool: str) -> None:
    # Only used by the stdout-purity tests: proves a stray print() can't break the wire.
    if os.getenv("CUSTOMER_MCP_NOISY") == "1":
        print(f"DEBUG-NOISE stray print from {tool}")


def build_server(store: CustomerStore | None = None) -> Server:
    store = store or CustomerStore.with_sample_data()
    server: Server = Server("customer-records-mcp", version="1.0.0")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return TOOLS

    # Registered directly rather than through @server.call_tool(), because that
    # decorator turns every exception into an isError result. Schema violations
    # must surface as real JSON-RPC errors.
    async def call_tool(request: types.CallToolRequest) -> types.ServerResult:
        name = request.params.name
        arguments = request.params.arguments or {}

        model = TOOL_INPUTS.get(name)
        if model is None:
            raise _invalid_params(f"Unknown tool: {name[:64]}")

        try:
            args = model.model_validate(arguments)
        except ValidationError as exc:
            errors = describe_validation_error(exc)
            logger.info("rejected tool call", extra={"fields": {"tool": name, "errors": errors}})
            raise _invalid_params(
                f"Invalid arguments for tool '{name}'", {"errors": errors}
            ) from None

        _debug_noise(name)
        try:
            if isinstance(args, GetCustomerRecordInput):
                payload = store.get_record(args.customer_id)
            elif isinstance(args, TriggerRefundInput):
                payload = store.refund(
                    args.customer_id, args.amount, args.reason, args.idempotency_key
                )
            else:  # pragma: no cover - TOOL_INPUTS and this dispatch must stay in sync
                raise RuntimeError(f"no handler for {name}")
        except StoreError as exc:
            logger.info("tool call failed", extra={"fields": {"tool": name, "code": exc.code}})
            return _tool_result({"error": exc.code, "message": str(exc)}, is_error=True)
        except Exception:
            logger.exception("unexpected error in tool", extra={"fields": {"tool": name}})
            raise McpError(
                types.ErrorData(code=types.INTERNAL_ERROR, message="Internal error")
            ) from None

        logger.info("tool call ok", extra={"fields": {"tool": name}})
        return _tool_result(payload)

    server.request_handlers[types.CallToolRequest] = call_tool
    return server


async def _serve(protocol_out: anyio.AsyncFile[str]) -> None:
    server = build_server()
    async with stdio_server(stdout=protocol_out) as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    protocol_out = reserve_stdout_for_protocol()
    configure_logging(os.getenv("CUSTOMER_MCP_LOG_LEVEL", "INFO"))
    logger.info("customer-records-mcp listening on stdio")
    try:
        anyio.run(_serve, protocol_out)
    except KeyboardInterrupt:
        logger.info("customer-records-mcp stopped")


if __name__ == "__main__":
    main()