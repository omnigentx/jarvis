"""Roborock login session persistence (tools.iot_server).

The session is a SECRET — it must persist to the DB via config_service with
is_secret=True (encrypted at rest, survives deploys), like every other credential
in the app. It used to be a plaintext JSON file under config/credentials/, which
the deploy's `git clean` wiped every time → repeated 2FA. These lock in the DB
contract without depending on the roborock lib's UserData shape or real Fernet.
"""
import asyncio
import json

from tools import iot_server


class _FakeConfigService:
    def __init__(self):
        self.store = {}

    def set(self, category, key, value, *, is_secret=False, **kw):
        self.store[(category, key)] = {"value": value, "is_secret": is_secret}

    def get(self, category, key, *, default=None):
        row = self.store.get((category, key))
        return row["value"] if row else default


class _FakeUserData:
    def __init__(self, d):
        self._d = d

    def as_dict(self):
        return self._d


def _use_fake_config(monkeypatch):
    fake = _FakeConfigService()
    monkeypatch.setattr("services.config_service.config_service", fake)
    monkeypatch.setattr(iot_server, "UserData",
                        type("UD", (), {"from_dict": staticmethod(lambda d: _FakeUserData(d))}))
    return fake


def test_session_persists_encrypted_in_db_under_service_roborock(monkeypatch):
    fake = _use_fake_config(monkeypatch)
    assert (iot_server._ROBOROCK_CATEGORY, iot_server._ROBOROCK_SESSION_KEY) \
        == ("service.roborock", "session")

    ud = _FakeUserData({"rriot": {"u": "user", "s": "secret"}, "token": "abc"})
    asyncio.run(iot_server.save_session(ud))

    row = fake.store[("service.roborock", "session")]
    assert row["is_secret"] is True                    # encrypted at rest
    assert json.loads(row["value"]) == ud.as_dict()    # stored as JSON


def test_session_round_trips_through_db(monkeypatch):
    _use_fake_config(monkeypatch)
    ud = _FakeUserData({"token": "xyz", "n": 1})
    asyncio.run(iot_server.save_session(ud))
    loaded = asyncio.run(iot_server.load_session())
    assert loaded.as_dict() == ud.as_dict()


def test_load_returns_none_when_nothing_stored(monkeypatch):
    _use_fake_config(monkeypatch)
    assert asyncio.run(iot_server.load_session()) is None


def test_session_encrypts_at_rest_via_real_config_service(monkeypatch, tmp_path):
    """End-to-end crypto: with a real ConfigService + real Fernet, is_secret=True
    stores CIPHERTEXT in system_config and load_session decrypts it back."""
    from core import secrets_crypto
    from core.database import Base, SystemConfig
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from services.config_service import ConfigService

    monkeypatch.setenv("JARVIS_MASTER_KEY", "iot-tests-master-key-please-rotate")
    secrets_crypto.reload_master_key()
    engine = create_engine(f"sqlite:///{tmp_path}/cfg.db", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    monkeypatch.setattr("services.config_service.config_service",
                        ConfigService(db_factory=Session))
    monkeypatch.setattr(iot_server, "UserData",
                        type("UD", (), {"from_dict": staticmethod(lambda d: _FakeUserData(d))}))

    ud = _FakeUserData({"token": "s3cr3t-value", "rriot": {"u": "x"}})
    asyncio.run(iot_server.save_session(ud))

    row = Session().query(SystemConfig).filter_by(
        category="service.roborock", key="session").one()
    assert row.is_secret is True
    assert "s3cr3t-value" not in row.value                       # ciphertext at rest
    assert asyncio.run(iot_server.load_session()).as_dict() == ud.as_dict()  # decrypts back
