"""Unit tests for helpers/crawl_markers.py — the single home for site-language
crawl heuristics (CLAUDE.md §7).

CHAPTER_TITLE_SUFFIX_RE intentionally unifies two formerly-different call-site
regexes (crawl_poller story_title vs story_name): both now strip "Quyển N" and
tolerate zero spaces before the number. These tests pin that shared behavior.
"""
import pytest

from helpers.crawl_markers import (
    CHAPTER_HEADING_RE,
    CHAPTER_ONE_RE,
    CHAPTER_TITLE_SUFFIX_RE,
    PRELUDE_RE,
    SITE_TITLE_SUFFIX_RE,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Original story_title behavior (Chương/Chapter with space).
        ("Đấu Phá Thương Khung Chương 12: Tiêu Viêm", "Đấu Phá Thương Khung"),
        ("My Story Chapter 3 - The End", "My Story"),
        # Unified behavior: "Quyển N" stripped at BOTH call sites now.
        ("Tiên Nghịch Quyển 2 Chương 5", "Tiên Nghịch"),
        # Unified behavior: zero spaces before the number tolerated.
        ("Truyện Hay Chương12: Mở Màn", "Truyện Hay"),
        # Case-insensitive.
        ("Foo chương 7", "Foo"),
        # No suffix → unchanged.
        ("Plain Title", "Plain Title"),
    ],
)
def test_chapter_title_suffix_strip(raw, expected):
    assert CHAPTER_TITLE_SUFFIX_RE.sub("", raw).strip() == expected


def test_chapter_heading_matches_vi_and_en():
    assert CHAPTER_HEADING_RE.search("Chương 15: abc")
    assert CHAPTER_HEADING_RE.search("chapter 2")
    assert CHAPTER_HEADING_RE.search("Hồi 3")
    assert not CHAPTER_HEADING_RE.search("Chương mở đầu")  # no number


def test_chapter_one_anchor():
    assert CHAPTER_ONE_RE.search("Chương 1: Khởi đầu")
    assert CHAPTER_ONE_RE.search("Chapter 01")
    assert not CHAPTER_ONE_RE.search("Chương 10: abc")  # 10 is not chapter one


def test_prelude_sections_count_as_story_start():
    for text in ("Mở đầu", "Văn án", "Prologue", "Preface"):
        assert PRELUDE_RE.search(text)


def test_site_title_suffix_strip():
    assert SITE_TITLE_SUFFIX_RE.sub("", "Tên Truyện - Truyện ABC").strip() == "Tên Truyện"
    assert SITE_TITLE_SUFFIX_RE.sub("", "No Suffix Here").strip() == "No Suffix Here"
