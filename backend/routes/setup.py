"""Setup Wizard API — ``/api/setup/*``.

Drives the 5-step first-run wizard (``auth → llm → services → yaml_config →
verify``).  Critical steps (``auth``, ``llm``, ``verify``) cannot be skipped;
the non-critical ``services`` and ``yaml_config`` steps can be bypassed so the
user can finish the wizard with a minimum viable configuration and flesh it
out later from the Settings UI.

The ``auth`` step is intentionally open: the user cannot bring a bearer token
before they have chosen one.  Once they complete this step, ``verify_api_key``
kicks in for every subsequent endpoint.
"""
from __future__ import annotations

import json
import logging
import re
import secrets as py_secrets
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core import auth as core_auth
from core.auth import verify_api_key
from core.database import (
    SETUP_WIZARD_CRITICAL_STEPS,
    SETUP_WIZARD_STEPS,
    SetupWizardStep,
    get_db_session,
)
from middleware.setup_gate import refresh_setup_complete
from services.config_service import config_service
from services.runtime_config import apply_api_key

logger = logging.getLogger("setup_api")
router = APIRouter(prefix="/api/setup", tags=["setup"])


# Service names come from the UI; restrict to a safe charset to keep the
# resulting config category (``service.<name>``) sane.
_SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,40}$")
_MIN_API_KEY_LEN = 16


# ---- Schemas -----------------------------------------------------------------


class StepStatus(BaseModel):
    name: str
    completed: bool
    skipped: bool
    completed_at: Optional[float] = None
    data: Optional[dict] = None


class SetupStatus(BaseModel):
    steps: list[StepStatus]
    overall_complete: bool
    current_step: Optional[str] = None  # first pending step, or None if done


class AuthStep(BaseModel):
    api_key: Optional[str] = Field(
        default=None,
        description=f"Master key (min {_MIN_API_KEY_LEN} chars). Auto-generated if omitted.",
    )


class LLMStep(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=100)
    # Optional so a user who is re-submitting Step 2 after hitting Back can
    # leave the field blank to preserve the previously-stored key.  We enforce
    # "must be present OR already stored" server-side below.
    api_key: Optional[str] = Field(default=None, max_length=500)
    base_url: Optional[str] = Field(default=None, max_length=500)


class ServicesStep(BaseModel):
    # ``{service: {key: value, ...}}`` — values are all treated as secrets.
    services: dict[str, dict[str, str]] = Field(default_factory=dict)


class VerifyStep(BaseModel):
    accept_warnings: bool = False


# ---- Helpers -----------------------------------------------------------------


def _mark_step_done(
    step_name: str,
    *,
    skipped: bool = False,
    data: Optional[dict[str, Any]] = None,
) -> None:
    db = get_db_session()
    try:
        row = db.query(SetupWizardStep).filter_by(step_name=step_name).one_or_none()
        if row is None:
            row = SetupWizardStep(step_name=step_name)
            db.add(row)
        row.completed = not skipped
        row.skipped = skipped
        row.completed_at = datetime.now().timestamp()
        if data is not None:
            row.data_json = json.dumps(data, ensure_ascii=False)
        db.commit()
    finally:
        db.close()
    # Invalidate the setup-gate cache so subsequent API calls see the new state
    # immediately instead of waiting for a lazy refresh.
    refresh_setup_complete()


def _load_steps() -> list[StepStatus]:
    db = get_db_session()
    try:
        rows = {r.step_name: r for r in db.query(SetupWizardStep).all()}
    finally:
        db.close()

    out: list[StepStatus] = []
    for name in SETUP_WIZARD_STEPS:
        r = rows.get(name)
        if r is None:
            out.append(StepStatus(name=name, completed=False, skipped=False))
            continue
        data: Optional[dict] = None
        if r.data_json:
            try:
                data = json.loads(r.data_json)
            except Exception:
                logger.warning("[SETUP] step %s has non-JSON data; ignoring", name)
        out.append(
            StepStatus(
                name=r.step_name,
                completed=bool(r.completed),
                skipped=bool(r.skipped),
                completed_at=r.completed_at,
                data=data,
            )
        )
    return out


def _overall_complete(steps: list[StepStatus]) -> bool:
    """All critical steps must be *completed* (not merely skipped)."""
    by_name = {s.name: s for s in steps}
    for critical in SETUP_WIZARD_CRITICAL_STEPS:
        s = by_name.get(critical)
        if s is None or not s.completed:
            return False
    return True


def _current_step(steps: list[StepStatus]) -> Optional[str]:
    for s in steps:
        if not (s.completed or s.skipped):
            return s.name
    return None


def _build_status() -> SetupStatus:
    steps = _load_steps()
    return SetupStatus(
        steps=steps,
        overall_complete=_overall_complete(steps),
        current_step=_current_step(steps),
    )


# ---- Endpoints ---------------------------------------------------------------


@router.get("/status", response_model=SetupStatus)
async def get_setup_status(_=Depends(verify_api_key)):
    return _build_status()


