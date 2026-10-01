"""Review code/configuration without executing embedded instructions."""

import pytest
from sqlalchemy import create_engine

from services.plugins.lifecycle import PluginLifecycle, PluginStateError
from services.plugins.review import review_package


def test_review_executable_source_as_text_and_reject_escape(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.json").write_text('{"name":"source"}')
    (source / "server.py").write_text('raise RuntimeError("DO_NOT_RUN")')
    engine = create_engine("sqlite:///" + str(tmp_path / "db"))
    lifecycle = PluginLifecycle(engine, tmp_path / "plugins")
    candidate = lifecycle.stage(
        source, repo="openai/plugins", commit="a" * 40, subdirectory=""
    )
    identity = candidate["id"]
    assert "server.py" in [
        item["path"] for item in review_package(lifecycle, identity)["files"]
    ]
    assert "DO_NOT_RUN" in review_package(lifecycle, identity, "server.py")["content"]
    for path in ("../db", "/etc/passwd", "..\\db"):
        with pytest.raises(PluginStateError):
            review_package(lifecycle, identity, path)
    (lifecycle.package_path(identity) / "server.py").write_text("modified")
    with pytest.raises(PluginStateError, match="changed"):
        review_package(lifecycle, identity, "server.py")
    engine.dispose()
