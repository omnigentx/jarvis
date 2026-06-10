"""Locale marker data for story-crawl heuristics.

Site-language words (chapter headings, next-page link text, …) live HERE
only — never inline at call sites (CLAUDE.md §7). Adding a locale =
extending these tuples/patterns in one place.
"""
import re

# Chapter heading containing any number — chapter-list / heading detection.
CHAPTER_HEADING_RE = re.compile(r"(chương|chapter|hồi)\s+\d+", re.I)
# href slug equivalent of the above.
CHAPTER_HREF_RE = re.compile(r"chapter|chuong", re.I)

# Heading that marks chapter #1 (used as the story-start anchor).
CHAPTER_ONE_RE = re.compile(r"(chương|chapter)\s+0*1(\s+:|$|\D)", re.I)

# Pre-chapter sections that also count as the story opening.
PRELUDE_RE = re.compile(r"(mở đầu|văn án|prologue|preface)", re.I)

# "<Story name> Chương 12: …" → strip the chapter suffix to get the name.
CHAPTER_TITLE_SUFFIX_RE = re.compile(r"\s*(Chương|Chapter|Quyển)\s*\d+.*$", re.I)

# Anchor text of next-page / next-chapter links.
NEXT_PAGE_LINK_RE = re.compile(r"(Trang tiếp|Tiếp|Next|Sau|»|›)", re.I)
# Lowercase substrings matched against a.get_text().lower().
NEXT_CHAPTER_PHRASES = ("chương sau", "tiếp", "next", "chap sau")

# Site-name suffix on page <title> (e.g. "Foo - Truyện ABC").
SITE_TITLE_SUFFIX_RE = re.compile(r"\s*-\s*Truyện.*$", re.I)
