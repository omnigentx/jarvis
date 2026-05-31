"""Unit tests for helpers.path_safety.

Pins the rejection set so regressions are caught loudly. Real traversal
attempts at the four call sites (crawl_poller, story_server, story_reader,
tts_pregen_job) feed through this helper; the integration assertions live
near those call sites.
"""

import os
from pathlib import Path

import pytest

from helpers.path_safety import safe_story_path


def test_happy_path(tmp_path):
    base = tmp_path / "stories"
    base.mkdir()
    p = safe_story_path(base, "MyStory", "chapter1.txt")
    assert p == (base / "MyStory" / "chapter1.txt").resolve()


def test_preserves_vietnamese_unicode(tmp_path):
    base = tmp_path / "stories"
    base.mkdir()
    p = safe_story_path(base, "Truyện Hay", "Chương 1.txt")
    assert "Truyện Hay" in str(p)
    assert "Chương 1.txt" in str(p)


@pytest.mark.parametrize("bad", [".", "..", "", "/", "\\", "a/b", "a\\b", "\x00"])
def test_rejects_separators_and_traversal(tmp_path, bad):
    base = tmp_path / "stories"
    base.mkdir()
    with pytest.raises(ValueError):
        safe_story_path(base, bad)


def test_rejects_traversal_via_join(tmp_path):
    # Two valid-looking parts that, when joined, escape the sandbox via "..".
    base = tmp_path / "stories"
    base.mkdir()
    # ".." is caught at the per-part check, not at the realpath check —
    # both layers exist; this asserts the per-part layer fires first.
    with pytest.raises(ValueError):
        safe_story_path(base, "story", "..")


def test_rejects_symlink_escape(tmp_path):
    base = tmp_path / "stories"
    base.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    # Plant a symlink that goes outside the sandbox.
    link = base / "evil"
    os.symlink(secret, link)
    with pytest.raises(ValueError):
        safe_story_path(base, "evil")


def test_returns_resolved_path(tmp_path):
    # base supplied as str → still returns a resolved Path.
    base = tmp_path / "stories"
    base.mkdir()
    p = safe_story_path(str(base), "OK")
    assert isinstance(p, Path)
    assert p.is_absolute()
