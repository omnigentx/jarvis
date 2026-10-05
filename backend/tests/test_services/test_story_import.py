"""Synthetic chapter data only: no user's local story is included."""
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, StoryMeta
from services.story_import import import_story, ImportProblem


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    return tmp_path / "stories", sessionmaker(bind=engine)


def chapter(name, body=b"Synthetic chapter text."):
    return SimpleNamespace(filename=name, file=BytesIO(body))


def run(env, files, key=None, title="Synthetic story"):
    root, session = env
    return import_story(title, key or str(uuid.uuid4()), files, root=root, session_factory=session)


def test_import_preserves_preview_order_bom_and_safe_unique_names(env):
    result = run(env, [chapter("folder/2_First.txt", b"\xef\xbb\xbfFirst synthetic text"),
                       chapter("folder/2_Second.txt", "Second synthetic text".encode())])
    folder = env[0] / result['id']
    paths = sorted(folder.glob('*.txt'))
    assert len(paths) == result['chapters'] == 2
    assert paths[0].name.startswith('000001_')
    assert paths[1].name.startswith('000002_')
    assert paths[0].read_text() == "First synthetic text"
    with env[1]() as db:
        assert db.get(StoryMeta, result['id']).title == 'Synthetic story'
    assert not list((env[0].parent / '.story-imports').glob('stage-*'))


@pytest.mark.parametrize('name,body,code', [
    ('../escape.txt', b'hello', 'unsafe_path'),
    ('root/../escape.txt', b'hello', 'unsafe_path'),
    ('/absolute.txt', b'hello', 'unsafe_path'),
    ('root/child/file.txt', b'hello', 'nested_folder'),
    ('empty.txt', b'  \n', 'empty_file'),
    ('bad.txt', b'\xff\xfeinvalid', 'invalid_encoding'),
    ('binary.txt', b'a\x00b', 'binary_file'),
    ('wrong.html', b'hello', 'unsupported_file'),
])
def test_invalid_file_rolls_back_everything(env, name, body, code):
    with pytest.raises(ImportProblem) as error:
        run(env, [chapter('good.txt'), chapter(name, body)])
    assert error.value.code == code
    assert not list(env[0].glob('import-*'))
    with env[1]() as db:
        assert db.query(StoryMeta).count() == 0
    assert not list((env[0].parent / '.story-imports').glob('stage-*'))


def test_retry_is_idempotent_and_changed_payload_is_conflict(env):
    key = str(uuid.uuid4())
    first = run(env, [chapter('one.txt')], key)
    assert run(env, [chapter('one.txt')], key)['id'] == first['id']
    with pytest.raises(ImportProblem) as error:
        run(env, [chapter('one.txt', b'Changed content')], key)
    assert error.value.status == 409
    with env[1]() as db:
        assert db.query(StoryMeta).count() == 1


def test_same_key_concurrent_requests_publish_once(env):
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: run(env, [chapter('one.txt')], key), range(2)))
    assert results[0]['id'] == results[1]['id']
    with env[1]() as db:
        assert db.query(StoryMeta).count() == 1


def test_database_commit_failure_removes_published_files(env, monkeypatch):
    factory = env[1]
    class BrokenSession:
        def __enter__(self):
            self.db = factory()
            self.db.commit = lambda: (_ for _ in ()).throw(RuntimeError('synthetic DB failure'))
            return self.db
        def __exit__(self, *args):
            self.db.close()
    with pytest.raises(RuntimeError):
        import_story('Synthetic story', str(uuid.uuid4()), [chapter('one.txt')], root=env[0], session_factory=BrokenSession)
    assert not list(env[0].glob('import-*'))


@pytest.mark.parametrize('title,key,files,code', [
    (' ', str(uuid.uuid4()), [chapter('a.txt')], 'invalid_title'),
    ('A' * 256, str(uuid.uuid4()), [chapter('a.txt')], 'invalid_title'),
    ('Synthetic', 'bad-key', [chapter('a.txt')], 'invalid_request_id'),
    ('Synthetic', str(uuid.uuid4()), [], 'file_count'),
    ('Synthetic', str(uuid.uuid4()), [chapter('a.txt'), chapter('a.txt')], 'duplicate_file'),
    ('Synthetic', str(uuid.uuid4()), [chapter('a.txt', b'A' * (2 * 1024 * 1024 + 1))], 'file_too_large'),
])
def test_validation_bounds(env, title, key, files, code):
    with pytest.raises(ImportProblem) as exc:
        run(env, files, key=key, title=title)
    assert exc.value.code == code
    assert not list(env[0].glob('import-*'))


def test_retry_repairs_metadata_after_crash(env):
    key = str(uuid.uuid4())
    result = run(env, [chapter('a.txt')], key)
    with env[1]() as db:
        db.delete(db.get(StoryMeta, result['id']))
        db.commit()
    assert run(env, [chapter('a.txt')], key) == result
    with env[1]() as db:
        assert db.get(StoryMeta, result['id']).chapter_count == 1


def test_expired_inactive_staging_cleanup_keeps_active_and_fresh(tmp_path):
    import fcntl
    import os
    from services.story_import import cleanup_staging, STAGE_TTL_SECONDS
    for name in ('stage-expired', 'stage-active', 'stage-fresh'):
        (tmp_path / name).mkdir()
    for name in ('stage-expired', 'stage-active'):
        os.utime(tmp_path / name, (1, 1))
    with (tmp_path / 'stage-active/.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        os.utime(tmp_path / 'stage-active', (1, 1))
        cleanup_staging(tmp_path, now=STAGE_TTL_SECONDS + 10)
        assert not (tmp_path / 'stage-expired').exists()
        assert (tmp_path / 'stage-active').exists()
        assert (tmp_path / 'stage-fresh').exists()


def test_bulk_folder_with_489_synthetic_chapters(env):
    files = [chapter(f'folder/{index}_Chapter.txt', b'Synthetic chapter.\n' * 1200) for index in range(489)]
    result = run(env, files)
    assert result['chapters'] == 489
    assert len(list((env[0] / result['id']).glob('*.txt'))) == 489
    assert not list((env[0].parent / '.story-imports').iterdir())


def test_total_upload_limit_rolls_back(env):
    files = [chapter(f'{index}.txt', b'A' * (2 * 1024 * 1024)) for index in range(7)]
    with pytest.raises(ImportProblem) as exc:
        run(env, files)
    assert exc.value.code == 'total_too_large'
    assert not list(env[0].glob('import-*'))
