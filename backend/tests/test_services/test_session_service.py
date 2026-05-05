"""Tests for session_service.py — chat session management."""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from fast_agent.session import SessionManager
from services.session_service import SessionService


@pytest.fixture()
def isolated_svc(tmp_path):
    """SessionService backed by a tmp session directory.

    Without isolation the service writes through to ``.fast-agent/sessions/``
    in the repo, leaving cruft after every test run. We swap the underlying
    SessionManager for one rooted at tmp_path so each test gets a clean slate
    and tmp_path is auto-cleaned by pytest.
    """
    svc = SessionService()
    svc._manager = SessionManager(cwd=tmp_path)
    return svc


class TestSessionService:
    """Tests for SessionService operations."""

    def test_init_creates_instance(self, isolated_svc):
        """SessionService should initialize without error."""
        assert isolated_svc is not None
        assert isolated_svc._manager is not None

    def test_create_session_returns_dict(self, isolated_svc):
        """create_session should return dict with id and title."""
        result = isolated_svc.create_session("Test Chat")
        assert "id" in result
        assert result["title"] == "Test Chat"

    def test_create_session_default_title(self, isolated_svc):
        """create_session with no title should use 'New Chat'."""
        result = isolated_svc.create_session()
        assert result["title"] == "New Chat"

    def test_list_sessions_returns_list(self, isolated_svc):
        """list_sessions should return a list."""
        assert isinstance(isolated_svc.list_sessions(), list)

    def test_multiple_sessions_unique_ids(self, isolated_svc):
        """Each created session should have a unique ID."""
        s1 = isolated_svc.create_session("Chat 1")
        s2 = isolated_svc.create_session("Chat 2")
        assert s1["id"] != s2["id"]


class TestDeleteSession:
    """Delete-path coverage. Without these the create_* tests above would
    silently accumulate session files over time (pre-isolation) and we'd
    have no signal that the delete code path actually works.
    """

    def test_delete_existing_session_returns_true(self, isolated_svc):
        created = isolated_svc.create_session("To Delete")
        assert isolated_svc.delete_session(created["id"]) is True
        # Manager-level confirmation: it's gone from disk.
        assert isolated_svc._manager.get_session(created["id"]) is None

    def test_delete_unknown_session_returns_false(self, isolated_svc):
        # Non-existent id: must NOT raise; returns False so callers / route
        # handlers can be idempotent.
        assert isolated_svc.delete_session("does-not-exist") is False

    def test_delete_removes_session_from_list(self, isolated_svc):
        s1 = isolated_svc.create_session("Keep")
        s2 = isolated_svc.create_session("Drop")
        isolated_svc.delete_session(s2["id"])
        # list_sessions skips sessions with no saved history (the create-only
        # ones used here are filtered out), so we assert via the manager.
        manager_ids = {info.name for info in isolated_svc._manager.list_sessions()}
        assert s1["id"] in manager_ids
        assert s2["id"] not in manager_ids

    def test_delete_is_idempotent(self, isolated_svc):
        created = isolated_svc.create_session("Del twice")
        assert isolated_svc.delete_session(created["id"]) is True
        # Second call: nothing to delete, must not raise.
        assert isolated_svc.delete_session(created["id"]) is False


class TestResumeAndSendCancellationRollback:
    """When a turn is cancelled mid-LLM, fast-agent's OpenAI provider
    catches CancelledError and returns an empty Prompt rather than
    re-raising. The agent has already pushed the user message + a
    placeholder assistant message into ``message_history``; if we let
    ``save_history`` run, the disk session ends up with a phantom
    user/blank-assistant pair the next reload renders as a real
    exchange. The rollback path must:

      * skip ``save_history`` for the cancelled call
      * pop the half-turn entries from ``agent.message_history`` so the
        in-memory state matches what we'd want on disk
    """

    @pytest.mark.asyncio
    async def test_cancelled_turn_does_not_persist_history(self, monkeypatch):
        # Build a fake agent whose ``send`` simulates fast-agent's
        # CancelledError swallow: it appends [user, assistant_empty]
        # into message_history, then returns "" instead of raising.
        fake_agent = MagicMock()
        fake_agent.message_history = []
        fake_agent.clear = MagicMock()
        fake_agent.config = MagicMock()
        fake_agent.config.default = True

        save_history_calls: list = []

        # Stub session that records save_history invocations.
        fake_session = MagicMock()
        fake_session.info.metadata = {}
        fake_session.info.name = "session-x"
        fake_session.set_title = MagicMock()
        fake_session.latest_history_path = MagicMock(return_value=None)
        async def _save(*args, **kwargs):
            save_history_calls.append(args)
        fake_session.save_history = _save

        svc = SessionService()
        svc._manager = MagicMock()
        svc._manager.get_session = MagicMock(return_value=fake_session)
        svc._manager.load_session = MagicMock(return_value=fake_session)
        svc._manager.create_session = MagicMock(return_value=fake_session)

        fake_app = MagicMock()
        fake_app._agents = {"Jarvis": fake_agent}

        async def _swallow_cancel_send(payload, agent_name=None):
            # Mirror fast-agent's contract on cancel.
            fake_agent.message_history.append({"role": "user", "content": "hi"})
            fake_agent.message_history.append({"role": "assistant", "content": ""})
            # Trigger ``cancelling()`` >0 by self-cancelling the task,
            # then catching the CancelledError and returning empty —
            # exactly the production swallow path.
            try:
                cur = asyncio.current_task()
                cur.cancel()
                await asyncio.sleep(0)  # surface the cancel
            except asyncio.CancelledError:
                pass
            return ""
        fake_app.send = _swallow_cancel_send

        with patch.object(svc, "_agent_lock", asyncio.Lock()):
            response, sid = await svc.resume_and_send(
                fake_app, "hi", session_id=None, agent_name="Jarvis"
            )

        assert response == ""
        # The fix must have popped both stale entries:
        assert fake_agent.message_history == [], (
            "agent.message_history retained the cancelled half-turn — "
            "the rollback should pop both the user and the empty "
            "assistant entries"
        )
        # And save_history must NOT have been called for the cancelled
        # turn — otherwise the phantom pair lands on disk.
        assert save_history_calls == [], (
            f"save_history was called {len(save_history_calls)} time(s) "
            "for a cancelled turn — the on-disk session would surface a "
            "ghost user/blank-assistant pair on reload"
        )
