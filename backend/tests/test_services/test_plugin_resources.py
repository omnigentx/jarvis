"""Bound disk/context growth and recover abandoned runtime downloads safely."""

import fcntl
import os
import time

import pytest
from sqlalchemy import create_engine

from services.plugins.lifecycle import PluginLifecycle, PluginStateError
from services.plugins.package import PackageError, inspect_package


def test_skill_metadata_cannot_inflate_every_model_call(tmp_path):
    (tmp_path / "plugin.json").write_text('{"name":"budget"}')
    (tmp_path / "SKILL.md").write_text(
        "---\nname: budget\ndescription: " + "x" * 4097 + "\n---\nBody"
    )
    with pytest.raises(PackageError, match="budget"):
        inspect_package(tmp_path)


def test_staging_rejects_candidate_quota_without_deleting_live_bytes(
    tmp_path, monkeypatch
):
    from services.plugins import resources

    monkeypatch.setattr(resources, "MAX_CANDIDATES", 1)
    engine = create_engine("sqlite:///" + str(tmp_path / "db"))
    lifecycle = PluginLifecycle(engine, tmp_path / "plugins")
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.json").write_text('{"name":"budget"}')
    (source / "SKILL.md").write_text("---\nname: budget\ndescription: Check\n---\nBody")
    first = lifecycle.stage(
        source, repo="openai/plugins", commit="a" * 40, subdirectory=""
    )
    with pytest.raises(PluginStateError, match="quota"):
        lifecycle.stage(source, repo="openai/plugins", commit="b" * 40, subdirectory="")
    assert lifecycle.package_path(first["id"]).exists()
    engine.dispose()


def test_orphan_cleanup_respects_age_and_live_download_lease(tmp_path):
    from services.plugins.resources import cleanup_orphans

    old = tmp_path / "download-abandoned"
    leased = tmp_path / "download-in-use"
    young = tmp_path / "download-new"
    for path in (old, leased, young):
        path.mkdir()
    past = time.time() - 90000
    os.utime(old, (past, past))
    with (leased / ".lease").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        os.utime(leased, (past, past))
        assert cleanup_orphans(tmp_path, set()) == [old.name]
        assert leased.exists() and young.exists()
