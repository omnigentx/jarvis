"""Shared fixtures for backend/tests/test_services.

Provides the ``mcp_db_isolation`` fixture: each test runs inside a SQLAlchemy
SAVEPOINT that is rolled back on teardown, so a test only undoes the rows it
itself inserted — no broad ``DELETE FROM mcp_servers`` that would also wipe
unrelated rows. This is opt-in (``autouse=False``); MCP test files that need
DB isolation declare a per-file autouse fixture that yields from this one.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def mcp_db_isolation(monkeypatch):
    """Wrap a test in a single outer transaction that is rolled back on
    teardown.

    Bind sessionmaker to a single Connection that already has a transaction
    open. Per SQLAlchemy: a Session bound to an externally-managed
    Connection does NOT commit that connection on ``session.commit()`` —
    the session just flushes; the connection's transaction stays open. So
    every ``with SessionLocal() as db: db.commit()`` from production code
    lands in the outer transaction, and a single ``outer.rollback()`` at
    teardown undoes only what this test wrote — never a broad
    ``DELETE FROM <table>`` that could wipe rows the test did not create.

    Patches ``SessionLocal`` in every module that imports it directly so
    code paths that bypass ``core.database.SessionLocal`` (e.g.
    ``services.mcp_catalog.SessionLocal``) also bind to the test
    connection.
    """
    from core import database as db_mod

    db_mod.Base.metadata.create_all(bind=db_mod.engine)

    connection = db_mod.engine.connect()
    outer_trans = connection.begin()

    TestSession = sessionmaker(bind=connection, autoflush=False, autocommit=False)

    # Capture the original before patching so we can identify modules that
    # imported it via ``from core.database import SessionLocal``.
    original_sl = db_mod.SessionLocal
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    # Patch every already-imported module (production AND test) whose
    # SessionLocal attribute IS the original — i.e. they did
    # ``from core.database import SessionLocal``.
    import sys
    for mod in list(sys.modules.values()):
        if mod is None or mod is db_mod:
            continue
        if getattr(mod, "SessionLocal", None) is original_sl:
            monkeypatch.setattr(mod, "SessionLocal", TestSession, raising=False)

    try:
        yield
    finally:
        if outer_trans.is_active:
            outer_trans.rollback()
        connection.close()
