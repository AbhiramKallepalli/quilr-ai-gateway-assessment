# QuilrAI – AI Field Developer assessment

Four small services that sit between AI agents and the tools and models they call:

| Task | Package | What it is |
|---|---|---|
| 1 | [`src/customer_mcp`](src/customer_mcp) | stdio MCP server: `get_customer_record`, `trigger_refund`, strict validation, stdout kept pure |
| 2 | [`src/mcp_gateway`](src/mcp_gateway) | HTTP reverse proxy for MCP: Bearer auth, role-based `admin_*` tool blocking, audit log |
| 3 | [`src/stream_guardrail`](src/stream_guardrail) | OpenAI-compatible LLM gateway endpoint that redacts emails, SSNs and card numbers from a live stream |
| 4 | [`src/model_router`](src/model_router) | Token-aware sliding-window rate limiter on SQLite, primary → secondary failover on 429 / 3 s timeout |

Python 3.12, one `uv` project, 221 tests. Mock upstreams live in [`src/mocks`](src/mocks), so nothing needs an API key.

## Quick start

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/); Docker is optional.

```bash
uv sync                           # install (uv downloads Python 3.12 if needed)
uv run pytest -q                  # 221 tests, ~40 s (one test really waits out the 3 s timeout)
uv run python scripts/demo.py     # walks through all four tasks in one process
docker compose up --build         # gateways on :8080 (Task 2), :8090 (Task 3), :8100 (Task 4)
```

With the compose stack running (bash/zsh; on Windows use Git Bash or WSL):

