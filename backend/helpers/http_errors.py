"""Helpers for HTTP error responses that don't leak internals.

Routes used to do ``raise HTTPException(500, detail=str(exc))`` which
ships the underlying message — file paths, library names, schema bits,
``__cause__`` chains — back to the client. Authenticated client only,
so the practical risk is low, but it's a pattern static analysers flag
on first pass and it's trivial to centralise.

``safe_500`` logs the real exception (with traceback) server-side and
returns a generic dict payload. Use it for 5xx server errors only.
For 4xx, the existing ``detail=...`` is usually intentional information
(``"approval not found"``) so don't blanket-replace those.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException


def safe_500(
    exc: BaseException,
    logger: logging.Logger,
    reason: str = "internal_error",
    *,
    status_code: int = 500,
) -> HTTPException:
    """Build an :class:`HTTPException` with a generic dict body, after
    logging the real exception (with traceback) to ``logger``.

    Args:
        exc: the caught exception. Logged with ``exc_info`` so the full
            stack lands in the server-side log without leaking back to
            the client.
        logger: the route's module-level logger; the [ROUTE] prefix tag
            keeps grepping easy.
        reason: short machine-readable tag for the client side
            (``"create_failed"``, ``"stats_failed"``, …). Stable strings
            so a frontend can switch on them if it wants to.
        status_code: defaults to 500; pass 503 for "external resource
            down" / "service unavailable" semantics if you keep the
            original status when changing the body.

    Returns:
        An :class:`HTTPException` with ``detail={"error": "internal_error",
        "reason": reason}``. Raise this from the caller.
    """
    logger.error("[ROUTE] %dxx — %s: %s", status_code // 100, reason, exc, exc_info=True)
    return HTTPException(
        status_code=status_code,
        detail={"error": "internal_error", "reason": reason},
    )