class AuthProbe(BaseModel):
    """Minimal "has a master key been set?" probe.

    Intentionally unauthenticated: the wizard's Auth step needs to know
    whether it should render the *pick-a-key* flow or the
    *confirm-existing-key* flow, and the user has no bearer to present yet.
    The response only reveals the boolean — never the key itself.
    """

    configured: bool


@router.get("/auth/probe", response_model=AuthProbe)
async def auth_probe():
    return AuthProbe(configured=bool(core_auth.JARVIS_API_KEY))


@router.post("/auth", response_model=SetupStatus)
async def setup_auth(payload: AuthStep, request: Request, response: Response):
    """Step 1 — choose, adopt, or generate the master key.

    Intentionally *not* guarded by :func:`verify_api_key` because the user
    cannot present a bearer token before they pick one.  Three cases:

    1. **No key yet** — generate or accept the supplied key and persist it.
    2. **Key already configured** (e.g. from ``.env`` on a fresh container)
       and the supplied key *matches* — treat the wizard step as confirmed
       and move on.  This recovers the "have key, wizard not done yet" state
       that previously left the user stuck.
    3. **Key already configured** and the supplied key *differs* — refuse.
       Rotating the master key belongs in the authenticated
       ``/api/settings`` surface, not the open wizard.

    On success we ALSO mint a ``jarvis_session`` cookie and return the
    CSRF token — same shape as ``POST /api/auth/login``. Without this,
    the wizard would have to stash the API key in localStorage to
    authenticate steps 2–5 (``/llm``, ``/services``, ``/verify``), which
    is exactly the legacy path we're moving off of.
    """
    supplied = (payload.api_key or "").strip()
    current = core_auth.JARVIS_API_KEY or ""

    if current:
        # Case 2/3: something is already in place.  Let the user confirm by
        # re-entering it (or leaving the field blank to "adopt" it).
        if supplied and supplied != current:
            raise HTTPException(
                status_code=403,
                detail=(
                    "A different master key is already configured. "
                    "Rotate via Settings → General → Authentication, "
                    "or re-enter the existing key to confirm."
                ),
            )
        # Adopt the existing key; ensure it's persisted in env + auth module.
        apply_api_key(current)
        config_service.set(
            "auth",
            "JARVIS_API_KEY",
            current,
            is_secret=False,
            source="wizard",
        )
        _mark_step_done("auth", data={"adopted": True})
        _mint_wizard_session(request, response)
        return _build_status()

    # Case 1: fresh install — generate or accept.
    api_key = supplied or py_secrets.token_urlsafe(32)
    if len(api_key) < _MIN_API_KEY_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"API key too short (min {_MIN_API_KEY_LEN} chars).",
        )

    # Apply to env/auth so the auth dependency picks it up immediately.
    # Crypto uses JARVIS_MASTER_KEY (separate env), unaffected by this write.
    apply_api_key(api_key)
    # The auth key is stored unencrypted (it would be circular under the
    # master key anyway); we rely on OS filesystem permissions, same
    # guarantee as a .env file.
    config_service.set(
        "auth",
        "JARVIS_API_KEY",
        api_key,
        is_secret=False,
        source="wizard",
    )
    _mark_step_done("auth", data={"generated": not supplied})
    _mint_wizard_session(request, response)
    return _build_status()


def _mint_wizard_session(request: "Request", response: "Response") -> None:
    """Mint a session cookie so the wizard's remaining steps can call
    cookie-authenticated endpoints (``/llm``, ``/services``, etc.).

    Raises 503 if ``JWT_SECRET`` is missing. Previously this swallowed
    the failure on the assumption the SPA could fall back to the
    Bearer header — but that fallback was removed in the cookie-only
    migration, so a silent miss here leaves the wizard wedged at step
    2 with a confusing "Authentication required" modal. Fail loud so
    the operator sees the actionable error.
    """
    from core.auth_cookies import set_auth_cookies
    from core.session import create_session_token, make_csrf_token

    try:
        session_token, _payload = create_session_token()
    except RuntimeError as exc:
        logger.error("[SETUP] JWT_SECRET missing — wizard cannot mint cookie: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "not_configured",
                "reason": "jwt_secret_unset",
                "message": (
                    "JWT_SECRET is not set. Add it to backend/.env "
                    "(any 32+ char random string) and restart the backend, "
                    "then re-run setup."
                ),
            },
        ) from exc
    csrf_token = make_csrf_token()
    set_auth_cookies(response, request, session_token, csrf_token)


