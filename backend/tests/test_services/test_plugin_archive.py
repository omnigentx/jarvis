"""Adversarial runtime download/extraction contracts."""
import io
import zipfile

import pytest
from services.plugins.archive import extract_package, validate_source
from services.plugins.package import PackageError


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        for path, content in files:
            z.writestr(path, content)
    return output.getvalue()


def test_extract_only_selected_plugin(tmp_path):
    data = archive([("repo-abc/plugins/review/plugin.json", '{"name":"review"}'),
                    ("repo-abc/other/secret.txt", "unrelated")])
    root = extract_package(data, tmp_path / "package", "plugins/review")
    assert (root / "plugin.json").is_file()
    assert not (root / "other").exists()


@pytest.mark.parametrize("path", ["repo/../outside", "/outside", "repo/plugins/x/../../outside", "repo/evil\\outside"])
def test_archive_traversal_rejected_before_writes(tmp_path, path):
    with pytest.raises(PackageError, match="path"):
        extract_package(archive([(path, "data")]), tmp_path / "package", "")
    assert not (tmp_path / "package").exists()


def test_archive_symlink_rejected(tmp_path):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        entry = zipfile.ZipInfo("repo/alias")
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        z.writestr(entry, "../../outside")
    with pytest.raises(PackageError, match="symlink"):
        extract_package(output.getvalue(), tmp_path / "package", "")


def test_expansion_limit_checked_before_writes(tmp_path):
    with pytest.raises(PackageError, match="size"):
        extract_package(archive([("repo/large", "x" * 2000)]), tmp_path / "package", "", max_bytes=1000)
    assert not (tmp_path / "package").exists()


def test_duplicate_member_rejected(tmp_path):
    with pytest.raises(PackageError, match="duplicate"):
        extract_package(archive([("repo/a", "one"), ("repo/a", "two")]), tmp_path / "package", "")


def test_no_mutable_ref_or_unapproved_repo():
    with pytest.raises(PackageError, match="commit"):
        validate_source("openai/plugins", "main", "plugins/review", {"openai/plugins"})
    with pytest.raises(PackageError, match="approved"):
        validate_source("attacker/plugins", "a" * 40, "", {"openai/plugins"})


@pytest.mark.parametrize("repo", ["https://localhost/secret", "openai/plugins?x=y", "../openai/plugins", "openai/plugins/extra"])
def test_source_is_repository_identity_not_arbitrary_url(repo):
    with pytest.raises(PackageError):
        validate_source(repo, "a" * 40, "", {repo})


def test_source_subdirectory_cannot_escape():
    with pytest.raises(PackageError, match="path"):
        validate_source("openai/plugins", "a" * 40, "../outside", {"openai/plugins"})
