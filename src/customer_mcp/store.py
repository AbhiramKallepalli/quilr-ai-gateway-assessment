"""In-memory customer data, standing in for a real CRM / billing backend."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


class StoreError(Exception):
    """A business-rule failure. Reported to the model as a tool result, not a protocol error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    tier: str
    status: str
    refundable_balance: Decimal


@dataclass(frozen=True)
class Refund:
    refund_id: str
    customer_id: str
    amount: Decimal
    reason: str
    created_at: str


class CustomerStore:
    def __init__(self, customers: list[Customer]) -> None:
        self._customers = {c.customer_id: c for c in customers}
        self._refunds_by_key: dict[str, tuple[tuple[str, Decimal], Refund]] = {}
        self._lock = threading.Lock()

    @classmethod
    def with_sample_data(cls) -> CustomerStore:
        return cls(
            [
                Customer("CUST-10001", "Priya Raman", "priya.raman@example.com",
                         "enterprise", "active", Decimal("250.00")),
                Customer("CUST-10002", "Marcus Lee", "marcus.lee@example.com",
                         "standard", "active", Decimal("40.00")),
                Customer("CUST-10003", "Elena Petrova", "elena.petrova@example.com",
                         "standard", "suspended", Decimal("100.00")),
            ]
        )

    def _require(self, customer_id: str) -> Customer:
        customer = self._customers.get(customer_id)
        if customer is None:
            raise StoreError("customer_not_found", f"No customer with id {customer_id}")
        return customer

    def get_record(self, customer_id: str) -> dict[str, object]:
        c = self._require(customer_id)
        return {
            "customer_id": c.customer_id,
            "name": c.name,
            "email": c.email,
            "tier": c.tier,
            "status": c.status,
            "refundable_balance": float(c.refundable_balance),
        }

    def refund(
        self, customer_id: str, amount: float, reason: str, idempotency_key: str | None = None
    ) -> dict[str, object]:
        value = Decimal(str(amount)).quantize(Decimal("0.01"))
        with self._lock:
            if idempotency_key is not None and idempotency_key in self._refunds_by_key:
                fingerprint, previous = self._refunds_by_key[idempotency_key]
                if fingerprint != (customer_id, value):
                    raise StoreError(
                        "idempotency_key_conflict",
                        "idempotency_key was already used for a different refund",
                    )
                return self._payload(previous, replayed=True)

            customer = self._require(customer_id)
            if customer.status != "active":
                raise StoreError(
                    "account_not_refundable", f"Account {customer_id} is {customer.status}"
                )
            if value > customer.refundable_balance:
                raise StoreError(
                    "exceeds_refundable_balance",
                    f"Requested {value} but only {customer.refundable_balance} is refundable",
                )

            customer.refundable_balance -= value
            refund = Refund(
                refund_id=f"RF-{uuid.uuid4().hex[:12].upper()}",
                customer_id=customer_id,
                amount=value,
                reason=reason,
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
            if idempotency_key is not None:
                self._refunds_by_key[idempotency_key] = ((customer_id, value), refund)
            return self._payload(refund, replayed=False)

    def _payload(self, refund: Refund, *, replayed: bool) -> dict[str, object]:
        return {
            "refund_id": refund.refund_id,
            "customer_id": refund.customer_id,
            "amount": float(refund.amount),
            "reason": refund.reason,
            "status": "processed",
            "created_at": refund.created_at,
            "remaining_refundable_balance": float(
                self._customers[refund.customer_id].refundable_balance
            ),
            "idempotent_replay": replayed,
        }