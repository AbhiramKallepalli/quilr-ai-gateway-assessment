# QuilrAI AI Field Developer / Solutions Engineer Assessment

This repository contains my solutions to the four assessment tasks. Each task focuses on a different part of an AI gateway: an MCP server, an MCP security proxy, a streaming PII filter, and a model router with rate limiting and failover.

| Task | Code | What it does |
| --- | --- | --- |
| 1 | [`src/customer_mcp`](src/customer_mcp) | MCP server over stdio with customer lookup, refund handling, and input validation |
| 2 | [`src/mcp_gateway`](src/mcp_gateway) | HTTP/JSON-RPC gateway with Bearer token authentication and role-based tool access |
| 3 | [`src/stream_guardrail`](src/stream_guardrail) | Streaming LLM proxy that redacts emails, SSNs, and credit card numbers |
| 4 | [`src/model_router`](src/model_router) | SQLite-backed token rate limiter with fallback to a secondary model provider |

The project uses Python 3.12 and `uv`. I included mock downstream services in [`src/mocks`](src/mocks), so the examples can run without external API keys. The repository includes 221 tests.

## Getting started

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/) first. Docker is only needed if you want to run the services through Compose.

```bash
uv sync                           # install dependencies
uv run pytest -q                  # run the test suite
uv run python scripts/demo.py     # run the demonstration
docker compose up --build         # start Tasks 2–4 on ports 8080, 8090, and 8100
```

To try the individual examples below, use bash or zsh (Git Bash or WSL on Windows). Task 1 starts directly; start the Compose services before testing Tasks 2–4.

```bash
# Task 1: the stdio server speaks JSON-RPC on stdout; a malformed customer_id is a -32602 error.
# stderr is discarded to show stdout holds nothing but protocol messages. The sleep keeps stdin
# open while the server answers, as a real MCP client would.
(printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"demo","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"cust-1"}}}'
 sleep 1) | uv run python -m customer_mcp 2>/dev/null
# {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18",...}}
# {"jsonrpc":"2.0","id":2,"error":{"code":-32602,"message":"Invalid arguments for tool 'get_customer_record'",...}}
# (or pipe into `docker compose run --rm -T customer-mcp` instead of `uv run ...`)

# Task 2: a viewer calling an admin tool is answered by the gateway, downstream never sees it
curl -s localhost:8080/mcp -H "Authorization: Bearer viewer-token-0123456789abcdef" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key"}}'
# {"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"Unauthorized Tool Call",...}}

# Task 3: PII redacted even though it arrives 3 characters at a time
curl -sN localhost:8090/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"m","stream":true,"mock_chunk_size":3,"messages":[{"role":"user","content":"x"}],
       "mock_response":"Email jane@example.com, SSN 123-45-6789, card 4111 1111 1111 1111."}'

# Task 4: primary times out after 3000 ms, the secondary answers
curl -si localhost:8100/v1/chat/completions -H "Authorization: Bearer sk-tenant-alpha-0123456789" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":50,
       "mock_delay_first_ms":4000,"mock_target":"primary"}'
# x-gateway-provider: secondary   x-gateway-attempts: 2
```

The `mock_*` request fields in these examples are controls for the local mock providers, not standard model-provider API parameters. For local runs, use `uv run --env-file .env.example python -m <service>`.

## How I tested the requirements

I organized the test suite by task under `tests/task1` through `tests/task4`. The table below points to the main checks for each requirement. The test files are the best place to verify the behavior in detail.

| Requirement | Implementation | Relevant tests |
| --- | --- | --- |
| Task 1: stdio isolation | The transport uses stdout for JSON-RPC and sends logs or incidental output to stderr. | `tests/task1/test_stdio_wire.py` |
| Task 1: errors and validation | Pydantic validates customer IDs, amounts, and refund reasons. Invalid tool arguments return JSON-RPC errors; business failures return tool results with `isError`. | `tests/task1/test_stdio_wire.py`, `test_schemas.py`, `test_official_client.py` |
| Task 2: request parsing | Checks malformed JSON-RPC messages, IDs, notifications, batches, and duplicate JSON keys. | `tests/task2/test_wire_format.py`, `test_batches.py` |
| Task 2: proxy and authorization | Tests forwarding, session headers, role checks, and blocking unauthorized `admin_*` calls before downstream execution. | `tests/task2/test_proxying.py`, `test_authorization.py` |
| Task 3: streaming | Tests SSE parsing, split UTF-8 sequences, and redaction when sensitive values span chunks. | `tests/task3/test_sse.py`, `test_pipeline.py`, `test_stream_redactor.py` |
| Task 3: buffering | Tests that output is streamed and the redactor does not store the full response. | `test_memory_stays_bounded_on_a_long_digit_run`, `test_output_is_streamed_not_buffered` |
| Task 4: rate limiting | Tests sliding-window usage, concurrent requests, persistence, and reservation handling. | `tests/task4/test_limiter.py` |
| Task 4: failover and errors | Tests timeouts, backup routing, and sanitized gateway errors. | `tests/task4/test_failover.py`, `test_gateway.py` |

