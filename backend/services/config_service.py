"""ConfigService — read/write the ``system_config`` table.

Responsibilities
----------------

* Apply the precedence chain **DB → env var → caller-supplied default** when
  reading a value (YAML files are handled separately by the YAML editor).
* Encrypt/decrypt secret values transparently using :mod:`core.secrets_crypto`.
* Append an audit row to ``config_history`` on every mutation. Secret values
  are masked (``***``) in history so we never persist ciphertext that might
  become unreadable after key rotation.
* Notify registered listeners when a value changes — the hot-reload pipeline
  subscribes to this stream.

The service is intentionally thin: it talks to SQLAlchemy directly and does
*not* cache. SQLite under WAL mode handles the request volume of a single-user
backend without trouble, and avoiding caches removes a whole class of staleness
bugs.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session

from core import secrets_crypto
from core.database import ConfigHistory, SessionLocal, SystemConfig

logger = logging.getLogger(__name__)

SECRET_PLACEHOLDER = "***"


# ---- Data shapes -------------------------------------------------------------


@dataclass(frozen=True)
class ConfigEntry:
    """Read-model returned to API/UI code.

    For secrets, ``value`` is always :data:`None` and the caller should display
    ``has_value`` to indicate whether a secret is configured. We never decrypt
    secrets when listing — only :meth:`ConfigService.get` returns plaintext.
    """

    category: str
    key: str
    value: Optional[str]
    is_secret: bool
    has_value: bool
    source: str
    updated_at: float
    updated_by: str


@dataclass(frozen=True)
class ConfigChangeEvent:
    """Emitted on every successful mutation."""

    category: str
    key: str
    old_value: Optional[str]
    new_value: Optional[str]
    is_secret: bool
    action: str  # 'create' | 'update' | 'delete'


ChangeListener = Callable[[ConfigChangeEvent], None]


# ---- Service -----------------------------------------------------------------


class ConfigService:
    """Single-user-friendly settings backend."""

    def __init__(
        self,
        db_factory: Callable[[], Session] = SessionLocal,
    ) -> None:
        self._db_factory = db_factory
        self._listeners: list[ChangeListener] = []
        self._lock = threading.RLock()

    # ---- Listener API -------------------------------------------------------

    def subscribe(self, listener: ChangeListener) -> Callable[[], None]:
        """Register a listener; returns an idempotent unsubscribe callback."""
        with self._lock:
            self._listeners.append(listener)

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return _unsubscribe

    def _emit(self, event: ConfigChangeEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception(
                    "[CONFIG] Listener %r raised on %s/%s — continuing",
                    getattr(listener, "__name__", listener),
                    event.category,
                    event.key,
                )

    # ---- Read API -----------------------------------------------------------

    def get(
        self,
        category: str,
        key: str,
        *,
        default: Optional[str] = None,
        env_var: Optional[str] = None,
    ) -> Optional[str]:
        """Return the resolved value (DB → env → default).

        Args:
            category: Logical grouping (``'auth'``, ``'llm'``, ``'service.github'``, ...).
            key: Setting name. Conventionally an env-var-style identifier.
            default: Returned when no value is found anywhere.
            env_var: Env var to consult when DB is empty. Defaults to ``key``.
        """
        row = self._fetch(category, key)
        if row is not None and row.value is not None:
            decoded = self._decode(row.value, row.is_secret, category, key)
            if decoded is not None:
                return decoded
            # Secret stored but undecryptable — fall through to env so the user
            # is not locked out after a key rotation.
            logger.warning(
                "[CONFIG] %s/%s: stored secret could not be decrypted; falling back to env",
                category,
                key,
            )

        env_name = env_var if env_var is not None else key
        env_value = os.getenv(env_name)
        if env_value is not None:
            return env_value
        return default

    def get_entry(self, category: str, key: str) -> Optional[ConfigEntry]:
        """Metadata-rich read for a single key (no env fallback)."""
        row = self._fetch(category, key)
        if row is None:
            return None
        return self._row_to_entry(row, mask_secrets=True)

    def list_category(self, category: str) -> list[ConfigEntry]:
        with self._db_factory() as db:
            rows = (
                db.query(SystemConfig)
                .filter(SystemConfig.category == category)
                .order_by(SystemConfig.key)
                .all()
            )
            return [self._row_to_entry(row, mask_secrets=True) for row in rows]

    def list_all(self) -> dict[str, list[ConfigEntry]]:
        with self._db_factory() as db:
            rows = (
                db.query(SystemConfig)
                .order_by(SystemConfig.category, SystemConfig.key)
                .all()
            )
            grouped: dict[str, list[ConfigEntry]] = {}
            for row in rows:
                grouped.setdefault(row.category, []).append(
                    self._row_to_entry(row, mask_secrets=True)
                )
            return grouped

    def get_history(
        self,
        *,
        category: Optional[str] = None,
        key: Optional[str] = None,
        limit: int = 100,
    ) -> list[ConfigHistory]:
        if limit <= 0:
            return []
        with self._db_factory() as db:
            query = db.query(ConfigHistory)
            if category is not None:
                query = query.filter(ConfigHistory.category == category)
            if key is not None:
                query = query.filter(ConfigHistory.key == key)
            return query.order_by(ConfigHistory.changed_at.desc()).limit(limit).all()

    # ---- Write API ----------------------------------------------------------

    def set(
        self,
        category: str,
        key: str,
        value: Optional[str],
        *,
        is_secret: bool = False,
        source: str = "user",
        user: str = "user",
    ) -> ConfigChangeEvent:
        """Create / update / delete one config row, atomically with history.

        ``value=None`` deletes the row. Setting the exact same value is a
        no-op (no history entry, no event).
        """
        self._validate_identifier("category", category)
        self._validate_identifier("key", key)

        with self._db_factory() as db:
            row = self._fetch(category, key, db=db)
            old_plain = self._decode(row.value, row.is_secret, category, key) if row else None

            if value is None:
                if row is None:
                    # Nothing to delete; surface as a no-op event for callers
                    # that want feedback.
                    return ConfigChangeEvent(category, key, None, None, is_secret, "delete")
                event = ConfigChangeEvent(
                    category=category,
                    key=key,
                    old_value=old_plain,
                    new_value=None,
                    is_secret=row.is_secret,
                    action="delete",
                )
                db.delete(row)
                self._append_history(db, event, user)
                db.commit()
                self._emit(event)
                return event

            if row is not None and old_plain == value and row.is_secret == is_secret:
                # Idempotent write — no audit noise, no listener wake-up.
                return ConfigChangeEvent(
                    category, key, old_plain, value, is_secret, "update"
                )

            stored = self._encode(value, is_secret)
            now = datetime.now().timestamp()

            if row is None:
                action = "create"
                row = SystemConfig(
                    category=category,
                    key=key,
                    value=stored,
                    is_secret=is_secret,
                    source=source,
                    updated_at=now,
                    updated_by=user,
                )
                db.add(row)
            else:
                action = "update"
                row.value = stored
                row.is_secret = is_secret
                row.source = source
                row.updated_at = now
                row.updated_by = user

            event = ConfigChangeEvent(
                category=category,
                key=key,
                old_value=old_plain,
                new_value=value,
                is_secret=is_secret,
                action=action,
            )
            self._append_history(db, event, user)
            db.commit()
            self._emit(event)
            return event

    def set_many(
        self,
        items: Iterable[tuple[str, str, Optional[str], bool]],
        *,
        source: str = "user",
        user: str = "user",
    ) -> list[ConfigChangeEvent]:
        """Atomic bulk update.

        ``items`` is an iterable of ``(category, key, value, is_secret)`` tuples.
        Either every change lands and listeners fire, or the whole batch is
        rolled back and listeners stay silent.
        """
        items = list(items)
        if not items:
            return []

        events: list[ConfigChangeEvent] = []
        with self._db_factory() as db:
            try:
                for category, key, value, is_secret in items:
                    self._validate_identifier("category", category)
                    self._validate_identifier("key", key)

                    row = self._fetch(category, key, db=db)
                    old_plain = (
                        self._decode(row.value, row.is_secret, category, key)
                        if row
                        else None
                    )

                    if value is None:
                        if row is None:
                            events.append(
                                ConfigChangeEvent(
                                    category, key, None, None, is_secret, "delete"
                                )
                            )
                            continue
                        event = ConfigChangeEvent(
                            category, key, old_plain, None, row.is_secret, "delete"
                        )
                        db.delete(row)
                        self._append_history(db, event, user)
                        events.append(event)
                        continue

                    if row is not None and old_plain == value and row.is_secret == is_secret:
                        events.append(
                            ConfigChangeEvent(
                                category, key, old_plain, value, is_secret, "update"
                            )
                        )
                        continue

                    stored = self._encode(value, is_secret)
                    now = datetime.now().timestamp()
                    if row is None:
                        action = "create"
                        db.add(
                            SystemConfig(
                                category=category,
                                key=key,
                                value=stored,
                                is_secret=is_secret,
                                source=source,
                                updated_at=now,
                                updated_by=user,
                            )
                        )
                    else:
                        action = "update"
                        row.value = stored
                        row.is_secret = is_secret
                        row.source = source
                        row.updated_at = now
                        row.updated_by = user

                    event = ConfigChangeEvent(
                        category, key, old_plain, value, is_secret, action
                    )
                    self._append_history(db, event, user)
                    events.append(event)

                db.commit()
            except Exception:
                db.rollback()
                raise

        # Emit listeners only after a successful commit so listeners observe a
        # consistent post-commit state.
        for event in events:
            if event.action in ("create", "update", "delete") and (
                event.old_value != event.new_value
                or event.action == "delete"
            ):
                self._emit(event)
        return events

    def delete(
        self,
        category: str,
        key: str,
        *,
        user: str = "user",
    ) -> bool:
        """Convenience wrapper for ``set(..., value=None)``. Returns True on hit."""
        event = self.set(category, key, None, user=user)
        return event.action == "delete" and event.old_value is not None

    # ---- Internals ----------------------------------------------------------

    @staticmethod
    def _validate_identifier(label: str, value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string (got {value!r})")
        if len(value) > 100:
            raise ValueError(f"{label} too long (max 100 chars)")

    def _fetch(
        self,
        category: str,
        key: str,
        *,
        db: Optional[Session] = None,
    ) -> Optional[SystemConfig]:
        if db is not None:
            return (
                db.query(SystemConfig)
                .filter(SystemConfig.category == category, SystemConfig.key == key)
                .one_or_none()
            )
        with self._db_factory() as owned:
            return (
                owned.query(SystemConfig)
                .filter(SystemConfig.category == category, SystemConfig.key == key)
                .one_or_none()
            )

    @staticmethod
    def _encode(value: str, is_secret: bool) -> str:
        if is_secret:
            return secrets_crypto.encrypt(value)
        return value

    @staticmethod
    def _decode(
        stored: Optional[str],
        is_secret: bool,
        category: str,
        key: str,
    ) -> Optional[str]:
        if stored is None:
            return None
        if not is_secret:
            return stored
        plain = secrets_crypto.decrypt(stored)
        if plain is None:
            logger.warning("[CONFIG] %s/%s: secret undecryptable", category, key)
        return plain

    @staticmethod
    def _row_to_entry(row: SystemConfig, *, mask_secrets: bool) -> ConfigEntry:
        if row.is_secret:
            value: Optional[str] = SECRET_PLACEHOLDER if mask_secrets else None
            has_value = row.value is not None and row.value != ""
            if mask_secrets and not has_value:
                value = None
        else:
            value = row.value
            has_value = row.value is not None and row.value != ""

        return ConfigEntry(
            category=row.category,
            key=row.key,
            value=value,
            is_secret=row.is_secret,
            has_value=has_value,
            source=row.source,
            updated_at=row.updated_at,
            updated_by=row.updated_by,
        )

    @staticmethod
    def _append_history(
        db: Session,
        event: ConfigChangeEvent,
        user: str,
    ) -> None:
        old = SECRET_PLACEHOLDER if event.is_secret and event.old_value else event.old_value
        new = SECRET_PLACEHOLDER if event.is_secret and event.new_value else event.new_value
        db.add(
            ConfigHistory(
                category=event.category,
                key=event.key,
                old_value=old,
                new_value=new,
                is_secret=event.is_secret,
                action=event.action,
                changed_by=user,
            )
        )


# Module-level singleton used by routes and other services.
config_service = ConfigService()
