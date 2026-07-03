"""SSoT for per-chapter TTS status (services.story_audio_index).

Real in-memory SQLite + real reconcile against a temp story tree / audio cache.
The only thing faked is the filesystem location (tmp dirs) — the read/hash/status
logic and the priority queries run for real.
"""
import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, StoryChapter, StoryProgress
from helpers.audio_cache import get_content_hash
from helpers.text_processing import clean_text_for_tts
from services import story_audio_index as sai


@pytest.fixture()
def env(tmp_path, monkeypatch):
    stories = tmp_path / "stories"
    audio = tmp_path / "audio"
    stories.mkdir()
    audio.mkdir()
    monkeypatch.setattr(sai, "STORIES_DIR", str(stories))
    monkeypatch.setattr(sai, "AUDIO_CACHE_DIR", str(audio))
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return types.SimpleNamespace(stories=stories, audio=audio, Session=Session)


def _chapter(env, story, fname, text="hello world"):
    d = env.stories / story
    d.mkdir(exist_ok=True)
    (d / fname).write_text(text, encoding="utf-8")


def _generate(env, text="hello world"):
    """Create the mp3 the reconcile will see as 'ready' (same hash the code uses)."""
    h = get_content_hash(clean_text_for_tts(text))
    (env.audio / f"{h}.mp3").write_bytes(b"MP3")
    return h


# ── reconcile: disk → table ─────────────────────────────────────────────────
def test_reconcile_builds_rows_and_status_from_disk(env):
    _chapter(env, "storyA", "01.txt", "chapter one text")
    _chapter(env, "storyA", "02.txt", "chapter two text")
    _generate(env, "chapter one text")   # 01 already has audio

    db = env.Session()
    sai.reconcile(db)
    rows = {r.chapter_file: r for r in db.query(StoryChapter).all()}
    assert set(rows) == {"01.txt", "02.txt"}
    assert rows["01.txt"].status == "ready" and rows["01.txt"].chapter_num == 0
    assert rows["02.txt"].status == "pending" and rows["02.txt"].chapter_num == 1
    assert sai.status_counts(db) == {"total": 2, "done": 1, "pending": 1}


def test_reconcile_skips_unchanged_without_rehash(env, monkeypatch):
    _chapter(env, "storyA", "01.txt")
    db = env.Session()
    sai.reconcile(db)                       # first pass reads+hashes
    calls = {"n": 0}
    real = sai._hash_and_status
    monkeypatch.setattr(sai, "_hash_and_status",
                        lambda s, c: calls.__setitem__("n", calls["n"] + 1) or real(s, c))
    sai.reconcile(db)                       # same fingerprint → must NOT re-hash
    assert calls["n"] == 0


def test_reconcile_detects_new_and_removed(env):
    _chapter(env, "storyA", "01.txt")
    db = env.Session()
    sai.reconcile(db)
    _chapter(env, "storyA", "02.txt")       # added
    (env.stories / "storyA" / "01.txt").unlink()  # removed
    sai.reconcile(db)
    files = {r.chapter_file for r in db.query(StoryChapter).all()}
    assert files == {"02.txt"}


# ── priority queue ──────────────────────────────────────────────────────────
def test_next_task_p0_is_active_story_next_chapter(env):
    for i, t in [("01.txt", "t1"), ("02.txt", "t2"), ("03.txt", "t3")]:
        _chapter(env, "storyA", i, t)
    _generate(env, "t1")                    # 01 ready; 02,03 pending
    db = env.Session()
    sai.reconcile(db)
    db.add(StoryProgress(story_title="storyA", last_chapter_num=0,
                         last_chapter_file="01.txt", last_played_at=100.0))
    db.commit()
    task = sai.next_task(db)
    assert task == {"story_title": "storyA", "chapter_file": "02.txt", "priority": 0}


def test_next_task_p2_then_p3(env):
    # storyA fully generated; storyB has no audio → P2 picks storyB ch1
    _chapter(env, "storyA", "01.txt", "a1")
    _generate(env, "a1")
    _chapter(env, "storyB", "01.txt", "b1")
    _chapter(env, "storyB", "02.txt", "b2")
    db = env.Session()
    sai.reconcile(db)
    task = sai.next_task(db)
    assert task["story_title"] == "storyB" and task["chapter_file"] == "01.txt"
    assert task["priority"] == 2
    # once storyB has some audio, remaining falls to P3
    sai.mark_ready(db, "storyB", "01.txt")
    task = sai.next_task(db)
    assert task["chapter_file"] == "02.txt" and task["priority"] == 3


def test_next_task_none_when_all_ready(env):
    _chapter(env, "storyA", "01.txt", "a1")
    _generate(env, "a1")
    db = env.Session()
    sai.reconcile(db)
    assert sai.next_task(db) is None


# ── status transitions ──────────────────────────────────────────────────────
def test_mark_ready_and_pending_by_hash(env):
    _chapter(env, "storyA", "01.txt", "a1")
    db = env.Session()
    sai.reconcile(db)
    row = db.get(StoryChapter, ("storyA", "01.txt"))
    assert row.status == "pending"
    h = get_content_hash(clean_text_for_tts("a1"))
    sai.mark_ready(db, "storyA", "01.txt", h)
    assert db.get(StoryChapter, ("storyA", "01.txt")).status == "ready"
    # eviction of that hash flips it back
    n = sai.mark_pending_by_hash(db, h)
    assert n == 1
    assert db.get(StoryChapter, ("storyA", "01.txt")).status == "pending"


def test_preview_queue_orders_by_priority(env):
    for i, t in [("01.txt", "t1"), ("02.txt", "t2"), ("03.txt", "t3")]:
        _chapter(env, "storyA", i, t)
    _generate(env, "t1")
    db = env.Session()
    sai.reconcile(db)
    db.add(StoryProgress(story_title="storyA", last_chapter_num=0,
                         last_chapter_file="01.txt", last_played_at=100.0))
    db.commit()
    q = sai.preview_queue(db, limit=10)
    assert [(x["chapter_file"], x["priority"]) for x in q] == [("02.txt", 0), ("03.txt", 1)]
