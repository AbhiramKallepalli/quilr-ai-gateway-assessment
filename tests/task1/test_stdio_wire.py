"""Drives the real server process over stdio and checks the raw wire.

CUSTOMER_MCP_NOISY=1 makes every tool call print() to stdout on purpose, so
these tests also prove a stray print cannot corrupt the JSON-RPC stream.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from typing import Any

import pytest

INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601


class StdioSession:
    def __init__(self) -> None:
        env = dict(os.environ, CUSTOMER_MCP_NOISY="1", PYTHONUNBUFFERED="1")
        self._stderr = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "customer_mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env=env,
        )
        self.stdout_lines: list[bytes] = []
        self._lines: queue.Queue[bytes] = queue.Queue()
        self._pump_thread = threading.Thread(target=self._pump, daemon=True)
        self._pump_thread.start()
        self._next_id = 0

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)

    def send(self, message: dict[str, Any] | str) -> None:
        raw = message if isinstance(message, str) else json.dumps(message)
        assert self.proc.stdin is not None
        self.proc.stdin.write(raw.encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def _read(self, timeout: float = 20.0) -> dict[str, Any]:
        line = self._lines.get(timeout=timeout)
        self.stdout_lines.append(line)
        return json.loads(line)

    def request(self, method: str, params: Any = None) -> dict[str, Any]:
        self._next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        while True:
            response = self._read()
            if response.get("id") == self._next_id:
                return response

    def call_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> str:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait(timeout=15)
        self._pump_thread.join(timeout=5)
        while not self._lines.empty():
            self.stdout_lines.append(self._lines.get_nowait())
        self._stderr.seek(0)
        return self._stderr.read().decode("utf-8", errors="replace")


def _open_session() -> StdioSession:
    s = StdioSession()
    init = s.request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    )
    assert "result" in init, init
    s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return s


@pytest.fixture
def session() -> Iterator[StdioSession]:
    s = _open_session()
    yield s
    s.close()


def test_lists_both_tools_with_input_schemas(session):
    tools = {t["name"]: t for t in session.request("tools/list")["result"]["tools"]}
    assert set(tools) == {"get_customer_record", "trigger_refund"}
    assert tools["trigger_refund"]["inputSchema"]["additionalProperties"] is False


def test_get_customer_record_success(session):
    result = session.call_tool("get_customer_record", {"customer_id": "CUST-10001"})["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["customer_id"] == "CUST-10001"


def test_malformed_customer_id_is_a_jsonrpc_invalid_params_error(session):
    response = session.call_tool("get_customer_record", {"customer_id": "cust-1"})
    assert "result" not in response
    assert response["error"]["code"] == INVALID_PARAMS
    assert response["error"]["data"]["errors"][0]["field"] == "customer_id"


@pytest.mark.parametrize(
    "arguments",
    [
        {"customer_id": "CUST-10001", "amount": -5, "reason": "Duplicate charge on order"},
        {"customer_id": "CUST-10001", "amount": "5", "reason": "Duplicate charge on order"},
        {"customer_id": "CUST-10001", "amount": True, "reason": "Duplicate charge on order"},
        {"customer_id": "CUST-10001", "amount": 5, "reason": "too short"},
        {"customer_id": "CUST-10001", "amount": 5},
        {"customer_id": "CUST-10001", "amount": 5, "reason": "Duplicate charge", "x": 1},
    ],
)
def test_invalid_refund_arguments_are_rejected(session, arguments):
    response = session.call_tool("trigger_refund", arguments)
    assert response["error"]["code"] == INVALID_PARAMS


def test_non_object_arguments_are_rejected(session):
    response = session.request(
        "tools/call", {"name": "get_customer_record", "arguments": ["CUST-10001"]}
    )
    assert response["error"]["code"] == INVALID_PARAMS


def test_unknown_tool_is_invalid_params(session):
    response = session.call_tool("delete_everything", {})
    assert response["error"]["code"] == INVALID_PARAMS


def test_unknown_method_is_rejected(session):
    # JSON-RPC specifies -32601 here. The MCP Python SDK validates each request
    # against its known request types before dispatch, so an unknown method
    # fails validation and comes back as -32602. Noted in the README.
    response = session.request("tools/explode", {})
    assert response["error"]["code"] in (METHOD_NOT_FOUND, INVALID_PARAMS)


def test_business_failures_are_tool_results_not_protocol_errors(session):
    missing = session.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
    assert missing["result"]["isError"] is True
    assert missing["result"]["structuredContent"]["error"] == "customer_not_found"

    too_much = session.call_tool(
        "trigger_refund",
        {"customer_id": "CUST-10002", "amount": 500, "reason": "Customer requested refund"},
    )
    assert too_much["result"]["structuredContent"]["error"] == "exceeds_refundable_balance"


def test_refund_with_idempotency_key_is_applied_once(session):
    args = {
        "customer_id": "CUST-10001",
        "amount": 20,
        "reason": "Duplicate charge on invoice INV-77",
        "idempotency_key": "retry-test-0001",
    }
    first = session.call_tool("trigger_refund", args)["result"]["structuredContent"]
    second = session.call_tool("trigger_refund", args)["result"]["structuredContent"]
    assert first["refund_id"] == second["refund_id"]
    assert second["idempotent_replay"] is True
    record = session.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
    assert record["result"]["structuredContent"]["refundable_balance"] == 230.0


def test_server_survives_garbage_input(session):
    session.send("this is not json {")
    assert "result" in session.request("ping")


def test_stdout_carries_only_jsonrpc_messages():
    s = _open_session()
    s.request("tools/list")
    s.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
    s.call_tool("get_customer_record", {"customer_id": "bad"})
    s.call_tool(
        "trigger_refund",
        {"customer_id": "CUST-10001", "amount": 1, "reason": "Goodwill credit for outage"},
    )
    stderr = s.close()

    assert s.stdout_lines, "expected protocol output on stdout"
    for raw in s.stdout_lines:
        message = json.loads(raw)
        assert message["jsonrpc"] == "2.0"
        assert "DEBUG-NOISE" not in raw.decode("utf-8")

    # The deliberate print()s and the logs were diverted to stderr.
    assert "DEBUG-NOISE" in stderr
    assert "customer-records-mcp listening on stdio" in stderr