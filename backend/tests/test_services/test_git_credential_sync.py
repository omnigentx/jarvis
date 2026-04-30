"""Tests for services.git_credential_sync — DB → file sinks for git + github MCP."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def sync_module(monkeypatch, tmp_path: Path):
    """Import the sync module with all file paths pointed at a tmp dir.

    Also redirects ``llm_provider_sync._SECRETS_YAML`` to the same tmp dir so
    the MCP-token patch path writes to a fake secrets file we can inspect.
    """
    # Must set JARVIS_PERSIST_DIR *before* the first import so the module-level
    # path constants capture the tmp dir.
    monkeypatch.setenv("JARVIS_PERSIST_DIR", str(tmp_path))

    # Fresh import — importlib.reload handles the repeat-test case.
    import importlib
    import services.git_credential_sync as mod
    importlib.reload(mod)

    # Redirect the secrets YAML path used by the MCP patch.
    import services.llm_provider_sync as llm_sync
    monkeypatch.setattr(llm_sync, "_SECRETS_YAML", tmp_path / "fastagent.secrets.yaml")

    return mod, tmp_path


@pytest.fixture
def fake_config_service(monkeypatch):
    """In-memory stand-in for services.config_service.config_service.

    Only implements ``get(category, key)`` since that's all this module uses.
    """
    store: dict[tuple[str, str], str | None] = {}

    class FakeCfg:
        def get(self, category: str, key: str):
            return store.get((category, key))

    fake = FakeCfg()
    import services.config_service as cfg_mod
    monkeypatch.setattr(cfg_mod, "config_service", fake)
    return fake, store


class TestRenderCredentials:
    """The two rendering helpers are pure — exercise them directly."""

    def test_token_present_emits_github_host(self, sync_module):
        mod, _ = sync_module
        assert mod._render_credentials("ghp_ABC") == "https://x-access-token:ghp_ABC@github.com\n"

    def test_token_missing_emits_empty_body(self, sync_module):
        """Empty file is correct "not configured" — git prompts for username."""
        mod, _ = sync_module
        assert mod._render_credentials(None) == ""
        assert mod._render_credentials("") == ""


class TestRenderGitconfig:
    def test_includes_credential_helper_pointing_at_container_path(self, sync_module):
        mod, _ = sync_module
        text = mod._render_gitconfig("Phuc", "p@example.com")
        assert "[credential]" in text
        # The helper must reference the *container* path, not the host path —
        # this is what git inside jarvis_backend actually sees.
        assert "helper = store --file=/app/git-credentials" in text

    def test_user_section_present_when_both_fields_given(self, sync_module):
        mod, _ = sync_module
        text = mod._render_gitconfig("Phuc", "p@example.com")
        assert "[user]" in text
        assert "name = Phuc" in text
        assert "email = p@example.com" in text

    def test_user_section_absent_when_both_fields_missing(self, sync_module):
        mod, _ = sync_module
        text = mod._render_gitconfig(None, None)
        assert "[user]" not in text

    def test_user_section_partial_still_renders(self, sync_module):
        """Missing half the identity is worse than none — but don't crash."""
        mod, _ = sync_module
        text = mod._render_gitconfig("Phuc", None)
        assert "[user]" in text
        assert "name = Phuc" in text
        assert "email" not in text


