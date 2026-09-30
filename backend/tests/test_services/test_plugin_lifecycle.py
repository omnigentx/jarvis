"""Approval/activation failures must never produce a false Ready state."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from services.plugins.lifecycle import PluginLifecycle, PluginStateError
from sqlalchemy import create_engine


@pytest.fixture
def lifecycle(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'registry.db'}")
    service = PluginLifecycle(engine, tmp_path / "runtime")
    yield service
    engine.dispose()


def candidate(tmp_path):
    root = tmp_path / "package"
    root.mkdir()
    (root / "plugin.json").write_text('{"name":"review"}')
    skill = root / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\ndescription: Review changes\n---\nReview.")
    return root


def test_registration_does_not_mean_ready(lifecycle, tmp_path):
    record = lifecycle.stage(candidate(tmp_path), repo="openai/plugins", commit="a" * 40, subdirectory="plugins/review")
    assert record["status"] == "needs_approval"
    assert lifecycle.get(record["id"])["digest"] == record["digest"]


@pytest.mark.asyncio
async def test_pending_approval_never_calls_runtime(lifecycle, tmp_path):
    record = lifecycle.stage(candidate(tmp_path), repo="openai/plugins", commit="a" * 40, subdirectory="")
    activate = AsyncMock()
    result = await lifecycle.activate(record["id"], "Jarvis", approve=AsyncMock(return_value=False), apply=activate)
    assert result["status"] == "needs_approval"
    activate.assert_not_called()


@pytest.mark.asyncio
async def test_ready_requires_positive_live_ack(lifecycle, tmp_path):
    record = lifecycle.stage(candidate(tmp_path), repo="openai/plugins", commit="a" * 40, subdirectory="")
    apply = AsyncMock(return_value=False)
    result = await lifecycle.activate(record["id"], "Jarvis", approve=AsyncMock(return_value=True), apply=apply)
    assert result["status"] == "activation_failed"
    assert result["agent"] == "Jarvis"


@pytest.mark.asyncio
async def test_real_ack_marks_binding_ready(lifecycle, tmp_path):
    record = lifecycle.stage(candidate(tmp_path), repo="openai/plugins", commit="a" * 40, subdirectory="")
    result = await lifecycle.activate(record["id"], "Jarvis", approve=AsyncMock(return_value=True), apply=AsyncMock(return_value=True))
    assert result["status"] == "ready"
    assert result["agent"] == "Jarvis"


@pytest.mark.asyncio
async def test_source_change_during_approval_is_rejected(lifecycle, tmp_path):
    record = lifecycle.stage(candidate(tmp_path), repo="openai/plugins", commit="a" * 40, subdirectory="")
    async def approve(_record):
        (lifecycle.package_path(record["id"]) / "plugin.json").write_text('{"name":"modified"}')
        return True
    apply = AsyncMock()
    with pytest.raises(PluginStateError, match="changed"):
        await lifecycle.activate(record["id"], "Jarvis", approve=approve, apply=apply)
    apply.assert_not_called()
    assert lifecycle.get(record["id"])["status"] != "ready"


@pytest.mark.asyncio
async def test_executable_package_cannot_borrow_content_approval(lifecycle, tmp_path):
    root = candidate(tmp_path)
    (root / "helper.py").write_text("raise SystemExit()")
    record = lifecycle.stage(root, repo="openai/plugins", commit="a" * 40, subdirectory="")
    apply = AsyncMock()
    result = await lifecycle.activate(record["id"], "Jarvis", approve=AsyncMock(return_value=True), apply=apply)
    assert result["status"] == "unsupported"
    apply.assert_not_called()


def test_invalid_identifier_cannot_address_files(lifecycle):
    with pytest.raises(PluginStateError):
        lifecycle.package_path("../../outside")


@pytest.mark.asyncio
async def test_failure_does_not_leak_exception_to_ui(lifecycle, tmp_path):
    record = lifecycle.stage(candidate(tmp_path), repo="openai/plugins", commit="a" * 40, subdirectory="")
    result = await lifecycle.activate(record["id"], "Jarvis", approve=AsyncMock(return_value=True), apply=AsyncMock(side_effect=RuntimeError("secret=not-for-ui")))
    assert result["status"] == "activation_failed"
    assert "not-for-ui" not in str(result)


@pytest.mark.asyncio
async def test_concurrent_operation_rejected_without_second_apply(lifecycle, tmp_path):
    record = lifecycle.stage(candidate(tmp_path), repo="openai/plugins", commit="a" * 40, subdirectory="")
    entered, release = asyncio.Event(), asyncio.Event()
    async def apply(*_args):
        entered.set()
        await release.wait()
        return True
    first = asyncio.create_task(lifecycle.activate(record["id"], "Jarvis", approve=AsyncMock(return_value=True), apply=apply))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(PluginStateError, match="in progress"):
            await lifecycle.activate(record["id"], "Jarvis", approve=AsyncMock(return_value=True), apply=AsyncMock())
    finally:
        release.set()
        await first


@pytest.mark.asyncio
async def test_cancelled_activation_is_not_left_activating(lifecycle, tmp_path):
    record = lifecycle.stage(candidate(tmp_path), repo="openai/plugins", commit="a" * 40, subdirectory="")
    entered = asyncio.Event()
    async def apply(*_args):
        entered.set()
        await asyncio.Event().wait()
    operation = asyncio.create_task(lifecycle.activate(record["id"], "Jarvis", approve=AsyncMock(return_value=True), apply=apply))
    await asyncio.wait_for(entered.wait(), 2)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert lifecycle.get(record["id"])["status"] == "activation_interrupted"


@pytest.mark.asyncio
async def test_cleanup_keeps_ready_agent_files(lifecycle, tmp_path):
    record = lifecycle.stage(candidate(tmp_path), repo="openai/plugins", commit="a" * 40, subdirectory="")
    await lifecycle.activate(record["id"], "Jarvis", approve=AsyncMock(return_value=True), apply=AsyncMock(return_value=True))
    assert lifecycle.cleanup(now=10**12) == []
    assert lifecycle.package_path(record["id"]).exists()


@pytest.mark.asyncio
async def test_failed_detach_never_deletes_bound_package(lifecycle, tmp_path):
    record = lifecycle.stage(candidate(tmp_path), repo="openai/plugins", commit="a" * 40, subdirectory="")
    await lifecycle.activate(record["id"], "Jarvis", approve=AsyncMock(return_value=True), apply=AsyncMock(return_value=True))
    result = await lifecycle.deactivate(record["id"], "Jarvis", remove=AsyncMock(return_value=False))
    assert result["bindings"][0]["status"] == "detach_failed"
    assert lifecycle.cleanup(now=10**12) == []


@pytest.mark.asyncio
async def test_cleanup_requires_acknowledged_detach_and_retention(lifecycle, tmp_path):
    import time
    record = lifecycle.stage(candidate(tmp_path), repo="openai/plugins", commit="a" * 40, subdirectory="")
    await lifecycle.activate(record["id"], "Jarvis", approve=AsyncMock(return_value=True), apply=AsyncMock(return_value=True))
    await lifecycle.deactivate(record["id"], "Jarvis", remove=AsyncMock(return_value=True))
    assert lifecycle.cleanup(now=time.time()) == []
    assert lifecycle.cleanup(now=time.time() + 86401) == [record["id"]]
    assert not lifecycle.package_path(record["id"]).exists()
    assert lifecycle.get(record["id"])["status"] == "expired"


def test_cleanup_expires_unused_candidate_but_retains_audit_record(lifecycle, tmp_path):
    record = lifecycle.stage(candidate(tmp_path), repo="openai/plugins", commit="a" * 40, subdirectory="")
    assert lifecycle.cleanup(now=10**12) == [record["id"]]
    assert lifecycle.get(record["id"])["status"] == "expired"


def test_existing_runtime_directory_is_not_left_world_writable(tmp_path):
    import stat
    root = tmp_path / 'runtime'
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    engine = create_engine(f"sqlite:///{tmp_path / 'registry.db'}")
    try:
        PluginLifecycle(engine, root)
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
    finally:
        engine.dispose()
