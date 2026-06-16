"""N1 regression: the exact-dedup concurrency backstop.

The app-level read-then-insert dedup in MemoryService is racy — two concurrent
captures of the same fact both miss the existence check and both commit. The
fix is a PARTIAL UNIQUE index on (owner_agent_name, normalized_content,
subject_scope) WHERE status='active', created in init_db() after a one-time
dedup pass. These tests pin both halves: the migration collapses pre-existing
duplicate active rows, and the index then rejects a new active duplicate while
still permitting archived/superseded copies.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, MemoryRecord

_DEDUP_INDEX_SQL = (
    "CREATE UNIQUE INDEX uq_memory_active_dedup "
    "ON memory_records (owner_agent_name, normalized_content, subject_scope) "
    "WHERE status = 'active'"
)
_DEDUP_COLLAPSE_SQL = (
    "UPDATE memory_records SET status='archived' "
    "WHERE status='active' AND id NOT IN ("
    "  SELECT id FROM ("
    "    SELECT id, ROW_NUMBER() OVER ("
    "      PARTITION BY owner_agent_name, normalized_content, subject_scope "
    "      ORDER BY created_at DESC, id DESC) AS rn "
    "    FROM memory_records WHERE status='active'"
    "  ) WHERE rn = 1)"
)


def _engine():
    eng = create_engine("sqlite:///:memory:",
                        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return eng


def _row(rid, *, status="active", content="user prefers car", created_at=1.0):
    return MemoryRecord(
        id=rid, owner_agent_name="Jarvis", memory_type="semantic",
        subject_scope="user", content=content, normalized_content=content,
        status=status, authority="user_confirmed", current_version=1,
        created_at=created_at, updated_at=created_at)


def test_migration_collapses_existing_active_dups():
    eng = _engine()
    Session = sessionmaker(bind=eng)
    db = Session()
    db.add(_row("old", created_at=10.0))
    db.add(_row("new", created_at=20.0))   # same tuple, newer
    db.commit()

    with eng.connect() as c:
        c.execute(text(_DEDUP_COLLAPSE_SQL))
        c.execute(text(_DEDUP_INDEX_SQL))   # must succeed now (no active dups left)
        c.commit()

    db.expire_all()
    assert db.get(MemoryRecord, "new").status == "active"   # newest kept
    assert db.get(MemoryRecord, "old").status == "archived"  # older collapsed


def test_index_rejects_second_active_dup_but_allows_archived():
    eng = _engine()
    with eng.connect() as c:
        c.execute(text(_DEDUP_INDEX_SQL))
        c.commit()
    Session = sessionmaker(bind=eng)
    db = Session()
    db.add(_row("a"))
    db.commit()

    # Second ACTIVE row with the same dedup tuple → index violation.
    db.add(_row("b"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # An ARCHIVED copy of the same fact is allowed (partial index excludes it).
    db.add(_row("c", status="archived"))
    db.commit()
    assert db.get(MemoryRecord, "c").status == "archived"