class TestWriteFromValues:
    def test_happy_path_writes_both_files_with_expected_modes(self, sync_module):
        mod, tmp = sync_module
        mod._write_from_values(token="ghp_X", user_name="Phuc", user_email="p@example.com")

        cred = tmp / "git-credentials"
        conf = tmp / "gitconfig"
        assert cred.exists() and conf.exists()
        # Mode 0600 for credentials — plaintext secret; 0644 for gitconfig.
        assert stat.S_IMODE(cred.stat().st_mode) == 0o600
        assert stat.S_IMODE(conf.stat().st_mode) == 0o644
        assert "ghp_X" in cred.read_text()
        assert "Phuc" in conf.read_text()

    def test_rotation_overwrites_old_token(self, sync_module):
        mod, tmp = sync_module
        mod._write_from_values(token="ghp_OLD", user_name="Phuc", user_email="p@example.com")
        mod._write_from_values(token="ghp_NEW", user_name="Phuc", user_email="p@example.com")

        body = (tmp / "git-credentials").read_text()
        assert "ghp_NEW" in body
        assert "ghp_OLD" not in body

    def test_clearing_token_empties_credentials_file(self, sync_module):
        mod, tmp = sync_module
        mod._write_from_values(token="ghp_X", user_name="Phuc", user_email="p@example.com")
        mod._write_from_values(token=None, user_name=None, user_email=None)

        # File must still exist so the Docker bind-mount inode stays valid —
        # content is just empty.
        assert (tmp / "git-credentials").exists()
        assert (tmp / "git-credentials").read_text() == ""

    def test_patches_github_mcp_token_in_secrets_yaml(self, sync_module):
        mod, tmp = sync_module
        mod._write_from_values(token="ghp_MCP", user_name="Phuc", user_email="p@example.com")

        data = yaml.safe_load((tmp / "fastagent.secrets.yaml").read_text())
        assert data["mcp"]["servers"]["github"]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_MCP"

    def test_clearing_token_removes_github_mcp_token_key(self, sync_module):
        mod, tmp = sync_module
        mod._write_from_values(token="ghp_X", user_name="Phuc", user_email="p@example.com")
        mod._write_from_values(token=None, user_name=None, user_email=None)

        data = yaml.safe_load((tmp / "fastagent.secrets.yaml").read_text())
        github_env = data.get("mcp", {}).get("servers", {}).get("github", {}).get("env", {})
        assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in github_env

    def test_preserves_unrelated_secrets_yaml_sections(self, sync_module):
        """Patch must be surgical — user's openai/anthropic/etc. survive."""
        mod, tmp = sync_module
        secrets_path = tmp / "fastagent.secrets.yaml"
        secrets_path.write_text(yaml.safe_dump({
            "anthropic": {"api_key": "sk-ant-keep"},
            "openai": {"base_url": "http://localhost:9000/v1", "api_key": "sk-keep"},
            "mcp": {"servers": {"brave-search": {"env": {"BRAVE_API_KEY": "keep"}}}},
        }))

        mod._write_from_values(token="ghp_X", user_name="Phuc", user_email="p@example.com")

        data = yaml.safe_load(secrets_path.read_text())
        assert data["anthropic"]["api_key"] == "sk-ant-keep"
        assert data["openai"]["api_key"] == "sk-keep"
        assert data["mcp"]["servers"]["brave-search"]["env"]["BRAVE_API_KEY"] == "keep"
        assert data["mcp"]["servers"]["github"]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_X"


class TestApplyChange:
    def test_dispatches_via_config_service(self, sync_module, fake_config_service):
        mod, tmp = sync_module
        _, store = fake_config_service
        # Seed the DB stand-in with all three fields, then trigger apply_change
        # on one of them — the sink reads the full current state, not the
        # individual mutation, so output still reflects all three.
        store[("service.github", "personal_access_token")] = "ghp_Y"
        store[("service.github", "user_name")] = "Phuc"
        store[("service.github", "user_email")] = "p@example.com"

        mod.apply_change("personal_access_token", "ghp_Y", action="update")

        assert "ghp_Y" in (tmp / "git-credentials").read_text()
        conf = (tmp / "gitconfig").read_text()
        assert "name = Phuc" in conf
        assert "email = p@example.com" in conf

    def test_unknown_field_is_noop(self, sync_module, fake_config_service):
        mod, tmp = sync_module
        mod.apply_change("some_unrelated_key", "value", action="update")
        assert not (tmp / "git-credentials").exists()
        assert not (tmp / "gitconfig").exists()


