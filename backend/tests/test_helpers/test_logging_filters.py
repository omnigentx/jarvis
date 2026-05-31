"""Unit tests for helpers.logging_filters.

Locks in the redaction behaviour so a future contributor changing the
patterns doesn't silently re-open a key-leak path. Covers the formats we
actually see in this codebase (env-style, JSON-style, Bearer headers).
"""

import logging

import pytest

from helpers.logging_filters import RedactingFormatter, RedactSecretsFilter, redact_secrets


# ---------- redact_secrets ----------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # Env-style
        ("API_KEY=abc123",         "API_KEY=***"),
        ("api-key=xyz_789",        "api-key=***"),
        # JSON-style (single + double quotes)
        ('{"api_key": "abc123"}',  '{"api_key": "***"}'),
        ("{'api_key': 'abc123'}",  "{'api_key': '***'}"),
        # YAML-style
        ("api_key: abc123",        "api_key: ***"),
        # Bearer header
        ("Authorization: Bearer eyJhbGc.payload.sig",
                                   "Authorization: ***"),
        ("authorization=Bearer xyz",
                                   "authorization=***"),
        # Mixed key/value lines (only the value is redacted)
        ("user=alice password=p@ss",
                                   "user=alice password=***"),
        # Tokens / secrets
        ("token: foo.bar.baz",     "token: ***"),
        ("secret_key: shhh",       "secret_key: ***"),
        # Multiple keys in one line — each value redacted independently
        ('api_key="A" token="B"',  'api_key="***" token="***"'),
    ],
)
def test_redacts(raw, expected):
    assert redact_secrets(raw) == expected


def test_non_secret_text_unchanged():
    # Common false-positive shapes must NOT be mangled.
    samples = [
        "User signed in successfully",
        "GET /api/status 200",
        "Loaded 42 records",
    ]
    for s in samples:
        assert redact_secrets(s) == s


def test_idempotent():
    once = redact_secrets("api_key=abc")
    twice = redact_secrets(once)
    assert once == twice == "api_key=***"


def test_empty_input_safe():
    assert redact_secrets("") == ""
    assert redact_secrets(None) is None  # type: ignore[arg-type]


# ---------- RedactSecretsFilter ----------

def test_filter_redacts_message():
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="config={'api_key': 'abc123'}", args=None, exc_info=None,
    )
    RedactSecretsFilter().filter(rec)
    assert rec.getMessage() == "config={'api_key': '***'}"


def test_filter_handles_percent_args():
    # logger.info("config=%s", {"api_key": "abc"}) shape — args is a tuple.
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="config=%s", args=({"api_key": "abc"},), exc_info=None,
    )
    RedactSecretsFilter().filter(rec)
    # Args cleared after redaction; the final message text was already merged
    # and scrubbed.
    assert rec.args is None
    assert rec.getMessage() == "config={'api_key': '***'}"


def test_filter_never_raises():
    # Pathological record that would normally raise on getMessage().
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="%s %s", args=("only-one-arg",), exc_info=None,
    )
    # Must not raise — record passes through.
    assert RedactSecretsFilter().filter(rec) is True


# ---------- RedactingFormatter (exc_info traceback scrub) ----------

def test_formatter_scrubs_exception_message():
    fmt = RedactingFormatter("%(message)s")
    try:
        # Stuff a secret into the exception message — simulates a third-
        # party lib that prints the full request body on error.
        raise RuntimeError("call failed: api_key=verysecret_abc123")
    except RuntimeError:
        import sys
        rec = logging.LogRecord(
            name="t", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="upstream failed", args=None, exc_info=sys.exc_info(),
        )
    rendered = fmt.format(rec)
    # The traceback text appended by formatException contains the
    # exception message — that's where the secret lives. Must be scrubbed.
    assert "verysecret_abc123" not in rendered
    assert "api_key=***" in rendered


def test_formatter_scrubs_record_message():
    fmt = RedactingFormatter("%(message)s")
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg='request headers: {"Authorization": "Bearer eyJxxx"}',
        args=None, exc_info=None,
    )
    rendered = fmt.format(rec)
    assert "eyJxxx" not in rendered
    assert "***" in rendered


def test_formatter_passes_clean_log_unchanged():
    fmt = RedactingFormatter("%(message)s")
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="user signed in", args=None, exc_info=None,
    )
    assert fmt.format(rec) == "user signed in"
