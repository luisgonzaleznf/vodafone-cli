"""CLI error contract. Synthetic token file; no network."""

import json

from typer.testing import CliRunner

from vodafone import api, cli

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


def test_auth_refresh_is_still_a_stub(tmp_path):
    result = CliRunner().invoke(cli.app, ["--tokens", str(_tokens_file(tmp_path)), "auth", "refresh"])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["type"] == "not_implemented"