```bash
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

`mock_*` fields are instructions for the mock providers only. Every environment variable is listed in [`.env.example`](.env.example), which also works as-is for running a service locally: `uv run --env-file .env.example python -m <service>`.

## Evaluation criteria and where they are proven

| Criterion | How | Evidence |
|---|---|---|
| **T1** stdout is pure JSON-RPC | fd 1 is duplicated for the transport, then pointed at stderr, so even a stray `print()` or C extension can't reach the protocol stream. Logs are JSON on stderr | `tests/task1/test_stdio_wire.py::test_stdout_carries_only_jsonrpc_messages` runs the real process with deliberate `print()`s and parses every stdout line |
| **T1** protocol compliance | schema violations and unknown tools → `-32602`; business failures → `isError: true` result; unexpected errors → `-32603` with no internals | `test_stdio_wire.py`, `test_official_client.py` (official MCP client) |
| **T1** validation edge cases | Pydantic strict models, `extra="forbid"`, `CUST-[0-9]{5}` (not `\d`, which matches Arabic-Indic digits), bools rejected as amounts, NaN/inf, > 2 decimals, reason checked after stripping | `tests/task1/test_schemas.py` (41 cases) |
| **T2** JSON-RPC wire parsing | strict parser: `-32700` / `-32600` / `-32602`, ids limited to string/integer, notifications get no body, batches handled, **duplicate keys rejected** | `tests/task2/test_wire_format.py`, `test_batches.py` |
| **T2** proxy construction | original bytes forwarded; JSON and SSE responses streamed back; `Mcp-Session-Id` round-trips; client `Authorization`/cookies never forwarded; spoofed `X-Gateway-Role` replaced | `tests/task2/test_proxying.py` |
| **T2** method-level authorization | only `tools/call` is gated; `admin_*` needs `admin` after NFKC + casefold; control/zero-width characters rejected; denied calls never reach downstream | `tests/task2/test_authorization.py`, `test_official_client.py` |
| **T3** stream chunking and buffer state | incremental SSE parser (events and UTF-8 split anywhere), one redactor per choice, held text released on next chunk / `finish_reason` / `[DONE]` | `tests/task3/test_sse.py`, `test_pipeline.py` |
| **T3** regex over partial text | holds back only text that could still become a match; property test: any text, any chunking, output = whole-text redaction | `tests/task3/test_stream_redactor.py` (Hypothesis) |
| **T3** memory and latency | held text bounded by the longest possible match; ~11 µs per 4-char chunk, 3.3 chars held on average | `test_memory_stays_bounded_on_a_long_digit_run`, `test_output_is_streamed_not_buffered`, `scripts/bench_guardrail.py` |
| **T4** async concurrency and timeout races | `asyncio.timeout` per attempt; the timed-out request is cancelled, not left running | `tests/task4/test_failover.py` (incl. the real 3000 ms default) |
| **T4** limiter eviction and token tracking | sliding log in SQLite, `BEGIN IMMEDIATE` reserve → reconcile with real usage, exact `Retry-After` | `tests/task4/test_limiter.py` (50 concurrent → exactly 25 granted; two OS processes → exactly 25 between them; survives restart) |
| **T4** fallback and error sanitization | 429 / timeout / 5xx / connection error → secondary; other 4xx not retried; one error shape everywhere | `tests/task4/test_gateway.py` asserts no stack trace, upstream body, URL or key in any error |

---

## Task 1: MCP server

```bash
uv run python -m customer_mcp              # speaks JSON-RPC on stdin/stdout, logs on stderr
docker compose run --rm -T customer-mcp    # same thing from the container image
```

It isn't started by `docker compose up` because a stdio server has no port: the MCP client launches the process and owns its stdin/stdout. `CUSTOMER_MCP_LOG_LEVEL` sets the log level.

To use it from Claude Desktop or another MCP client:

```json
{ "mcpServers": { "customer-records": {
    "command": "uv",
    "args": ["--directory", "C:/path/to/quilr-ai-gateway-assessment", "run", "python", "-m", "customer_mcp"]
} } }
```

**Two error channels.** If the caller breaks the contract (bad `customer_id`, `amount: "10"`, an unknown tool), that's a JSON-RPC `-32602` with field-level details in `error.data`, without echoing the rejected value back. If the call is valid but fails for a business reason (customer not found, refund larger than the refundable balance, suspended account), it's a normal result with `isError: true`, because that's information the model should read and act on, not a transport failure.

**Why not `@server.call_tool()`.** The SDK decorator turns every exception into an `isError` result, which would hide schema violations from the client. The handler is registered on the low-level server directly so they surface as real JSON-RPC errors.

**Idempotent refunds.** `trigger_refund` takes an optional `idempotency_key`. Retrying with the same key returns the original refund instead of issuing a second one; reusing a key for a different refund is rejected. Agents retry, so a money-moving tool should be safe to retry. The tools also carry MCP annotations (`readOnlyHint`, `idempotentHint`) for clients and gateways that act on them.

Assumption: "CUST-XXXXX" means five ASCII digits.

## Task 2: MCP security gateway

```bash
uv run python -m mocks.mcp_downstream --port 9000          # the downstream it proxies to
uv run --env-file .env.example python -m mcp_gateway       # gateway on :8080
```

Request path: authenticate → parse strictly → authorize every message → forward the original bytes or answer locally → one audit event per decision.

- **Deny by default at every step.** Missing or unknown token: HTTP 401 with `WWW-Authenticate`. An unknown role in the token file stops the gateway from starting, rather than failing open later.
- **Decide on exactly what will run.** The gateway authorizes the parsed body and forwards the raw bytes, so the parse must be unambiguous. `{"name":"get_customer_record","name":"admin_reset_key"}` is a parser-differential bypass (Python keeps the last key, some parsers keep the first), so duplicate keys are a parse error.
- **Prefix checks that hold up.** Names are NFKC-normalized and casefolded before the `admin_` check (`ADMIN_x`, full-width `ａｄｍｉｎ_x`); names with control or zero-width characters are rejected outright.
- **Batches are all-or-nothing.** One unauthorized call in a batch rejects the whole batch: the offending entry gets `-32001`, the rest `-32004`, nothing is forwarded. Partially running a batch the client sent as a unit is worse than refusing it. (MCP 2025-06-18 removed batching; a strict mode could reject arrays outright.)
- **`-32001` comes back with HTTP 200.** MCP clients treat non-2xx as a transport error, so a 403 would hide the JSON-RPC error from the agent. Malformed bodies get 400, like the official server.
- **Identity downstream.** The client's `Authorization` header is never forwarded. The gateway sends its own `X-Gateway-Subject` / `X-Gateway-Role` and drops any client-supplied copies.
- **Optional discovery filtering.** `GATEWAY_HIDE_UNAUTHORIZED_TOOLS=1` removes `admin_*` tools from `tools/list` for viewers. Off by default because the task asks for transparent forwarding.

## Task 3: Streaming PII guardrail

```bash
uv run python -m mocks.llm_provider --port 9100               # mock OpenAI-compatible provider
uv run --env-file .env.example python -m stream_guardrail     # base_url http://localhost:8090/v1
uv run python scripts/bench_guardrail.py
```

`POST /v1/chat/completions` is OpenAI-compatible, so an application only changes its `base_url`. Point `GUARDRAIL_UPSTREAM_URL` at a real provider to use it for real.

**Hold back only what could still be sensitive.** A fixed look-back window delays every chunk, including plain prose. This redactor uses the `regex` module's partial matching to find the earliest position from which the end of the buffer could still become (or grow into) a match, e.g. `jane@exa` or `4111 1111`, and emits everything before it immediately. In practice only the trailing word is held. A complete match is emitted only once it can no longer grow (`john@acme.com.` might still become `john@acme.com.au`).

**Correctness is property-tested.** For any text and any way of cutting it into chunks, the joined output equals redacting the whole text at once (`test_any_chunking_matches_whole_text_redaction`). That test found an edge case during development: `regex` prefers a later complete match over an earlier partial one, which would have let a split value through. The hold point is now computed explicitly.

**Bounded memory.** Every pattern has a maximum length, so the redactor never holds more than ~345 characters per stream regardless of response length. Nothing accumulates the full response.

**Detectors.** Email; SSN with consistent `-` or space separators, excluding never-issued ranges (000, 666, 9xx, group 00, serial 0000); card numbers of 13–19 digits with optional separators, **Luhn-checked** so order numbers and timestamps aren't redacted.

**Stream handling.** The SSE parser copes with events and multi-byte characters split across reads. Chunks whose content is entirely held are skipped rather than sent empty. Upstream events that aren't JSON are dropped rather than forwarded unscanned. Client disconnects close the upstream request.

Measured on a Windows laptop (1 MB of prose with PII, `scripts/bench_guardrail.py`):

| chunk size | added per chunk (p50 / p99) | held back (avg / max) |
|---|---|---|
| 4 chars | 11 µs / 52 µs | 3.3 / 24 chars |
| 16 chars | 73 µs / 242 µs | 3.3 / 24 chars |
| 64 chars | 200 µs / 437 µs | 3.3 / 24 chars |

## Task 4: Rate limiter and fallback router

```bash
uv run python -m mocks.llm_provider --port 9101 --name primary
uv run python -m mocks.llm_provider --port 9102 --name secondary
uv run --env-file .env.example python -m model_router         # on :8100, SQLite in data/
```

**Sliding log, not buckets.** Every request is a row `(tenant, ts_ms, tokens)`. Usage is the sum over the last 60 s, and expired rows are deleted on every check. Fixed one-minute buckets would let a tenant send 2× the limit across a boundary.

**Reserve, then reconcile.** Checking the limit and recording usage after the response lets concurrent requests all pass the same check. Instead, before calling a model, the router reserves the *estimated* tokens (prompt ≈ chars/4 + `max_tokens`) in one `BEGIN IMMEDIATE` transaction. That takes SQLite's write lock before reading, so it also holds across several gateway processes sharing the file. After the response, the reservation is replaced by the provider's `usage.total_tokens`. If every provider fails, it is released and the tenant isn't charged. `Retry-After` is the exact time until enough old usage leaves the window.

**SQLite off the event loop.** All database work runs on one dedicated thread, so the loop never blocks on disk I/O and the connection is only used from one thread. WAL mode and `busy_timeout`. Tenant API keys are only held as SHA-256 hashes; the database stores tenant ids.

**Failover.** Each attempt runs under `asyncio.timeout(3.0)`. The timeout and the response can't both win: either the block finishes or it is cancelled, and cancelling also cancels the HTTP request so the slow primary isn't left running (`test_timed_out_primary_is_cancelled_not_left_running`).

| Upstream result | Action |
|---|---|
| 429, timeout, connection error, 5xx | try the secondary |
| other 4xx | return `400 upstream_rejected`; a bad request fails everywhere |
| 429 with `Retry-After` | also skip the primary for that long, so later requests don't each wait out a timeout |

For streams the timeout covers the time to the first chunk; after that the stream is committed to that provider.

**One error shape.** `{"error": {"type", "code", "message", "request_id"}}` with `x-request-id`; details go to the log under the same id. Responses also carry `x-gateway-provider`, `x-gateway-attempts` and `x-ratelimit-remaining-tokens`.

---

## Deploying at a customer

- **Configuration** is environment-only ([`.env.example`](.env.example)); a missing required setting stops the service at startup with the variable's name. Token files can be mounted as secrets (`GATEWAY_TOKENS_FILE`). One container image runs every service ([`docker-compose.yml`](docker-compose.yml)), as a non-root user, including the stdio MCP server (`docker compose run --rm -T customer-mcp`).
- **Logs** are one JSON object per line; the Task 2 audit events (`decision`, `auth_failed`, `forwarded`, `downstream_error`) carry `request_id`, subject, role, tool and reason, which is what a SIEM rule needs.
- **Identity.** Static tokens stand in for the customer's IdP. Swapping `authenticate()` for JWT/JWKS validation (issuer, audience, a `role` or group claim) is a contained change.
- **Scaling Task 4.** SQLite with `BEGIN IMMEDIATE` is correct for one node, or several processes on one host (tested). Several hosts need a shared store: Redis with a Lua script doing the same evict–sum–insert atomically, or Postgres.
- **Troubleshooting.**
  - MCP client sees "connection closed" → run `python -m customer_mcp` by hand; an import or config error is printed on stderr.
  - Agent sees `-32001` → find its `request_id` in the gateway audit log.
  - Tenant gets 429 → `x-ratelimit-remaining-tokens` and `Retry-After` show why.
  - 504 / 502 → `model_router` logs each failed attempt with provider and reason.

## Findings worth sharing with the product team

1. **Response scanning on streams.** QuilrAI's LLM Gateway docs say response-side scanning is skipped for SSE streams and chunks pass through unchanged. Task 3 shows one way to close that gap without buffering the response: hold back only what could still be PII, keep memory bounded, and prove equivalence with a property test. It holds back less than a fixed look-back window (the LiteLLM plugin's `incremental_diff` mode holds back `stream_holdback_chars`, 64 by default).
2. **Counting tokens at completion can overshoot.** The budgets section of the rate, token and budget limits docs notes that tokens are counted when a request completes, so one in-flight request can carry a counter past its limit. Reserving the estimate up front and reconciling afterwards keeps the limit exact under concurrency.
3. **The MCP Python SDK returns `-32602` for unknown methods.** It validates each request against its known request types before dispatch, so `tools/explode` fails validation instead of returning JSON-RPC's `-32601`. Worth knowing when mapping errors in a gateway.
4. **Parser differentials in MCP gateways.** Any gateway that authorizes on a parsed body and forwards raw bytes should reject duplicate JSON keys, or re-serialize what it inspected.

## Limitations and next steps

- Token estimation is a character heuristic; a tokenizer per model (e.g. tiktoken) would make reservations tighter.
- Task 3 redacts `delta.content` only; tool-call arguments in the stream and the request side could go through the same redactor.
- Regexes find well-formed identifiers. Names, addresses and free-form PHI need a model-based detector, which Quilr's platform already has; this layer is the cheap, deterministic first pass.
- The Task 2 policy is a prefix rule. A real deployment would load per-tool policy (tags, annotations such as `destructiveHint`, per-group overrides).
- Streams in Task 4 are charged at the reserved estimate because providers usually omit usage unless `stream_options.include_usage` is set.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/task3 -q      # one task at a time
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs lint and the full suite on Ubuntu and Windows and builds the Docker image. See [`docs/threat-model.md`](docs/threat-model.md) for the attacks each control is meant to stop.