@router.post("/llm", response_model=SetupStatus)
async def setup_llm(payload: LLMStep, _=Depends(verify_api_key)):
    # Per-provider schema: the wizard lets the user configure one provider at
    # a time; we persist its credentials under ``llm.{provider}_api_key`` /
    # ``llm.{provider}_base_url`` so future swaps to a different provider
    # don't clobber the previously stored key.  The UI label "custom" is
    # translated to fast-agent's canonical ``generic`` slot here so we have a
    # single source of truth downstream.
    ui_provider = (payload.provider or "").lower().strip()
    slot = "generic" if ui_provider == "custom" else ui_provider
    if slot not in ("openai", "anthropic", "generic"):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported provider: {payload.provider!r}",
        )

    api_key_field = f"{slot}_api_key"
    base_url_field = f"{slot}_base_url"

    items: list[tuple[str, str, Optional[str], bool]] = [
        ("llm", "provider", ui_provider, False),
        ("llm", "model", payload.model, False),
    ]

    submitted_key = (payload.api_key or "").strip()
    if submitted_key:
        items.append(("llm", api_key_field, submitted_key, True))
        api_key_stored = True
    else:
        # Blank api_key on re-submit means "keep what's already stored for
        # this provider".  Refuse only when the provider has never been
        # configured — LLM can't authenticate without a key.
        existing = config_service.get("llm", api_key_field)
        if not existing:
            raise HTTPException(
                status_code=400,
                detail="api_key is required for the first LLM setup.",
            )
        api_key_stored = True

    if payload.base_url:
        items.append(("llm", base_url_field, payload.base_url, False))

    config_service.set_many(items, source="wizard")

    # Expose a per-provider has-key map so the wizard can light up every
    # provider tab the user has already configured — switching tabs mid-
    # wizard must not lose the "already stored" badge for other providers.
    # Secret *values* are still never echoed.
    keys_by_slot: dict[str, bool] = {}
    for s in ("openai", "anthropic", "generic"):
        keys_by_slot[s] = bool(config_service.get("llm", f"{s}_api_key"))

    _mark_step_done(
        "llm",
        # ``data`` is visible via GET /api/setup/status (no setup-gate
        # blockage) so the wizard can hydrate Step 2 on revisit.  api_key is
        # deliberately omitted — secrets should only flow back through the
        # authenticated /api/settings surface once the wizard is complete.
        data={
            "provider": ui_provider,
            "model": payload.model,
            "base_url": payload.base_url or None,
            "api_key_set": api_key_stored,
            "keys_by_slot": keys_by_slot,
        },
    )
    return _build_status()


@router.post("/services", response_model=SetupStatus)
async def setup_services(payload: ServicesStep, _=Depends(verify_api_key)):
    items: list[tuple[str, str, Optional[str], bool]] = []
    configured: list[str] = []
    for svc_name, fields in payload.services.items():
        name = (svc_name or "").strip()
        if not name or not _SERVICE_NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid service name: {svc_name!r}",
            )
        if not fields:
            continue
        for k, v in fields.items():
            if not isinstance(k, str) or not k.strip():
                raise HTTPException(
                    status_code=400, detail=f"Empty field name under service {name!r}"
                )
            items.append((f"service.{name}", k.strip(), v, True))
        configured.append(name)
    if items:
        config_service.set_many(items, source="wizard")
    _mark_step_done("services", data={"configured": configured})
    return _build_status()


@router.post("/yaml_config", response_model=SetupStatus)
async def setup_yaml(_=Depends(verify_api_key)):
    """Mark the YAML config step done — the editor itself lives in Phase 2."""
    _mark_step_done("yaml_config")
    return _build_status()


@router.post("/verify", response_model=SetupStatus)
async def setup_verify(payload: VerifyStep, _=Depends(verify_api_key)):
    missing: list[str] = []
    if not core_auth.JARVIS_API_KEY:
        missing.append("auth.JARVIS_API_KEY")
    # Per-provider storage: the active provider is in ``llm.provider`` and its
    # key lives under ``llm.{slot}_api_key`` (UI "custom" → slot "generic").
    ui_provider = (config_service.get("llm", "provider") or "").lower().strip()
    slot = "generic" if ui_provider == "custom" else ui_provider
    if not slot or not config_service.get("llm", f"{slot}_api_key"):
        missing.append(f"llm.{slot or 'anthropic'}_api_key")
    if not config_service.get("llm", "model"):
        missing.append("llm.model")

    if missing and not payload.accept_warnings:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Cannot complete setup — missing critical config.",
                "missing": missing,
            },
        )
    _mark_step_done(
        "verify",
        data={"missing": missing, "accepted_warnings": payload.accept_warnings},
    )
    return _build_status()


@router.post("/step/{name}/skip", response_model=SetupStatus)
async def skip_step(name: str, _=Depends(verify_api_key)):
    if name not in SETUP_WIZARD_STEPS:
        raise HTTPException(status_code=404, detail=f"Unknown step: {name}")
    if name in SETUP_WIZARD_CRITICAL_STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"Step '{name}' is critical and cannot be skipped.",
        )
    _mark_step_done(name, skipped=True)
    return _build_status()


@router.post("/reset", response_model=SetupStatus)
async def reset_wizard(_=Depends(verify_api_key)):
    """Reset wizard flags.  Does **not** touch stored config rows — use the
    Settings API if the user wants to clear those too."""
    db = get_db_session()
    try:
        db.query(SetupWizardStep).update(
            {
                SetupWizardStep.completed: False,
                SetupWizardStep.skipped: False,
                SetupWizardStep.completed_at: None,
                SetupWizardStep.data_json: None,
            }
        )
        db.commit()
    finally:
        db.close()
    refresh_setup_complete()
    return _build_status()
