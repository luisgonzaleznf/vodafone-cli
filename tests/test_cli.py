"""CLI error contract. Synthetic token file; no network."""

import base64
import json
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest
from typer.testing import CliRunner

import vodafone
from vodafone import api, cli
from vodafone.tokens import FileTokenStore

SECRET = "S" * 28


def _tokens_file(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"accessToken": SECRET, "userCsrfToken": "c" * 36}))
    return path


def test_unexpected_exception_is_sanitized_json(tmp_path, monkeypatch):
    def boom(client):
        raise RuntimeError(f"GET https://example.invalid/?nif=00000000T Bearer {SECRET}")

    monkeypatch.setattr(api, "get_me", boom)
    result = CliRunner().invoke(cli.app, ["--tokens", str(_tokens_file(tmp_path)), "me"])

    assert result.exit_code == 1
    assert json.loads(result.stderr) == {
        "error": {"type": "internal", "message": "unexpected internal error: RuntimeError", "exit_code": 1}
    }
    assert SECRET not in result.output and "00000000T" not in result.output


def test_refresh_without_a_refresh_token_is_an_auth_error(tmp_path):
    result = CliRunner().invoke(cli.app, ["--tokens", str(_tokens_file(tmp_path)), "auth", "refresh"])

    assert result.exit_code == 3
    assert json.loads(result.stderr)["error"]["type"] == "auth_expired"


def _status(path):
    return CliRunner().invoke(cli.app, ["--tokens", str(path), "auth", "status"])


@pytest.mark.parametrize(
    "fields",
    [
        {"accessToken": {"token": SECRET}},
        {"accessToken": [SECRET]},
        {"accessToken": 12345678},
        {"accessToken": "has a space"},
        {"accessToken": "line\nbreak"},
        {"userCsrfToken": {"csrf": "x"}},
        {"refreshToken": {"token": "r" * 32}},
        {"refreshToken": ["r" * 32]},
        {"jwt": {"payload": "x"}},
        {"jwt": [{"payload": "x"}]},
    ],
)
def test_malformed_credentials_are_an_auth_error(tmp_path, fields):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"accessToken": SECRET, "userCsrfToken": "c" * 36, **fields}))

    for args in (["auth", "status"], ["me"]):
        result = CliRunner().invoke(cli.app, ["--tokens", str(path), *args])
        assert result.exit_code == 3, result.output
        assert result.stdout == ""
        error = json.loads(result.stderr)["error"]
        assert error["type"] == "auth_expired" and "vodafone auth login" in error["message"]
        assert SECRET not in result.output


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


@pytest.mark.parametrize("exp", [10**30, 1e300, -(10**20), float("nan"), True, "1700000000"])
def test_status_survives_an_out_of_range_jwt_exp(tmp_path, exp):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"accessToken": SECRET, "userCsrfToken": "c" * 36, "jwt": [_jwt({"exp": exp})]}))

    result = _status(path)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["jwt"] == {"present": True, "expires_at": None}


def test_status_reports_a_valid_jwt_exp(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"accessToken": SECRET, "userCsrfToken": "c" * 36, "jwt": [_jwt({"exp": 1900000000})]}))

    assert json.loads(_status(path).stdout)["jwt"]["expires_at"] == "2030-03-17T17:46:40+00:00"


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00", "not a date", 17])
def test_status_survives_an_out_of_range_expiry(tmp_path, value):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"accessToken": SECRET, "userCsrfToken": "c" * 36, "accessTokenExpirationTime": value}))

    result = _status(path)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["access_token"]["expires_at"] is None


def test_status_unexpected_exception_is_sanitized_json(tmp_path, monkeypatch):
    def boom(self):
        raise OverflowError(f"Bearer {SECRET}")

    monkeypatch.setattr(FileTokenStore, "load", boom)
    result = _status(_tokens_file(tmp_path))

    assert result.exit_code == 1
    assert json.loads(result.stderr) == {
        "error": {"type": "internal", "message": "unexpected internal error: OverflowError", "exit_code": 1}
    }
    assert SECRET not in result.output


def test_a_missing_token_file_points_at_native_login(tmp_path):
    result = _status(tmp_path / "missing.json")

    assert result.exit_code == 3
    assert "vodafone auth login" in json.loads(result.stderr)["error"]["message"]


def _pyproject_version() -> str:
    return tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]["version"]


@pytest.mark.parametrize("args", [["--version"], ["--version", "me"], ["--tokens", "/nonexistent", "--version"]])
def test_version_prints_the_package_version_and_exits(args, monkeypatch):
    monkeypatch.setattr(api, "run_with_client", lambda *a: pytest.fail("--version must not run a command"))

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{version('vodafone-cli')}\n"
    assert version("vodafone-cli") == _pyproject_version()


def test_version_without_installed_metadata_says_unknown(monkeypatch):
    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(vodafone, "version", missing)
    result = CliRunner().invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == "unknown\n"