class TestReconcileFromDb:
    def test_writes_placeholder_files_even_without_any_config(self, sync_module, fake_config_service):
        """Bind-mount needs valid inodes before the user runs the wizard."""
        mod, tmp = sync_module
        fake, _ = fake_config_service
        mod.reconcile_from_db(fake)
        assert (tmp / "git-credentials").exists()
        assert (tmp / "gitconfig").exists()
        assert (tmp / "git-credentials").read_text() == ""  # token empty until wizard fills it

    def test_seeds_from_db_when_values_present(self, sync_module, fake_config_service):
        mod, tmp = sync_module
        fake, store = fake_config_service
        store[("service.github", "personal_access_token")] = "ghp_FROMDB"
        store[("service.github", "user_name")] = "Phuc"
        store[("service.github", "user_email")] = "p@example.com"

        mod.reconcile_from_db(fake)

        assert "ghp_FROMDB" in (tmp / "git-credentials").read_text()


class TestNoTokenLeaks:
    """Token value must never hit the log — only path + byte count."""

    def test_log_contains_no_token_substring(self, sync_module, caplog):
        mod, _ = sync_module
        token = "ghp_NEVER_LOG_ME_PLZ_1234567890"
        with caplog.at_level("INFO"):
            mod._write_from_values(token=token, user_name="Phuc", user_email="p@example.com")
        log_text = "\n".join(r.getMessage() for r in caplog.records)
        assert token not in log_text


class TestTokenUrlEncoding:
    """Fine-grained PATs and mirror tokens may contain URL-reserved chars."""

    def test_at_sign_in_token_is_escaped(self, sync_module):
        mod, _ = sync_module
        # A token containing '@' would produce a URL like
        # https://x-access-token:a@b@github.com — git parses the *last* '@'
        # as the host separator and treats "a" as the password, silently
        # misauthenticating. Must be percent-encoded.
        body = mod._render_credentials("github_pat_a@b")
        assert "a@b" not in body  # raw '@' must not survive
        assert "a%40b" in body
        assert body.endswith("@github.com\n")

    def test_colon_in_token_is_escaped(self, sync_module):
        mod, _ = sync_module
        body = mod._render_credentials("tok:en")
        assert "tok:en" not in body
        assert "tok%3Aen" in body

    def test_classic_pat_passes_through_unchanged(self, sync_module):
        mod, _ = sync_module
        # Classic ghp_ tokens use only [A-Za-z0-9_] — nothing to escape.
        assert mod._render_credentials("ghp_ABC123_xyz") == (
            "https://x-access-token:ghp_ABC123_xyz@github.com\n"
        )


class TestFailLoud:
    """Filesystem errors on the git-credentials / gitconfig path must raise,
    per the ``feedback_no_silent_fallbacks`` rule — a half-configured
    credential file that only explodes at ``git clone`` time is worse than a
    visible boot-time failure."""

    def test_atomic_write_raises_on_permission_denied(self, sync_module, monkeypatch):
        mod, tmp = sync_module
        target = tmp / "git-credentials"

        def boom(*_a, **_kw):
            raise PermissionError("read-only mount")

        # Simulate a host filesystem that rejects the first write (tmp file).
        monkeypatch.setattr(mod.Path, "write_bytes", boom)
        with pytest.raises(PermissionError):
            mod._atomic_write(target, "hello", mode=0o600)

    def test_atomic_write_cleans_tmp_even_when_raising(self, sync_module, monkeypatch):
        mod, tmp = sync_module
        target = tmp / "git-credentials"

        original_chmod = os.chmod

        def fail_on_chmod(path, mode):
            # Let the tmp write succeed so we have a stray file, then fail.
            if str(path).endswith(".tmp"):
                raise PermissionError("denied")
            return original_chmod(path, mode)

        monkeypatch.setattr(mod.os, "chmod", fail_on_chmod)
        with pytest.raises(PermissionError):
            mod._atomic_write(target, "hello", mode=0o600)

        # Even on failure the stray tmp must be removed so a retry starts clean.
        assert not (tmp / "git-credentials.tmp").exists()

    def test_write_from_values_propagates_atomic_write_error(self, sync_module, monkeypatch):
        mod, _ = sync_module

        def boom(*_a, **_kw):
            raise OSError("simulated FS error")

        monkeypatch.setattr(mod, "_atomic_write", boom)
        with pytest.raises(OSError):
            mod._write_from_values(token="ghp_X", user_name="Phuc", user_email="p@e.com")

    def test_secrets_yaml_patch_failure_does_not_block_git_files(
        self, sync_module, monkeypatch
    ):
        """The MCP yaml patch is explicitly best-effort per docstring.

        If the yaml is malformed we still want the git CLI side to succeed —
        dev agents can work without the github MCP, but not the reverse.
        """
        mod, tmp = sync_module

        def boom(_):
            raise RuntimeError("pretend yaml exploded")

        monkeypatch.setattr(mod, "_patch_secrets_yaml_github_token", boom)
        # Must NOT raise.
        mod._write_from_values(token="ghp_X", user_name="Phuc", user_email="p@e.com")
        assert (tmp / "git-credentials").exists()
        assert (tmp / "gitconfig").exists()


