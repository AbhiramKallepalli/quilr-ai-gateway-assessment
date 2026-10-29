import pytest

from stream_guardrail.patterns import luhn_valid, redact_text


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Contact priya.raman@example.com today", "EMAIL"),
        ("mail a.b+tag@mail.co.uk now", "EMAIL"),
        ("SSN 123-45-6789.", "SSN"),
        ("SSN 123 45 6789.", "SSN"),
        ("card 4111 1111 1111 1111 exp 12/29", "CARD"),
        ("card 4111-1111-1111-1111", "CARD"),
        ("card 4111111111111111", "CARD"),
        ("amex 378282246310005", "CARD"),
    ],
)
def test_detects_sensitive_values(text, kind):
    redacted, counts = redact_text(text)
    assert "[REDACTED]" in redacted
    assert counts[kind] == 1


@pytest.mark.parametrize(
    "text",
    [
        "Order 4111111111111112 shipped",  # fails Luhn
        "SSN 000-12-3456 is never issued",
        "SSN 666-12-3456 is never issued",
        "SSN 912-34-5678 is never issued",
        "SSN 123-00-4567 is never issued",
        "mixed separators 123-45 6789",
        "call 555-123-4567",
        "the date 2026-10-29",
        "version 1.2.3 released",
        "user@localhost has no TLD",
        "just prose with no secrets at all",
    ],
)
def test_leaves_non_sensitive_text_alone(text):
    redacted, counts = redact_text(text)
    assert redacted == text
    assert not counts


def test_redacts_every_occurrence_and_keeps_surrounding_text():
    text = "a@b.io, 123-45-6789 and 4111 1111 1111 1111!"
    assert redact_text(text)[0] == "[REDACTED], [REDACTED] and [REDACTED]!"


def test_whole_dotted_domain_is_redacted():
    assert redact_text("write to j@acme.com.au.")[0] == "write to [REDACTED]."


def test_luhn():
    assert luhn_valid("4111111111111111")
    assert luhn_valid("378282246310005")
    assert not luhn_valid("4111111111111112")
