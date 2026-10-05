"""Bounded, retry-safe folder import. Uploaded text never enters LLMs or logs."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from core.database import StoryMeta, get_db_session

MAX_FILES = 1000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 12 * 1024 * 1024
MAX_REQUEST_BYTES = 14 * 1024 * 1024
STAGE_TTL_SECONDS = 24 * 3600


class ImportProblem(ValueError):
    def __init__(self, code: str, status: int = 422, index: int | None = None):
        super().__init__(code)
        self.code, self.status, self.index = code, status, index


def _source_name(filename: str, index: int) -> str:
    # Browser folder uploads carry root/name; only direct children are allowed.
    path = PurePosixPath(filename)
    if (not filename or '\\' in filename or path.is_absolute()
            or any(p in {'', '.', '..'} for p in filename.split('/'))
            or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
        raise ImportProblem('unsafe_path', index=index)
    if len(path.parts) > 2:
        raise ImportProblem('nested_folder', index=index)
    if path.suffix.lower() != '.txt':
        raise ImportProblem('unsupported_file', index=index)
    return str(path)


def _stored_name(filename: str, index: int) -> str:
    name = PurePosixPath(filename).stem
    name = re.sub(r'^\d+[\s_.-]*', '', name)
    name = re.sub(r'[^\w\s.-]', '_', unicodedata.normalize('NFC', name)).strip(' .')
    # Keep below common filesystem byte limits even with multibyte chapter names.
    name = name.encode('utf-8')[:180].decode('utf-8', errors='ignore') or 'chapter'
    return f'{index:06d}_{name}.txt'


def cleanup_staging(staging_root: Path, now: float | None = None) -> None:
    """Sweep only expired inactive owned stages, including abrupt-crash leftovers."""
    cutoff = (time.time() if now is None else now) - STAGE_TTL_SECONDS
    for stage in staging_root.glob('stage-*'):
        try:
            if stage.is_symlink() or not stage.is_dir() or stage.stat().st_mtime >= cutoff:
                continue
            with (stage / '.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                shutil.rmtree(stage)
        except (BlockingIOError, FileNotFoundError):
            continue


def _publish(stage: Path, root: Path, story_id: str, manifest: dict,
             session_factory: Callable) -> dict:
    destination = root / story_id
    # One fixed host-owned lock avoids growing a lock file per import and
    # serializes publication/idempotency across Uvicorn workers.
    with (root.parent / '.story-import.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with session_factory() as db:
            existing = db.get(StoryMeta, story_id)
            if destination.exists():
                saved = json.loads((destination / '.import.json').read_text('utf-8'))
                if saved['digest'] != manifest['digest']:
                    raise ImportProblem('retry_conflict', status=409)
                # A crash after atomic rename but before SQLite commit left a
                # complete story. A same-key retry repairs metadata without
                # re-uploading or duplicating chapters.
                if existing is None:
                    db.add(StoryMeta(story_id=story_id, title=saved['title'],
                                     chapter_count=saved['chapters']))
                    db.commit()
                return {'id': story_id, 'title': saved['title'], 'chapters': saved['chapters']}
            if existing is not None:
                raise ImportProblem('retry_conflict', status=409)
            published = False
            try:
                db.add(StoryMeta(story_id=story_id, title=manifest['title'],
                                 chapter_count=manifest['chapters']))
                db.flush()
                os.replace(stage, destination)
                published = True
                db.commit()
            except BaseException:
                db.rollback()
                if published:
                    shutil.rmtree(destination)
                raise
    return {'id': story_id, 'title': manifest['title'], 'chapters': manifest['chapters']}


def import_story(title: str, request_id: str, files: list[Any], *,
                 root: Path = Path('data/stories'), session_factory: Callable = get_db_session,
                 progress: Callable[[int, int], None] | None = None) -> dict:
    title = unicodedata.normalize('NFC', title.strip())
    if not title or len(title) > 255 or any(ord(c) < 32 for c in title):
        raise ImportProblem('invalid_title')
    try:
        key = str(uuid.UUID(request_id))
    except (ValueError, AttributeError):
        raise ImportProblem('invalid_request_id') from None
    if not files or len(files) > MAX_FILES:
        raise ImportProblem('file_count')
    root.mkdir(parents=True, exist_ok=True)
    staging_root = root.parent / '.story-imports'
    staging_root.mkdir(mode=0o700, exist_ok=True)
    cleanup_staging(staging_root)
    stage = Path(tempfile.mkdtemp(prefix='stage-', dir=staging_root))
    digest = hashlib.sha256(title.encode('utf-8'))
    total = 0
    seen = set()
    try:
        with (stage / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for index, upload in enumerate(files, 1):
                source = _source_name(upload.filename, index)
                if source in seen:
                    raise ImportProblem('duplicate_file', index=index)
                seen.add(source)
                body = upload.file.read(MAX_FILE_BYTES + 1)
                if len(body) > MAX_FILE_BYTES:
                    raise ImportProblem('file_too_large', status=413, index=index)
                total += len(body)
                if total > MAX_TOTAL_BYTES:
                    raise ImportProblem('total_too_large', status=413)
                try:
                    text = body.decode('utf-8-sig')
                except UnicodeDecodeError:
                    raise ImportProblem('invalid_encoding', index=index) from None
                if '\x00' in text:
                    raise ImportProblem('binary_file', index=index)
                if not text.strip():
                    raise ImportProblem('empty_file', index=index)
                digest.update(json.dumps([index, source, hashlib.sha256(body).hexdigest()],
                                         ensure_ascii=True).encode())
                (stage / _stored_name(source, index)).write_text(text, encoding='utf-8')
                if progress and (index % 25 == 0 or index == len(files)):
                    progress(index, len(files))
            manifest = {'title': title, 'chapters': len(files), 'digest': digest.hexdigest()}
            (stage / '.import.json').write_text(json.dumps(manifest, ensure_ascii=False), 'utf-8')
            return _publish(stage, root, f'import-{key}', manifest, session_factory)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