## Task 1: Customer MCP server

```bash
uv run python -m customer_mcp              # speaks JSON-RPC on stdin/stdout, logs on stderr
docker compose run --rm -T customer-mcp    # same thing from the container image
```

Task 1 runs over stdio, so it is started by an MCP client rather than by the regular `docker compose up` command. The client sends JSON-RPC messages through stdin and reads the responses from stdout. I keep logging on stderr to avoid interfering with the protocol.

For Claude Desktop or another MCP client, a configuration can look like this (replace the path with your local repository path):

```json
{ "mcpServers": { "customer-records": {
    "command": "uv",
    "args": ["--directory", "C:/path/to/quilr-ai-gateway-assessment", "run", "python", "-m", "customer_mcp"]
} } }
```

The server exposes `get_customer_record` and `trigger_refund`. I used strict Pydantic models for their inputs. A customer ID must match `CUST-` followed by five ASCII digits. Refund amounts must be positive, finite values with at most two decimal places, and refund reasons must have at least ten characters after trimming whitespace.

I separated invalid requests from valid requests that fail for business reasons. Invalid arguments or unknown tools return JSON-RPC `-32602`. Cases such as a missing customer or an insufficient refundable balance return an MCP tool result with `isError: true`. I registered a low-level tool handler because the SDK's usual decorator handles exceptions as tool errors rather than exposing the validation errors in the required JSON-RPC format. One SDK detail: an unknown JSON-RPC *method* (as opposed to an unknown tool) comes back as `-32602` rather than `-32601`, because the SDK validates every request against its known request types before dispatch. `test_unknown_method_is_rejected` documents this.

`trigger_refund` also supports an optional `idempotency_key`. Repeating a refund with the same key returns the earlier result; using that key with different refund details is rejected. For retry safety, clients should supply the key. I also included tool annotations for clients that use them.

## Task 2: MCP security gateway

```bash
uv run python -m mocks.mcp_downstream --port 9000          # the downstream it proxies to
uv run --env-file .env.example python -m mcp_gateway       # gateway on :8080
```

The gateway sits between an MCP client and a mock downstream server. It authenticates the Bearer token, parses the JSON-RPC message, checks whether the requested tool is allowed, and either forwards the request or responds locally.

For `tools/list`, the gateway forwards the request by default. For `tools/call`, a tool name beginning with `admin_` requires an admin role. A viewer attempting an admin call receives JSON-RPC error `-32001` without the request reaching the downstream server.

I also handled a few cases that could cause authorization problems:

- The parser rejects duplicate JSON keys so the gateway and downstream service cannot interpret the same request differently.
- Tool names are normalized before checking the `admin_` prefix. Names containing control or zero-width characters are rejected.
- Unauthorized batches are rejected as a whole rather than forwarding some calls and blocking others.
- The gateway does not forward the client's authorization header or cookies. It sets its own downstream identity headers and replaces any client-supplied copies.
- An optional `GATEWAY_HIDE_UNAUTHORIZED_TOOLS=1` setting hides admin tools from viewer discovery responses. It is disabled by default because the assessment asks for transparent `tools/list` forwarding.

The mock authentication setup uses static tokens. For a production environment, authentication would need to integrate with the customer's identity provider.

## Task 3: Streaming PII guardrail

```bash
uv run python -m mocks.llm_provider --port 9100               # mock OpenAI-compatible provider
uv run --env-file .env.example python -m stream_guardrail     # base_url http://localhost:8090/v1
uv run python scripts/bench_guardrail.py
```

This service provides an OpenAI-compatible `POST /v1/chat/completions` endpoint. It forwards the request to an upstream model and checks the streamed response before sending text to the client. The example uses the included mock provider. `GUARDRAIL_UPSTREAM_URL` controls the upstream endpoint.

The main difficulty was handling sensitive information split across several chunks. For example, an email address might arrive as `jane@` followed by `example.com`. Redacting each chunk independently would miss it.

I used partial matching from the `regex` library to keep a small amount of unfinished text until it is clear whether it forms a sensitive value. Text that cannot be part of a match can be sent immediately. This avoids collecting the entire response and reduces unnecessary delay.

The current detectors cover email addresses, formatted US Social Security numbers, and card numbers of 13–19 digits. Card numbers are checked with the Luhn algorithm to reduce false positives. The SSE parser also handles events and UTF-8 characters split across network reads.

I included property-based tests using Hypothesis to compare chunk-by-chunk redaction with whole-text redaction across different input splits. I also added a small benchmark script to measure the processing overhead. These numbers are measurements from one Windows laptop, not guaranteed latency on other machines:

