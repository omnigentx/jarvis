"""A plugin cannot alter the host-enforced execution profile."""

import pytest

from services.plugins.package import PackageError
from services.plugins.sandbox import sandbox_settings


def test_container_profile_drops_host_access_and_pins_image(tmp_path):
    settings = sandbox_settings(
        tmp_path,
        {"command": "python", "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"]},
        "sha256:" + "a" * 64,
    )
    args = settings.args
    assert settings.command == "docker"
    assert "--network=none" in args
    assert "--read-only" in args
    assert "--cap-drop=ALL" in args
    assert "--security-opt=no-new-privileges" in args
    assert "--user=65532:65532" in args
    assert "--pids-limit=64" in args
    assert "--memory=256m" in args
    assert "--cpus=1" in args
    assert args[-2:] == ["python", "/plugin/server.py"]
    assert any(
        str(tmp_path.resolve()) in arg and arg.endswith(":/plugin:ro") for arg in args
    )
    assert not any("docker.sock" in arg for arg in args)


@pytest.mark.parametrize("image", ["python:latest", "python:3.13", "sha256:bad"])
def test_floating_or_invalid_images_cannot_execute(tmp_path, image):
    with pytest.raises(PackageError):
        sandbox_settings(tmp_path, {"command": "python", "args": ["server.py"]}, image)


@pytest.mark.parametrize(
    "config",
    [
        {"command": "python", "args": ["server.py"], "cwd": "/etc"},
        {
            "command": "python",
            "args": ["server.py"],
            "env": {"LD_PRELOAD": "/plugin/evil.so"},
        },
        {"command": "docker", "args": ["run", "--privileged", "evil"]},
        {
            "command": "python",
            "args": ["server.py"],
            "transport": "http",
            "url": "http://localhost",
        },
    ],
)
def test_manifest_cannot_escape_host_execution_profile(tmp_path, config):
    with pytest.raises(PackageError):
        sandbox_settings(tmp_path, config, "sha256:" + "a" * 64)


@pytest.mark.parametrize(
    "key",
    [
        "PATH",
        "HOME",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "SSL_CERT_FILE",
        "GIT_CONFIG_SYSTEM",
        "HTTP_PROXY",
        "UV_TOOL_DIR",
    ],
)
def test_manifest_cannot_redirect_host_docker_cli_environment(tmp_path, key):
    with pytest.raises(PackageError, match="environment"):
        sandbox_settings(
            tmp_path,
            {"command": "python", "env": {key: "/untrusted"}},
            "sha256:" + "a" * 64,
        )


def test_container_host_mount_mapping_is_operator_controlled(tmp_path, monkeypatch):
    root = tmp_path / "inside" / "candidate/package"
    root.mkdir(parents=True)
    monkeypatch.setenv("JARVIS_PLUGIN_RUNTIME_ROOT", str(tmp_path / "inside"))
    monkeypatch.setenv("JARVIS_PLUGIN_HOST_ROOT", "/host/plugins")
    settings = sandbox_settings(root, {"command": "python"}, "sha256:" + "a" * 64)
    assert "/host/plugins/candidate/package:/plugin:ro" in settings.args
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(PackageError, match="mount"):
        sandbox_settings(outside, {"command": "python"}, "sha256:" + "a" * 64)
