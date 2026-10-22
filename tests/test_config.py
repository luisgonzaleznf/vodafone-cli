"""Token-file lookup order. No real token files are read or written."""

from pathlib import Path

import pytest

from vodafone import config


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A working directory without a token file, and no lookup overrides from the real environment."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(config.TOKENS_FILE_ENV, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def _installed(monkeypatch, env):
    # An installed copy: config.py sits in <env>/lib/python3.X/site-packages/vodafone/.
    monkeypatch.setattr(config, "_PROJECT_ROOT", env / "venv" / "lib" / "python3.14")


def test_the_env_var_wins(env, monkeypatch):
    monkeypatch.setenv(config.TOKENS_FILE_ENV, "~/elsewhere.json")
    (env / ".local-secrets").mkdir()
    (env / ".local-secrets" / "tokens.json").write_text("{}")

    assert config.default_tokens_path() == env / "home" / "elsewhere.json"


def test_a_token_file_in_the_working_directory_is_used(env, monkeypatch):
    _installed(monkeypatch, env)
    (env / ".local-secrets").mkdir()
    (env / ".local-secrets" / "tokens.json").write_text("{}")

    assert config.default_tokens_path() == env / ".local-secrets" / "tokens.json"


def test_a_source_checkout_uses_its_own_local_secrets(env):
    checkout = Path(config.__file__).resolve().parents[2]
    assert (checkout / "pyproject.toml").is_file()  # the tests run from a checkout

    assert config.default_tokens_path() == checkout / ".local-secrets" / "tokens.json"


def test_an_installed_copy_uses_the_user_config_dir(env, monkeypatch):
    _installed(monkeypatch, env)

    assert config.default_tokens_path() == env / "home" / ".config" / "vodafone-cli" / "tokens.json"


def test_an_installed_copy_honours_xdg_config_home(env, monkeypatch):
    _installed(monkeypatch, env)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(env / "xdg"))

    assert config.default_tokens_path() == env / "xdg" / "vodafone-cli" / "tokens.json"


def test_a_relative_xdg_config_home_is_ignored(env, monkeypatch):
    _installed(monkeypatch, env)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")

    assert config.default_tokens_path() == env / "home" / ".config" / "vodafone-cli" / "tokens.json"
