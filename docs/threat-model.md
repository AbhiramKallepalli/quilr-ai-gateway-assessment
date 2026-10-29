# Threat model

What each control in this repository is meant to stop, mapped to the
[OWASP Top 10 for LLM Applications (2025)](https://genai.owasp.org/llm-top-10/) where one applies.
"Test" names the test that exercises the control.

## Task 1: MCP server

| Threat | Control | Test |
|---|---|---|
| Model sends malformed or hostile arguments to a money-moving tool (LLM06 Excessive Agency) | strict Pydantic schemas, no coercion, `extra="forbid"`, amount bounds and precision | `tests/task1/test_schemas.py` |
| Look-alike identifiers (`CUST-١٠٠٠١`) passing a `\d` check | ASCII-only `[0-9]` pattern | `test_rejects_malformed_customer_ids` |
| Agent retries cause duplicate refunds | `idempotency_key` with conflict detection | `test_refund_with_idempotency_key_is_applied_once` |
| Protocol stream corrupted by stray output, causing client desync | fd-level stdout reservation | `test_stdout_carries_only_jsonrpc_messages` |
| Rejected input reflected back (data leakage) | error details list the field and rule, not the value | `test_error_details_do_not_echo_input` |

## Task 2: MCP gateway

| Threat | Control | Test |
|---|---|---|
| Unauthenticated access to tools | Bearer token required on every method, including GET/DELETE | `test_authentication.py` |
| Privilege escalation via admin tools (LLM06) | `admin_*` requires the admin role; decided before forwarding | `test_viewer_calling_admin_tool_is_blocked_before_downstream` |
| Prefix-check evasion with case, padding or Unicode look-alikes | NFKC + casefold + strip before matching | `test_admin_prefix_variants_are_still_blocked` |
| Evasion with zero-width / control characters | names containing Cc/Cf characters rejected | `test_tool_names_with_hidden_characters_are_rejected` |
| Smuggling an admin call inside a batch | every element authorized; any denial rejects the batch | `test_admin_call_hidden_in_a_batch_blocks_the_whole_batch` |
| Parser differential (duplicate keys) | duplicate keys are a parse error | `test_duplicate_keys_are_rejected_to_prevent_parser_differentials` |
| Identity spoofing to the downstream server | client identity headers dropped, gateway sets its own | `test_client_credentials_and_spoofed_identity_never_reach_downstream` |
| Credential forwarding to an untrusted downstream | client `Authorization` and cookies never forwarded | same |
| Resource exhaustion with huge bodies | body size limit, checked while streaming the body | `test_oversized_body_is_rejected` |
| Internal topology leaked in errors | downstream failures return a fixed message | `test_downstream_outage_returns_a_clean_error` |

## Task 3: Streaming guardrail

| Threat | Control | Test |
|---|---|---|
| Model output leaks PII to the user (LLM02 Sensitive Information Disclosure) | email / SSN / card redaction on the response stream | `test_gateway_endpoint.py` |
| PII split across chunks to slip past a per-chunk check | partial-match holdback, property-tested over arbitrary chunking | `test_any_chunking_matches_whole_text_redaction` |
| Memory exhaustion through a long unterminated candidate | bounded patterns cap held text | `test_memory_stays_bounded_on_a_long_digit_run` |
| Unscannable upstream events passed through | non-JSON SSE events dropped | `test_non_json_events_are_dropped` |
| Provider credentials taken from or leaked to clients | provider key comes from gateway config only | `test_provider_key_is_sent_upstream_and_client_key_is_not` |

## Task 4: Router

| Threat | Control | Test |
|---|---|---|
| Tenant exhausts shared model capacity or budget (LLM10 Unbounded Consumption) | per-tenant sliding-window token limit, checked before any provider call | `test_tenant_over_limit_gets_429_with_retry_after` |
| Concurrent requests racing past the limit | atomic reserve under `BEGIN IMMEDIATE`, across processes | `test_concurrent_reservations_never_overshoot`, `test_separate_processes_share_one_limit` |
| Stack traces, upstream bodies or keys in errors | single sanitized error shape, details logged by request id | `test_gateway.py::assert_standard_error` |
| Raw tenant API keys at rest | keys stored as SHA-256 digests in memory; the database stores tenant ids only | `model_router/app.py` |
