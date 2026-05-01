"""Tests for helpers/tts_rules.py — single source for the TTS prompt prefix.

The rule must be applied ONLY where the agent's reply will be read aloud
(chat endpoints). Cron / scheduler / notification paths must not import or
call this — those tests live next to the cron scheduler to assert the
negative.
"""
import pytest

from helpers.tts_rules import TTS_OUTPUT_RULES_VI, prepend_tts_rules


class TestRuleConstant:
    def test_rule_mentions_units(self):
        assert "ki-lô-mét" in TTS_OUTPUT_RULES_VI
        assert "phần trăm" in TTS_OUTPUT_RULES_VI

    def test_rule_mentions_decimal_handling(self):
        assert "phẩy" in TTS_OUTPUT_RULES_VI

    def test_rule_does_not_forbid_markdown_chars(self):
        # The legacy rule 5 banned #, *, -, _ which conflicts with the
        # markdown UI renderer. Make sure the new rule does NOT carry
        # that constraint forward.
        assert "Tránh dùng" not in TTS_OUTPUT_RULES_VI
        assert "ký tự đặc biệt" not in TTS_OUTPUT_RULES_VI


class TestPrependBehavior:
    def test_prepends_rule_before_message(self):
        out = prepend_tts_rules("Hôm nay thời tiết thế nào?")
        assert out.startswith("QUY TẮC OUTPUT CHO TTS")
        assert out.endswith("Hôm nay thời tiết thế nào?")

    def test_keeps_separator_between_rule_and_message(self):
        out = prepend_tts_rules("test")
        # Separator must isolate the rule block from the user message so
        # the agent doesn't merge them into one sentence.
        assert "\n---\n" in out

    def test_handles_empty_message(self):
        out = prepend_tts_rules("")
        assert TTS_OUTPUT_RULES_VI in out

    def test_handles_multiline_message(self):
        msg = "Dòng 1\nDòng 2\nDòng 3"
        out = prepend_tts_rules(msg)
        assert out.endswith(msg)

    def test_does_not_mutate_input(self):
        msg = "original"
        prepend_tts_rules(msg)
        assert msg == "original"
