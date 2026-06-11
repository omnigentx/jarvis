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

    def test_list_sessions_returns_envelope(self, isolated_svc):
        """list_sessions returns the paginated {items, total} envelope."""
        result = isolated_svc.list_sessions()
        assert isinstance(result, dict)
        assert isinstance(result["items"], list)
        assert isinstance(result["total"], int)

    def test_multiple_sessions_unique_ids(self, isolated_svc):
        """Each created session should have a unique ID."""
        s1 = isolated_svc.create_session("Chat 1")
        s2 = isolated_svc.create_session("Chat 2")
        assert s1["id"] != s2["id"]


class TestListSessionsFilterPagination:
    """Agent-scoping + paging for the conversation sidebar.

    Sessions only become listable once a primary agent is stamped (a bare
    create has no agent and is hidden), so we seed metadata directly — the
    same key ``resume_and_send`` writes on the first turn.
    """

    @staticmethod
    def _seed(svc, title, agent):
        from services.session_service import PRIMARY_AGENT_META_KEY
        s = svc._manager.create_session(
            metadata={"title": title, PRIMARY_AGENT_META_KEY: agent}
        )
        return s.info.name

    def test_filters_by_agent(self, isolated_svc):
        self._seed(isolated_svc, "J1", "Jarvis")
        self._seed(isolated_svc, "J2", "Jarvis")
        self._seed(isolated_svc, "I1", "IoT")

        all_rows = isolated_svc.list_sessions()
        assert all_rows["total"] == 3

        jarvis = isolated_svc.list_sessions(agent_name="Jarvis")
        assert jarvis["total"] == 2
        assert {r["agent_name"] for r in jarvis["items"]} == {"Jarvis"}

        iot = isolated_svc.list_sessions(agent_name="IoT")
        assert iot["total"] == 1
        assert iot["items"][0]["agent_name"] == "IoT"

    def test_pagination_window_with_stable_total(self, isolated_svc):
        for i in range(5):
            self._seed(isolated_svc, f"C{i}", "Jarvis")

        first = isolated_svc.list_sessions(agent_name="Jarvis", limit=2, offset=0)
        assert first["total"] == 5
        assert len(first["items"]) == 2

        last = isolated_svc.list_sessions(agent_name="Jarvis", limit=2, offset=4)
        assert last["total"] == 5
        assert len(last["items"]) == 1

        # total stays the filtered count even when the offset is past the end.
        beyond = isolated_svc.list_sessions(agent_name="Jarvis", limit=2, offset=10)
        assert beyond["total"] == 5
        assert beyond["items"] == []

    def test_pages_do_not_overlap(self, isolated_svc):
        for i in range(5):
            self._seed(isolated_svc, f"C{i}", "Jarvis")
        p1 = isolated_svc.list_sessions(agent_name="Jarvis", limit=2, offset=0)
        p2 = isolated_svc.list_sessions(agent_name="Jarvis", limit=2, offset=2)
        ids = [r["id"] for r in p1["items"]] + [r["id"] for r in p2["items"]]
        assert len(ids) == len(set(ids))  # no id appears on two pages

    @pytest.fixture()
    def isolated_svc(self, tmp_path):
        svc = SessionService()
        svc._manager = SessionManager(cwd=tmp_path)
        return svc


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
    placeholder assistant message into ``message_history``; that phantom
    pair must not survive. The rollback path must:

      * POP the half-turn entries from ``agent.message_history`` BEFORE
        saving, so neither the phantom user nor the blank assistant lands
        on disk.
      * STILL stamp ``primary_agent`` metadata + ``save_history`` — the
        history is already clean (popped) so nothing phantom is persisted,
        and the metadata is required for the session to remain VISIBLE in
        list_sessions. (Regression 2026-06-01: returning early here skipped
        the stamp, so a session whose first turn was cancelled — e.g. a long
        crawl turn the user navigated away from — got no primary_agent and
        was hidden → "conversation disappeared".)
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
        # The fix must have popped both stale entries BEFORE save:
        assert fake_agent.message_history == [], (
            "agent.message_history retained the cancelled half-turn — "
            "the rollback should pop both the user and the empty "
            "assistant entries"
        )
        # save_history IS called now — but on the already-cleaned history,
        # so no phantom pair lands on disk. The call is what persists the
        # primary_agent metadata that keeps the session visible.
        assert len(save_history_calls) == 1, (
            "save_history must run once on a cancelled first turn so the "
            "primary_agent stamp is persisted (else the session is hidden "
            "from list_sessions — the 'conversation disappeared' bug)"
        )
        # The stamp itself must be present.
        assert fake_session.info.metadata.get("primary_agent") == "Jarvis", (
            "primary_agent was not stamped on the cancelled turn — the "
            "session would be hidden from the conversation list"
        )
