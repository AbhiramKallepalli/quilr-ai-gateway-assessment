import pytest
from pydantic import ValidationError

from customer_mcp.schemas import (
    GetCustomerRecordInput,
    TriggerRefundInput,
    describe_validation_error,
)

VALID_REFUND = {
    "customer_id": "CUST-10001",
    "amount": 25.5,
    "reason": "Duplicate charge on invoice INV-2291",
}


@pytest.mark.parametrize("customer_id", ["CUST-10001", "CUST-00000", "CUST-99999"])
def test_accepts_well_formed_customer_ids(customer_id):
    assert GetCustomerRecordInput.model_validate({"customer_id": customer_id})


@pytest.mark.parametrize(
    "customer_id",
    [
        "cust-10001",  # wrong case
        "CUST-1000",  # too short
        "CUST-100011",  # too long
        "CUST-1000A",  # non-digit
        " CUST-10001",  # leading space
        "CUST-10001 ",  # trailing space
        "CUST-10001\n",  # trailing newline
        "CUST_10001",  # wrong separator
        "CUST-١٠٠٠١",  # Arabic-Indic digits match \d but are not valid ids
        "",
        10001,  # wrong type
        None,
    ],
)
def test_rejects_malformed_customer_ids(customer_id):
    with pytest.raises(ValidationError):
        GetCustomerRecordInput.model_validate({"customer_id": customer_id})


def test_rejects_missing_and_unknown_fields():
    with pytest.raises(ValidationError):
        GetCustomerRecordInput.model_validate({})
    with pytest.raises(ValidationError):
        GetCustomerRecordInput.model_validate({"customer_id": "CUST-10001", "admin": True})


@pytest.mark.parametrize("amount", [0.01, 1, 99.99, 10_000])
def test_accepts_valid_amounts(amount):
    parsed = TriggerRefundInput.model_validate({**VALID_REFUND, "amount": amount})
    assert parsed.amount == float(amount)


@pytest.mark.parametrize(
    "amount",
    [0, -1, -0.01, True, False, "10.00", None, [10], float("nan"), float("inf"), 10.001, 10_000.01],
)
def test_rejects_invalid_amounts(amount):
    with pytest.raises(ValidationError):
        TriggerRefundInput.model_validate({**VALID_REFUND, "amount": amount})


@pytest.mark.parametrize(
    "reason",
    ["short", " " * 20, "   too short   ", "a" * 501, 1234567890123, None],
)
def test_rejects_invalid_reasons(reason):
    with pytest.raises(ValidationError):
        TriggerRefundInput.model_validate({**VALID_REFUND, "reason": reason})


def test_reason_is_stripped_and_exact_minimum_is_accepted():
    parsed = TriggerRefundInput.model_validate({**VALID_REFUND, "reason": "  0123456789  "})
    assert parsed.reason == "0123456789"


def test_error_details_do_not_echo_input():
    secret = "CUST-SECRET-4111111111111111"
    with pytest.raises(ValidationError) as exc:
        GetCustomerRecordInput.model_validate({"customer_id": secret})
    details = describe_validation_error(exc.value)
    assert details[0]["field"] == "customer_id"
    assert secret not in str(details)


def test_json_schema_is_strict():
    schema = TriggerRefundInput.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"customer_id", "amount", "reason"}
    assert schema["properties"]["customer_id"]["pattern"] == "^CUST-[0-9]{5}$"