class TestEBUSYFallback:
    """``os.replace`` returns EBUSY on Docker bind-mounts (rename over an
    inode mount is forbidden on Linux). The fallback path truncates +
    rewrites in place so rotation still works inside the container."""

    def test_ebusy_triggers_inplace_rewrite(self, sync_module, monkeypatch):
        import errno as _errno

        mod, tmp = sync_module
        target = tmp / "git-credentials"
        # Pre-create the target so the in-place write path has something to
        # overwrite — mirrors the real bind-mount scenario.
        target.write_text("old-body")

        call_count = {"n": 0}
        original_replace = os.replace

        def replace_busy(src, dst):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError(_errno.EBUSY, "device busy")
            return original_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", replace_busy)
        mod._atomic_write(target, "new-body", mode=0o600)

        assert target.read_text() == "new-body"
        # Tmp must be cleaned up even on the fallback path.
        assert not (tmp / "git-credentials.tmp").exists()

    def test_non_ebusy_oserror_propagates(self, sync_module, monkeypatch):
        import errno as _errno

        mod, tmp = sync_module
        target = tmp / "git-credentials"

        def replace_acces(_src, _dst):
            raise OSError(_errno.EACCES, "no permission")

        monkeypatch.setattr(mod.os, "replace", replace_acces)
        with pytest.raises(OSError):
            mod._atomic_write(target, "body", mode=0o600)


class TestRuntimeConfigRouting:
    """Verify the runtime_config listener actually dispatches
    ``service.github.*`` events into ``git_credential_sync.apply_change``
    (this was split across two modules in the initial implementation and is
    easy to regress by adding a new ``service.*`` branch above ours)."""

    def test_github_field_dispatched_to_sync(self, sync_module, monkeypatch):
        from services import runtime_config

        mod, _ = sync_module
        calls: list[tuple[str, str, str]] = []

        def spy(key, new_value, *, action):
            calls.append((key, str(new_value) if new_value is not None else None, action))

        monkeypatch.setattr(mod, "apply_change", spy)

        class Evt:
            category = "service.github"
            key = "personal_access_token"
            new_value = "ghp_ROUTED"
            action = "update"

        runtime_config._on_config_change(Evt())
        assert calls == [("personal_access_token", "ghp_ROUTED", "update")]

    def test_other_service_still_hits_env_branch(self, monkeypatch):
        """Regression guard: the new branch must not eat ``service.roborock``
        and other legacy ``service.*`` entries."""
        from services import runtime_config

        monkeypatch.delenv("ROBOROCK_USERNAME", raising=False)

        class Evt:
            category = "service.roborock"
            key = "ROBOROCK_USERNAME"
            new_value = "user@example.com"
            action = "update"

        runtime_config._on_config_change(Evt())
        assert os.environ["ROBOROCK_USERNAME"] == "user@example.com"
