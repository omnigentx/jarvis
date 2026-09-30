"""An approved catalog is not approval of every repository it references."""
from unittest.mock import AsyncMock

import pytest
from services.plugins.installation import PluginInstaller
from services.plugins.lifecycle import PluginLifecycle
from services.plugins.package import PackageError
from sqlalchemy import create_engine


@pytest.fixture
def installer(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'registry.db'}")
    lifecycle = PluginLifecycle(engine, tmp_path / 'runtime')
    service = PluginInstaller(lifecycle)
    yield service
    engine.dispose()


@pytest.mark.asyncio
async def test_unapproved_source_is_not_downloaded(installer):
    download = AsyncMock()
    result = await installer.install('unknown/plugin', 'a' * 40, '', approve=AsyncMock(return_value=False), download=download)
    assert result['status'] == 'needs_source_approval'
    download.assert_not_called()


@pytest.mark.asyncio
async def test_branch_name_is_rejected_before_approval_or_download(installer):
    approval, download = AsyncMock(), AsyncMock()
    with pytest.raises(PackageError):
        await installer.install('openai/plugins', 'main', '', approve=approval, download=download)
    approval.assert_not_called()
    download.assert_not_called()


@pytest.mark.asyncio
async def test_staging_removes_download_temporary_files(installer):
    directories = []
    async def download(repo, commit, subdirectory, destination):
        directories.append(destination)
        destination.mkdir()
        (destination / 'plugin.json').write_text('{"name":"review"}')
        skill = destination / 'skills/review'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\ndescription: Review changes\n---\nCheck changes.')
    result = await installer.install('openai/plugins', 'a' * 40, 'plugins/review', approve=AsyncMock(return_value=True), download=download)
    assert result['status'] == 'needs_approval'
    assert result['repo'] == 'openai/plugins'
    assert installer.lifecycle.package_path(result['id']).exists()
    assert not directories[0].exists()


@pytest.mark.asyncio
async def test_catalog_inspection_never_executes_capabilities(installer):
    async def download(repo, commit, subdirectory, destination):
        destination.mkdir()
        (destination / 'plugin.json').write_text('{"name":"review","hooks":{"command":"touch /tmp/unsafe"}}')
    result = await installer.install('openai/plugins', 'a' * 40, '', approve=AsyncMock(return_value=True), download=download)
    assert result['blockers']
    assert result['status'] == 'needs_approval'
