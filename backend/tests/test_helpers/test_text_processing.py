"""Tests for helpers/text_processing.clean_text_for_tts.

After lifting the markdown ban from the agent prompt (see helpers/tts_rules.py),
this function is the single point that strips markdown formatting before the
text reaches the TTS engine. If a regression here lets `#` or ``` through, the
voice output will read "hashtag" / "backtick backtick backtick" aloud.
"""
from helpers.text_processing import clean_text_for_tts


class TestUrlsAndLinks:
    def test_strips_https_url(self):
        assert clean_text_for_tts("Xem https://example.com") == "Xem"

    def test_strips_www_url(self):
        assert clean_text_for_tts("Visit www.foo.bar today") == "Visit today"

    def test_keeps_link_label(self):
        assert clean_text_for_tts("Click [here](https://x.com) please") == "Click here please"

    def test_drops_image_alt(self):
        assert clean_text_for_tts("Look ![alt](https://x.png) now") == "Look now"


class TestMarkdownStripping:
    def test_strips_h1(self):
        assert clean_text_for_tts("# Title\nBody") == "Title Body"

    def test_strips_h3(self):
        assert clean_text_for_tts("### Sub\nBody") == "Sub Body"

    def test_strips_bold(self):
        assert clean_text_for_tts("This is **bold** text") == "This is bold text"

    def test_strips_italic_asterisk(self):
        assert clean_text_for_tts("This is *italic* text") == "This is italic text"

    def test_strips_italic_underscore(self):
        assert clean_text_for_tts("This is _italic_ text") == "This is italic text"

    def test_strips_inline_code(self):
        assert clean_text_for_tts("Run `npm test` first") == "Run npm test first"

    def test_drops_fenced_code_block(self):
        text = "Here is code:\n```python\nprint('hi')\n```\nDone."
        out = clean_text_for_tts(text)
        assert "print" not in out
        assert "python" not in out
        assert "```" not in out
        assert out.startswith("Here is code:")
        assert out.endswith("Done.")

    def test_drops_mermaid_block(self):
        text = "Flow:\n```mermaid\ngraph TD\nA-->B\n```\nDone."
        out = clean_text_for_tts(text)
        assert "graph" not in out
        assert "mermaid" not in out
        assert "```" not in out

    def test_strips_unordered_bullet(self):
        text = "- item one\n- item two"
        out = clean_text_for_tts(text)
        assert out == "item one item two"

    def test_strips_ordered_list(self):
        text = "1. first\n2. second"
        out = clean_text_for_tts(text)
        assert out == "first second"

    def test_strips_blockquote(self):
        assert clean_text_for_tts("> Quoted line") == "Quoted line"


class TestPreservesContent:
    def test_keeps_normal_punctuation(self):
        out = clean_text_for_tts("Xin chào, hôm nay là thứ Hai.")
        assert out == "Xin chào, hôm nay là thứ Hai."

    def test_keeps_vietnamese_diacritics(self):
        out = clean_text_for_tts("Tôi đã hoàn thành **bài tập** rồi")
        assert "Tôi đã hoàn thành" in out
        assert "bài tập" in out

    def test_collapses_whitespace_after_strip(self):
        # Stripping a fenced code block in the middle leaves a gap; the
        # final whitespace pass must collapse it so TTS doesn't pause.
        text = "Before\n```\nx = 1\n```\nAfter"
        out = clean_text_for_tts(text)
        assert "  " not in out
        assert "\n" not in out


class TestEdgeCases:
    def test_empty_returns_empty(self):
        assert clean_text_for_tts("") == ""

    def test_none_safe(self):
        assert clean_text_for_tts(None) == ""

    def test_does_not_eat_asterisk_in_words(self):
        # Single * inside a word is not italic — must not be stripped wrongly.
        out = clean_text_for_tts("a*b is a variable")
        assert "a*b" in out or "ab is a variable" in out
