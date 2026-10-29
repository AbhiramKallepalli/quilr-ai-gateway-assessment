from hypothesis import given
from hypothesis import strategies as st

from stream_guardrail.sse import SSEEvent, SSEParser

RAW = (
    b": keep-alive\n\n"
    b'data: {"a": 1}\n\n'
    b"event: message\r\ndata: line one\r\ndata: line two\r\n\r\n"
    b"data: caf\xc3\xa9 \xf0\x9f\x99\x82\n\n"
    b"data: [DONE]\n\n"
)
EXPECTED = [
    SSEEvent('{"a": 1}'),
    SSEEvent("line one\nline two", event="message"),
    SSEEvent("café 🙂"),
    SSEEvent("[DONE]"),
]


def parse(chunks: list[bytes]) -> list[SSEEvent]:
    parser = SSEParser()
    events = [e for c in chunks for e in parser.feed(c)]
    return events + parser.close()


def test_parses_a_complete_stream():
    assert parse([RAW]) == EXPECTED


def test_byte_at_a_time_including_inside_utf8_characters():
    assert parse([RAW[i : i + 1] for i in range(len(RAW))]) == EXPECTED


@given(st.lists(st.integers(0, len(RAW)), max_size=20))
def test_any_split(cuts):
    points = [0, *sorted(cuts), len(RAW)]
    assert parse([RAW[a:b] for a, b in zip(points, points[1:], strict=False)]) == EXPECTED


def test_unterminated_final_event_is_returned_on_close():
    assert parse([b"data: tail"]) == [SSEEvent("tail")]


def test_encode_round_trip():
    event = SSEEvent("a\nb", event="message")
    assert parse([event.encode()]) == [event]
