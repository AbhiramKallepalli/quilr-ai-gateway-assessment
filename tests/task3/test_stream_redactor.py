"""The streaming redactor must produce exactly what whole-text redaction produces,
however the text is cut into chunks, while holding back as little as possible."""

from hypothesis import given, settings
from hypothesis import strategies as st

from stream_guardrail.patterns import MAX_MATCH_LENGTH, redact_text
from stream_guardrail.redactor import StreamRedactor

FRAGMENTS = st.sampled_from(
    [
        "john@acme.com",
        "a.b+c@mail.co.uk",
        "x@y.io",
        "123-45-6789",
        "123 45 6789",
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "4111111111111111",
        "4111111111111112",
        "000-12-3456",
        " ",
        ".",
        "-",
        "@",
        ",",
        "\n",
        "x",
        "7",
        "42",
        "com",
        "au",
        "hello",
        "card ",
        "SSN: ",
        "é",
        "🙂",
    ]
)


def stream(text: str, cuts: list[int]) -> tuple[list[str], int]:
    redactor = StreamRedactor()
    out, prev, peak = [], 0, 0
    for cut in [*sorted(cuts), len(text)]:
        out.append(redactor.feed(text[prev:cut]))
        peak = max(peak, redactor.pending)
        prev = cut
    out.append(redactor.flush())
    return out, peak


@settings(max_examples=1500, deadline=None)
@given(st.lists(FRAGMENTS, max_size=30), st.data())
def test_any_chunking_matches_whole_text_redaction(parts, data):
    text = "".join(parts)
    cuts = data.draw(st.lists(st.integers(0, len(text)), max_size=15))
    pieces, peak = stream(text, cuts)
    assert "".join(pieces) == redact_text(text)[0]
    assert peak <= MAX_MATCH_LENGTH + 1


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=200), st.data())
def test_arbitrary_text_matches_whole_text_redaction(text, data):
    cuts = data.draw(st.lists(st.integers(0, len(text)), max_size=10))
    assert "".join(stream(text, cuts)[0]) == redact_text(text)[0]


def test_one_character_at_a_time():
    text = "my card is 4111 1111 1111 1111 and email jane@example.org."
    pieces, _ = stream(text, list(range(len(text))))
    assert "".join(pieces) == "my card is [REDACTED] and email [REDACTED]."
    assert all("4111" not in p and "jane@" not in p for p in pieces)


def test_plain_prose_is_released_immediately():
    redactor = StreamRedactor()
    assert redactor.feed("Hello there, how are you today? ") == "Hello there, how are you today? "
    assert redactor.pending == 0


def test_only_the_possible_match_is_held():
    redactor = StreamRedactor()
    assert redactor.feed("Reach me at jane@exa") == "Reach me at "
    # "thanks" could still be the start of another address, so it waits too.
    assert redactor.feed("mple.com, thanks") == "[REDACTED], "
    assert redactor.feed("!") == "thanks!"


def test_pending_tail_is_released_when_it_turns_out_harmless():
    redactor = StreamRedactor()
    assert redactor.feed("room 4111") == "room "
    assert redactor.feed(" on floor 2") == "4111 on floor "
    assert redactor.flush() == "2"


def test_memory_stays_bounded_on_a_long_digit_run():
    redactor = StreamRedactor()
    for _ in range(10_000):
        redactor.feed("1")
        assert redactor.pending <= MAX_MATCH_LENGTH + 1


def test_counts_by_type():
    redactor = StreamRedactor()
    redactor.feed("a@b.io 123-45-6789 ")
    redactor.feed("4111111111111111 ")
    redactor.flush()
    assert dict(redactor.counts) == {"EMAIL": 1, "SSN": 1, "CARD": 1}
