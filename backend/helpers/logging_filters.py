"""Logging filters — defensive scrub for secret-looking values.

Attached to every handler by ``core.logging_config.setup_logging`` so that
any log line touching the root logger (or any of the named child loggers
configured there) has secret-like substrings replaced with ``***``.

This is defense-in-depth: the codebase tries not to log secrets in the
first place, but a future contributor adding ``logger.debug("config=%s",
config)`` where ``config`` contains an api_key would otherwise leak it
to ``logs/jarvis.log`` (and any future APM that ships log lines off-box).
"""

from __future__ import annotations

import logging
import re

# Keywords whose value should be redacted in log lines.
_SECRET_KEYS = (
    "api_key", "api-key", "apikey",
    "access_token", "access-token",
    "auth_token", "auth-token",
    "secret_key", "secret-key",
    "password", "passwd",
    "secret",
    "token",
)

# "Authorization" gets its own pattern: the value may be multi-word
# ("Bearer <jwt>", "Basic <b64>") which the generic KV value class would
# truncate at the first whitespace.
_AUTHZ_RE = re.compile(
    r"(?i)(\bauthorization\s*[\'\"]?\s*[:=]\s*[\'\"]?)"
    r"([^\'\",}\])]+)"
)

# Generic key=value / "key": "value" for keywords whose value is a single
# token. Stops at whitespace / common JSON-dict closers / quotes so the
# surrounding structure stays intact.
_KV_RE = re.compile(
    r"(?i)(\b(?:" + "|".join(re.escape(k) for k in _SECRET_KEYS) + r")\b"
    r"\s*[\'\"]?\s*[:=]\s*[\'\"]?)"
    r"([^\s\'\",}\])]+)"
)

# Standalone "Bearer <token>" anywhere in the line (when not preceded by
# "Authorization:" — that case is handled by _AUTHZ_RE first).
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)(\S+)")


def redact_secrets(text: str) -> str:
    """Replace secret-looking values inside ``text`` with ``***``.

    Idempotent and safe for log lines, JSON snippets, env-style assignments,
    and ``Authorization: Bearer <token>`` headers. Never raises.
    """
    if not text:
        return text
    # Apply Authorization first so the multi-word value (Bearer + token)
    # is consumed before the standalone Bearer/KV patterns run.
    text = _AUTHZ_RE.sub(r"\1***", text)
    text = _BEARER_RE.sub(r"\1***", text)
    text = _KV_RE.sub(r"\1***", text)
    return text


class RedactSecretsFilter(logging.Filter):
    """Logging filter that scrubs secret-looking values from every record.

    Apply to handlers (not loggers) so the scrub catches records propagated
    from child loggers too. On any internal error the record passes through
    unchanged — log delivery is more important than perfect redaction.

    NOTE: this filter only touches ``record.msg`` / merged args. It does
    NOT see the rendered exception traceback emitted by handlers that use
    ``exc_info=True`` — that text is produced by
    :py:meth:`logging.Formatter.formatException` and bypasses the filter
    chain. Pair this filter with :class:`RedactingFormatter` on every
    handler to scrub the traceback too.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            formatted = record.getMessage()
            redacted = redact_secrets(formatted)
            if redacted != formatted:
                # ``record.getMessage()`` already merged %-args; clear args so
                # downstream formatters don't try to merge them a second time.
                record.msg = redacted
                record.args = None
        except Exception:
            # Logging path must never raise. Best-effort only.
            pass
        return True


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs the *fully rendered* log line, including the
    exception traceback that :class:`RedactSecretsFilter` cannot reach.

    ``Logger.error("X", exc_info=True)`` and ``logger.exception(...)`` cause
    the formatter to append a traceback produced by
    :py:meth:`logging.Formatter.formatException`. If a secret is stuffed
    in a frame's local repr or in an exception message, the filter chain
    has already finished and the secret lands in the file untouched.

    Attaching this formatter to every handler closes that gap by running
    ``redact_secrets`` over the final output. Never raises — on regex
    failure it falls back to the unredacted superclass output.
    """

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        try:
            return redact_secrets(text)
        except Exception:
            # Same rule as the filter: logging path must never raise.
            return text