| **chunk size** | **added per chunk (p50 / p99)** | **held back (avg / max)** |
| -------------- | ------------------------------- | ------------------------- |
| 4 chars        | 11 µs / 52 µs                   | 3.3 / 24 chars            |
| 16 chars       | 73 µs / 242 µs                  | 3.3 / 24 chars            |
| 64 chars       | 200 µs / 437 µs                 | 3.3 / 24 chars            |

**Current scope:** The filter processes streamed `delta.content` text. It does not yet inspect tool-call arguments or request-side content. Those would need additional handling before using this as a general-purpose PII protection layer.

## Task 4: Rate limiter and model fallback

```bash
uv run python -m mocks.llm_provider --port 9101 --name primary
uv run python -m mocks.llm_provider --port 9102 --name secondary
uv run --env-file .env.example python -m model_router         # on :8100, SQLite in data/
```

I used an SQLite sliding-window log to track estimated token usage by tenant over the previous 60 seconds. Each request records its tenant, timestamp, and token count. Expired entries are removed as the window moves.

To avoid concurrent requests passing the limit at the same time, the router reserves an estimated token amount before contacting the model. It uses an SQLite `BEGIN IMMEDIATE` transaction to make the check and reservation together. When the provider supplies actual usage, the router reconciles the reservation with `usage.total_tokens`. If all attempts fail, it releases the reservation. Database operations run off the asyncio event loop.

The estimate is based on prompt length and the requested output budget, so it is not an exact tokenizer-based count. In particular, actual usage can differ from the estimate. This is an important limitation if the service is used to enforce strict billing limits.

The router tries the primary provider first. It switches to the secondary provider when the primary returns 429, encounters a connection error or server error, or exceeds the configured three-second timeout. Other upstream 4xx errors are returned as sanitized gateway errors rather than automatically retried.

| Primary provider result | Router behavior |
| --- | --- |
| 429, timeout, connection failure, or 5xx | Attempt the secondary provider |
| Other 4xx | Return a standardized `upstream_rejected` error |
| 429 with `Retry-After` | Temporarily avoid the primary for the indicated period |

For streaming responses, the timeout applies while waiting for the initial chunk. Once streaming has started, the router stays with that provider. The error response format is `{"error": {"type", "code", "message", "request_id"}}`; internal exceptions and provider details are kept out of the client response. Response headers include the request ID, provider, attempt count, and remaining estimated token allowance.

## Running the services outside the mock setup

The sample configuration is in [`.env.example`](.env.example). The Docker configuration is in [`docker-compose.yml`](docker-compose.yml). Services use environment variables for configuration, and missing required settings cause startup to fail.

Task 2 includes structured audit logs with request IDs, roles, tools, and authorization decisions. Those are useful when checking why a request was allowed or blocked.

There are a few differences between this assessment setup and a production deployment. The gateway currently uses static tokens rather than an identity provider. The SQLite limiter works for processes on the same host sharing the database file, but a multi-host deployment would need a shared store such as PostgreSQL or Redis. The mock providers are included for local testing and do not require real model credentials.

For troubleshooting, I start with the service logs and the request ID. MCP startup errors appear on stderr. Task 2 authorization failures can be traced in its audit log, and Task 4 logs provider failures and fallback decisions.

## Notes from the implementation

A few design choices came out of working through the tasks:

1. **Streaming redaction needs state.** Looking only at the current chunk is not enough when an email address or card number is split across messages. Keeping a small amount of pending text is a practical compromise between correctness and response latency.
2. **Rate limiting needs to account for concurrent requests.** Recording usage only after a model responds can allow several requests to pass the same available-budget check. Reserving an estimate before forwarding helps prevent that race, although the estimate can still differ from actual token usage.
3. **MCP error handling depends on the SDK path.** I used a lower-level handler for Task 1 because I needed invalid arguments to produce JSON-RPC errors instead of normal tool error results.
4. **A proxy should authorize the same request that it forwards.** Rejecting duplicate JSON keys avoids cases where the gateway and downstream parser disagree about the requested tool.

## Limitations and possible improvements

- Replace character-based token estimates with a tokenizer appropriate to the selected model.
- Extend Task 3 redaction to streamed tool-call arguments and request content.
- Consider additional PII detection methods for names, addresses, and less structured information. Regex matching alone cannot cover every sensitive value.
- Replace Task 2's `admin_` prefix rule with configurable tool-level permissions for real deployments.
- Improve token accounting for streamed responses when a provider does not return final usage data.

## Development and tests

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run pytest tests/task3 -q
```

The [CI workflow](.github/workflows/ci.yml) runs linting and tests on Ubuntu and Windows and builds the Docker image. I also documented the security assumptions and attack cases in [`docs/threat-model.md`](docs/threat-model.md).
