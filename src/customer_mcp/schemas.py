"""Input schemas for the customer tools.

Validation is strict on purpose: the caller is an LLM, and a model that sends
"100" for an amount or "cust-1" for an id should get a precise error back
instead of having the server guess what it meant.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# [0-9] rather than \d: \d also matches non-ASCII digits such as "١٠٠٠١".
CUSTOMER_ID_PATTERN = r"^CUST-[0-9]{5}$"
MAX_REFUND_AMOUNT = 10_000.00
REASON_MIN_LENGTH = 10
REASON_MAX_LENGTH = 500


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class GetCustomerRecordInput(_StrictInput):
    customer_id: str = Field(
        pattern=CUSTOMER_ID_PATTERN,
        description="Customer identifier in the format CUST-XXXXX (five digits).",
        examples=["CUST-10001"],
    )


class TriggerRefundInput(_StrictInput):
    customer_id: str = Field(
        pattern=CUSTOMER_ID_PATTERN,
        description="Customer identifier in the format CUST-XXXXX (five digits).",
        examples=["CUST-10001"],
    )
    amount: float = Field(
        gt=0,
        le=MAX_REFUND_AMOUNT,
        description=f"Refund amount, greater than 0 and at most {MAX_REFUND_AMOUNT:.2f}, "
        "with at most two decimal places.",
        examples=[25.50],
    )
    reason: str = Field(
        min_length=REASON_MIN_LENGTH,
        max_length=REASON_MAX_LENGTH,
        description=f"Why the refund is issued, at least {REASON_MIN_LENGTH} characters.",
        examples=["Customer was charged twice for invoice INV-2291"],
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Optional. Retrying with the same key returns the original refund "
        "instead of issuing a second one.",
    )

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_must_be_a_number(cls, value: Any) -> Any:
        # bool is a subclass of int, so True would otherwise become a 1.00 refund.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("amount must be a JSON number")
        return float(value)

    @field_validator("amount")
    @classmethod
    def _amount_must_be_currency(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("amount must be a finite number")
        exponent = Decimal(str(value)).as_tuple().exponent
        if isinstance(exponent, int) and exponent < -2:
            raise ValueError("amount supports at most two decimal places")
        return value

    @field_validator("reason", mode="before")
    @classmethod
    def _strip_reason(cls, value: Any) -> Any:
        # Length is checked after stripping, so ten spaces is not a reason.
        return value.strip() if isinstance(value, str) else value


def describe_validation_error(exc: ValidationError) -> list[dict[str, str]]:
    """Field-level errors for the client, without echoing the rejected values back."""
    details = []
    for err in exc.errors(include_url=False):
        field = ".".join(str(part) for part in err["loc"]) or "arguments"
        details.append({"field": field, "type": err["type"], "message": err["msg"]})
    return details