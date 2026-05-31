"""Unit tests for helpers.http_safety.get_capped_text.

Locks in the cap behaviour so chapter crawls can't be tricked into
unbounded reads. Uses a mocked Response with iter_content so we don't
need a network roundtrip.
"""

from unittest.mock import patch, MagicMock

from helpers.http_safety import get_capped_text


def _mock_response(chunks: list[bytes], status: int = 200, encoding: str = "utf-8"):
    resp = MagicMock()
    resp.status_code = status
    resp.encoding = encoding
    resp.apparent_encoding = encoding
    resp.iter_content = MagicMock(return_value=iter(chunks))
    resp.close = MagicMock()
    return resp


def test_returns_status_and_text_under_cap():
    chunks = [b"hello ", b"world"]
    with patch("helpers.http_safety.requests.get", return_value=_mock_response(chunks)):
        status, text = get_capped_text("http://example.com/x")
    assert status == 200
    assert text == "hello world"


def test_passes_non_200_status_through():
    with patch("helpers.http_safety.requests.get", return_value=_mock_response([b"err"], status=429)):
        status, text = get_capped_text("http://example.com/x")
    assert status == 429
    assert text == "err"


def test_caps_oversized_body(caplog):
    # 3 chunks of 8 bytes each = 24 bytes total; cap at 16 bytes → truncate.
    chunks = [b"a" * 8, b"b" * 8, b"c" * 8]
    with patch("helpers.http_safety.requests.get", return_value=_mock_response(chunks)):
        with caplog.at_level("WARNING"):
            status, text = get_capped_text(
                "http://example.com/big", max_bytes=16, warn_bytes=4,
            )
    assert status == 200
    # First two chunks (16 bytes) fit; third pushes over → break before append.
    assert text == "a" * 8 + "b" * 8
    # Warning emitted for both the warn threshold AND the truncation.
    assert any("exceeded 4 bytes" in r.getMessage() for r in caplog.records)
    assert any("truncated at 16 bytes" in r.getMessage() for r in caplog.records)


def test_warn_threshold_does_not_truncate(caplog):
    # 12 bytes total, warn at 4, cap at 100 → warns once but returns full body.
    chunks = [b"hello", b"world!", b"!"]
    with patch("helpers.http_safety.requests.get", return_value=_mock_response(chunks)):
        with caplog.at_level("WARNING"):
            status, text = get_capped_text(
                "http://example.com/medium", max_bytes=100, warn_bytes=4,
            )
    assert status == 200
    assert text == "helloworld!!"
    warn_msgs = [r.getMessage() for r in caplog.records if "exceeded" in r.getMessage()]
    assert len(warn_msgs) == 1


def test_close_called_on_exit():
    resp = _mock_response([b"x"])
    with patch("helpers.http_safety.requests.get", return_value=resp):
        get_capped_text("http://example.com/x")
    resp.close.assert_called_once()
