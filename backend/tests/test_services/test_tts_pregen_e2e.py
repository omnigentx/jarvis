"""E2E for the TTS pre-gen queue through the REAL TTSPreGenJob.

Drives the actual production methods — construction → rescan/reconcile,
``get_next_task`` (real ``asyncio.to_thread`` + reconcile + SSoT query),
``execute_task`` (its real already-generated fast path → status write),
``get_status`` — against a real in-memory SQLite + a temp story tree / audio
cache. The ONLY thing avoided is the edge_tts network call, via the
already-on-disk short-circuit; the queue/status subsystem under change runs for
real (no mocking of it).
"""
import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, StoryChapter
from helpers.audio_cache import get_content_hash
from helpers.text_processing import clean_text_for_tts


@pytest.fixture()
def job_env(tmp_path, monkeypatch):
    import helpers.audio_cache as ac
    import services.story_audio_index as sai
    import services.tts_pregen_job as tpj

    stories = tmp_path / "stories"
    audio = tmp_path / "audio"
    stories.mkdir()
    audio.mkdir()
    # Point every module that resolves the story tree / audio cache at the temp dirs.
    for mod, attr in [(sai, "STORIES_DIR"), (sai, "AUDIO_CACHE_DIR"),
                      (tpj, "STORIES_DIR"), (tpj, "AUDIO_CACHE_DIR"),
                      (ac, "AUDIO_CACHE_DIR")]:
        monkeypatch.setattr(mod, attr, str(stories if attr == "STORIES_DIR" else audio))

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(tpj, "get_db_session", lambda: Session())
    return types.SimpleNamespace(stories=stories, audio=audio, Session=Session, tpj=tpj)


def _chapter(env, story, fname, text):
    d = env.stories / story
    d.mkdir(exist_ok=True)
    (d / fname).write_text(text, encoding="utf-8")


def _write_mp3(env, text):
    """Simulate a generated chapter: the mp3 the job's fast path will find."""
    h = get_content_hash(clean_text_for_tts(text))
    (env.audio / f"{h}.mp3").write_bytes(b"MP3")
    return h


async def test_e2e_pregen_queue_flow(job_env):
    env = job_env
    _chapter(env, "storyA", "01.txt", "alpha chapter one")
    _chapter(env, "storyA", "02.txt", "alpha chapter two")

    job = env.tpj.TTSPreGenJob(types.SimpleNamespace())   # __init__ → rescan → reconcile

    # Both chapters pending → status endpoint reflects the SSoT.
    assert job.get_status()["total"] == 2
    assert job.get_status()["done"] == 0

    # get_next_task drives the real to_thread reconcile + indexed SSoT query.
    task = await job.get_next_task()
    assert task["story_title"] == "storyA" and task["chapter_file"] == "01.txt"

    # "Generate" ch01: its mp3 now exists → execute_task takes the already-done
    # fast path (no network) and flips the SSoT status to ready.
    _write_mp3(env, "alpha chapter one")
    assert await job.execute_task(dict(task)) is True

    db = env.Session()
    assert db.get(StoryChapter, ("storyA", "01.txt")).status == "ready"
    assert job.get_status()["done"] == 1

    # Queue advances to the next pending chapter.
    task2 = await job.get_next_task()
    assert task2["chapter_file"] == "02.txt"

    # Generate the last one → queue drains to empty.
    _write_mp3(env, "alpha chapter two")
    await job.execute_task(dict(task2))
    assert await job.get_next_task() is None
    assert job.get_status()["done"] == 2


async def test_e2e_new_story_is_picked_up_after_reconcile(job_env):
    env = job_env
    _chapter(env, "storyA", "01.txt", "alpha one")
    _write_mp3(env, "alpha one")                         # storyA already generated
    job = env.tpj.TTSPreGenJob(types.SimpleNamespace())  # reconcile → storyA/01 ready
    assert env.Session().get(StoryChapter, ("storyA", "01.txt")).status == "ready"
    assert await job.get_next_task() is None             # nothing pending

    # A new story appears on disk; the fingerprint changes → the next tick's
    # off-loop reconcile brings it into the SSoT and the queue serves it.
    _chapter(env, "storyB", "01.txt", "beta one")
    task = await job.get_next_task()
    assert task["story_title"] == "storyB" and task["chapter_file"] == "01.txt"
