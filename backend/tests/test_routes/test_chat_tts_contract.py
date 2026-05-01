"""Contract test: TTS rule prefix is applied at exactly the right layers.

Why static-source checks instead of route integration tests: the chat
endpoints depend on `agent_app` (a fast-agent runtime, MCP servers, OAuth
state, etc.) which is heavy to spin up in unit tests. The only behavior
we need to lock in here is "every chat endpoint wraps the user message
with prepend_tts_rules, and the cron scheduler does NOT". Source-level
assertions catch any future refactor that moves the call to a wrong layer.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestChatRoutesApplyPrefix:
    def test_chat_module_imports_prepender(self):
        src = _read("routes/chat.py")
        assert "from helpers.tts_rules import prepend_tts_rules" in src

    def test_post_chat_wraps_user_message(self):
        src = _read("routes/chat.py")
        assert "prepend_tts_rules(request.message)" in src

    def test_chat_stream_wraps_user_message(self):
        src = _read("routes/chat.py")
        assert "prepend_tts_rules(message)" in src

    def test_chat_audio_wraps_transcribed_text(self):
        src = _read("routes/chat.py")
        assert "prepend_tts_rules(text)" in src

    def test_every_resume_and_send_in_chat_uses_prefix(self):
        src = _read("routes/chat.py")
        # Each call to session_service.resume_and_send in this file must
        # pass a prepend_tts_rules(...) wrapper as the message arg. If a
        # future endpoint forgets it, the chat path will silently regress.
        lines = src.splitlines()
        for i, line in enumerate(lines):
            if "session_service.resume_and_send" in line and "agent_app" in line:
                # The agent_app + message arg is on either the same line
                # or the next one — scan a small window.
                window = "\n".join(lines[i:i + 4])
                assert "prepend_tts_rules" in window, (
                    f"resume_and_send near line {i + 1} of routes/chat.py "
                    f"is missing the TTS rule prefix"
                )


class TestCronSchedulerDoesNotApplyPrefix:
    def test_cron_module_does_not_import_prepender(self):
        src = _read("services/cron_scheduler.py")
        assert "tts_rules" not in src
        assert "prepend_tts_rules" not in src

    def test_execute_agent_turn_calls_resume_and_send_directly(self):
        # The scheduled-task path must hand the raw payload to the agent —
        # adding the TTS prefix here would re-introduce the leak we just
        # fixed (TTS-style spelling in markdown notifications).
        src = _read("services/cron_scheduler.py")
        idx = src.find("async def _execute_agent_turn")
        assert idx != -1
        body = src[idx:idx + 1500]
        assert "resume_and_send" in body
        assert "prepend_tts_rules" not in body


class TestAgentInstructionDoesNotEmbedRule:
    def test_jarvis_instruction_no_longer_carries_tts_block(self):
        src = _read("agent.py")
        # The legacy block lived inside Jarvis's instruction string. After
        # B1 it is in helpers/tts_rules.py only — asserting the unique
        # opening phrase is gone catches accidental re-introduction.
        assert "QUY TẮC CHUẨN HÓA OUTPUT CHO TTS" not in src
        # And the markdown-banning rule 5 must not come back either.
        assert "Tránh dùng quá nhiều ký tự đặc biệt" not in src
