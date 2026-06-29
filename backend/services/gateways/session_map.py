"""Persistent chat→session binding for gateways (the SSoT index).

Every inbound message from the same external chat must continue the *same*
backend conversation, across restarts. This module is the single authoritative
place that binds ``(platform, chat_id)`` to a backend ``session_id``.

It deliberately does NOT decide whether a session still exists — the backend
``SessionManager`` owns that. The dispatcher passes :func:`lookup`'s result into
``resume_and_send`` (which creates a fresh session if the bound one is gone) and
then calls :func:`upsert` with the id that came back, so the binding self-heals
instead of silently pointing at a dead session.
"""
from __future__ import annotations

import time
from typing import Optional

from core.database import GatewayChat, get_db_session


def lookup(platform: str, chat_id: str) -> Optional[str]:
    """Return the backend session_id bound to this chat, or ``None``."""
    db = get_db_session()
    try:
        row = (
            db.query(GatewayChat)
            .filter_by(platform=platform, chat_id=str(chat_id))
            .one_or_none()
        )
        return row.session_id if row else None
    finally:
        db.close()


def get_agent(platform: str, chat_id: str) -> Optional[str]:
    """Return the per-chat answering agent bound to this chat, or ``None``.

    Set by the ``/agent`` command; lets a single gateway answer different chats
    with different agents without a config change.
    """
    db = get_db_session()
    try:
        row = (
            db.query(GatewayChat)
            .filter_by(platform=platform, chat_id=str(chat_id))
            .one_or_none()
        )
        return row.agent_name if row else None
    finally:
        db.close()


def delete(platform: str, chat_id: str) -> None:
    """Drop the chat→session binding so the next message starts a fresh
    conversation (the ``/new`` command). Idempotent."""
    db = get_db_session()
    try:
        db.query(GatewayChat).filter_by(platform=platform, chat_id=str(chat_id)).delete()
        db.commit()
    finally:
        db.close()


def set_agent(platform: str, chat_id: str, agent_name: str) -> None:
    """Change the answering agent for this chat (``/agent``), keeping the bound
    session if any. Creates a session-less row (``session_id=""``) when the chat
    has no binding yet so the choice persists; the next message fills in the
    real session id. ``""`` reads back as falsy in :func:`lookup`, so a fresh
    session is created on that next turn."""
    db = get_db_session()
    try:
        now = time.time()
        row = (
            db.query(GatewayChat)
            .filter_by(platform=platform, chat_id=str(chat_id))
            .one_or_none()
        )
        if row:
            row.agent_name = agent_name
            row.updated_at = now
        else:
            db.add(GatewayChat(
                platform=platform, chat_id=str(chat_id),
                session_id="", agent_name=agent_name,
                created_at=now, updated_at=now,
            ))
        db.commit()
    finally:
        db.close()


def upsert(platform: str, chat_id: str, session_id: str, agent_name: str) -> None:
    """Bind (or rebind) ``(platform, chat_id)`` to ``session_id``."""
    db = get_db_session()
    try:
        now = time.time()
        row = (
            db.query(GatewayChat)
            .filter_by(platform=platform, chat_id=str(chat_id))
            .one_or_none()
        )
        if row:
            row.session_id = session_id
            row.agent_name = agent_name
            row.updated_at = now
        else:
            db.add(GatewayChat(
                platform=platform,
                chat_id=str(chat_id),
                session_id=session_id,
                agent_name=agent_name,
                created_at=now,
                updated_at=now,
            ))
        db.commit()
    finally:
        db.close()
