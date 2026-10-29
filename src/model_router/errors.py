"""The one error shape every gateway failure uses.

Upstream bodies, stack traces, provider URLs and exception text are logged
under the request id and never returned. Clients get a stable code they can
branch on, a short message and the request id for support.
"""

from __future__ import annotations

from starlette.responses import JSONResponse

from .router import AllProvidersFailed


def gateway_error(
    status: int,
    code: str,
    message: str,
    request_id: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error_type = (
        "rate_limit_error"
        if status == 429
        else ("invalid_request_error" if status < 500 else "gateway_error")
    )
    return JSONResponse(
        {"error": {"type": error_type, "code": code, "message": message, "request_id": request_id}},
        status_code=status,
        headers={"x-request-id": request_id, **(headers or {})},
    )


def upstream_error(failure: AllProvidersFailed, request_id: str) -> JSONResponse:
    last = failure.last
    headers = {"x-gateway-attempts": str(failure.attempts)}
    if last.kind == "rate_limited":
        return gateway_error(
            429,
            "upstream_rate_limited",
            "All model providers are rate limiting requests",
            request_id,
            {**headers, "retry-after": "1"},
        )
    if last.kind == "timeout":
        return gateway_error(
            504, "upstream_timeout", "No model provider responded in time", request_id, headers
        )
    if last.kind == "rejected":
        return gateway_error(
            400, "upstream_rejected", "The model provider rejected the request", request_id, headers
        )
    return gateway_error(
        502, "upstream_unavailable", "No model provider is available", request_id, headers
    )
