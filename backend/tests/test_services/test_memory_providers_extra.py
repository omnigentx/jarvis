"""WS04: communication-provider authorization."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, CommunicationRecord
from services.retrieval.contracts import RetrievalRequest
from services.retrieval.providers.communication_provider import CommunicationProvider


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


async def test_communication_authorization(db):
    db.add(CommunicationRecord(id="c1", channel="email", sender="Jarvis",
                               recipients_json='["Riley [SA]"]', subject="deploy plan",
                               body="we will deploy friday", created_at=10.0))
    db.add(CommunicationRecord(id="c2", channel="email", sender="Riley [SA]",
                               recipients_json='["Jarvis"]', subject="deploy notes",
                               body="deploy verification steps", created_at=20.0))
    db.add(CommunicationRecord(id="c3", channel="email", sender="Riley [SA]",
                               recipients_json='["Other"]', subject="deploy secret",
                               body="deploy private chat", created_at=30.0))
    db.commit()
    prov = CommunicationProvider(db)
    res = await prov.search(RetrievalRequest(owner_agent_name="Jarvis", query="deploy"), limit=10)
    ids = {e.record_id for e in res}
    assert ids == {"c1", "c2"}        # sender or recipient only; c3 excluded
