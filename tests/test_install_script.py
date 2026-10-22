"""install.sh behaviour, offline: a stub `uv` records what it is asked to do, and a stub `curl` serves a
stand-in for Astral's installer that puts that stub where the real installer would. Synthetic
credentials only."""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "install.sh"
SYSTEM_PATH = "/usr/bin:/bin"
SECRET = "s3cr3t-token-value"

pytestmark = pytest.mark.skipif(
    shutil.which("uv", path=SYSTEM_PATH) is not None, reason="a system-wide uv would shadow the stub"
)

STUB_UV = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_LOG"
case "$*" in
    "tool dir") printf '%s\\n' "$STUB_ROOT/tools" ;;
    "tool dir --bin") printf '%s\\n' "$STUB_ROOT/bin" ;;
    "tool install "*)
        mkdir -p "$STUB_ROOT/bin"
        printf '#!/bin/sh\\necho 0.1.0\\n' > "$STUB_ROOT/bin/vodafone"
        chmod +x "$STUB_ROOT/bin/vodafone"
        ;;
    *) exit 2 ;;
esac
"""

# Serves an "installer" that copies the stub uv into $FAKE_INSTALL_TARGET.
STUB_CURL = """#!/bin/sh
printf 'curl %s\\n' "$*" >> "$STUB_LOG"
printf 'mkdir -p "%s" && cp "%s" "%s/uv"\\n' "$FAKE_INSTALL_TARGET" "$STUB_UV" "$FAKE_INSTALL_TARGET"
"""


def _executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def sandbox(tmp_path):
    """A clean environment: no uv on PATH, a scratch HOME, and nothing inherited from the real one."""
    stub_uv = _executable(tmp_path / "stubs" / "uv", STUB_UV)
    fake_bin = tmp_path / "fake-bin"  # on PATH; holds only what a test puts there
    fake_bin.mkdir()
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}:{SYSTEM_PATH}",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "STUB_ROOT": str(tmp_path / "uv-tools"),
        "STUB_UV": str(stub_uv),
    }
    return tmp_path, env, stub_uv, fake_bin


def _run(env: dict[str, str], **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", str(SCRIPT)], env={**env, **extra}, capture_output=True, text=True, timeout=30)


def _calls(env: dict[str, str]) -> list[str]:
    log = Path(env["STUB_LOG"])
    return log.read_text().splitlines() if log.exists() else []


def test_installs_the_pinned_tag_from_github(sandbox):
    _, env, stub_uv, fake_bin = sandbox
    shutil.copy(stub_uv, fake_bin / "uv")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert "tool install vodafone-cli @ git+https://github.com/luisgonzaleznf/vodafone-cli.git@v0.1.0" in _calls(env)
    assert "Installed vodafone 0.1.0" in result.stdout and "vodafone auth login" in result.stdout


@pytest.mark.parametrize(
    "source",
    [
        f"https://user:{SECRET}@example.invalid/vodafone-cli.git",
        f"https://{SECRET}@example.invalid/vodafone-cli.git",
        f"git+https://x-access-token:{SECRET}@example.invalid/vodafone-cli.git",
        f"ssh://git:{SECRET}@example.invalid/vodafone-cli.git",
        f"ssh://git@{SECRET}@example.invalid/vodafone-cli.git",
        # Forms that slip past a user-info check: an empty authority, a query, a fragment,
        # backslashes, escapes, a non-numeric port, other schemes and bare paths.
        f"https:///user:{SECRET}@example.invalid/vodafone-cli.git",
        f"https://example.invalid/vodafone-cli.git?access_token={SECRET}",
        f"https://example.invalid/vodafone-cli.git#{SECRET}",
        f"https://example.invalid\\user:{SECRET}@example.invalid/vodafone-cli.git",
        f"https://example.invalid/{SECRET}%40/vodafone-cli.git",
        f"https://example.invalid:{SECRET}/vodafone-cli.git",
        f"HTTPS://{SECRET}@example.invalid/vodafone-cli.git",
        f"http://example.invalid/{SECRET}:x@vodafone-cli.git",
        f"file://{SECRET}@example.invalid/srv/vodafone-cli",
        f"/srv/git/{SECRET}@vodafone-cli",
        f"https://example.invalid/{SECRET} vodafone-cli.git",
    ],
)
def test_a_source_url_that_is_not_plain_is_refused_without_echoing_it(sandbox, source):
    _, env, _, fake_bin = sandbox
    _executable(fake_bin / "curl", STUB_CURL)  # uv is missing: refusing must come before bootstrapping it

    result = _run(env, VODAFONE_CLI_SOURCE=source, FAKE_INSTALL_TARGET=env["HOME"] + "/.local/bin")

    assert result.returncode == 1
    assert "VODAFONE_CLI_SOURCE must be a plain" in result.stderr
    assert SECRET not in result.stdout + result.stderr
    assert _calls(env) == []


@pytest.mark.parametrize("ref", [f"v1@{SECRET}", f"main?{SECRET}", f"-{SECRET}", f"v1 {SECRET}"])
def test_a_ref_that_is_not_plain_is_refused_without_echoing_it(sandbox, ref):
    _, env, stub_uv, fake_bin = sandbox
    shutil.copy(stub_uv, fake_bin / "uv")

    result = _run(env, VODAFONE_CLI_REF=ref)

    assert result.returncode == 1
    assert "must be a plain tag, branch or commit" in result.stderr
    assert SECRET not in result.stdout + result.stderr
    assert _calls(env) == []


@pytest.mark.parametrize("home", [None, ""])
def test_a_missing_home_is_refused_before_bootstrapping_uv(sandbox, home):
    _, env, _, fake_bin = sandbox
    _executable(fake_bin / "curl", STUB_CURL)
    env = {name: value for name, value in env.items() if name != "HOME"}
    if home is not None:
        env["HOME"] = home

    result = _run(env, FAKE_INSTALL_TARGET="/nonexistent/bin")

    assert result.returncode == 1
    assert "HOME is not set" in result.stderr
    assert _calls(env) == []


@pytest.mark.parametrize(
    "source",
    [
        "ssh://git@github.com/luisgonzaleznf/vodafone-cli.git",
        "ssh://github.com:22/luisgonzaleznf/vodafone-cli.git",
        "https://git.example.invalid:8443/team/vodafone-cli",
        "file:///srv/git/vodafone-cli",
    ],
)
def test_plain_sources_are_accepted(sandbox, source):
    _, env, stub_uv, fake_bin = sandbox
    shutil.copy(stub_uv, fake_bin / "uv")

    result = _run(env, VODAFONE_CLI_SOURCE=source)

    assert result.returncode == 0, result.stderr
    assert f"tool install vodafone-cli @ git+{source}@v0.1.0" in _calls(env)


# Where Astral's installer puts uv, by its precedence: UV_INSTALL_DIR, CARGO_DIST_FORCE_INSTALL_DIR,
# UV_UNMANAGED_INSTALL (a forced dir equal to Cargo's home gets /bin), then XDG_BIN_HOME,
# $XDG_DATA_HOME/../bin and ~/.local/bin.
@pytest.mark.parametrize(
    ("variables", "target"),
    [
        ({}, "{home}/.local/bin"),
        ({"UV_INSTALL_DIR": "{tmp}/install-dir"}, "{tmp}/install-dir"),
        ({"UV_INSTALL_DIR": "{home}/.cargo"}, "{home}/.cargo/bin"),
        ({"CARGO_DIST_FORCE_INSTALL_DIR": "{tmp}/forced"}, "{tmp}/forced"),
        ({"UV_UNMANAGED_INSTALL": "{tmp}/unmanaged"}, "{tmp}/unmanaged"),
        ({"UV_UNMANAGED_INSTALL": "{tmp}/unmanaged", "XDG_BIN_HOME": "{tmp}/xdg-bin"}, "{tmp}/unmanaged"),
        ({"XDG_BIN_HOME": "{tmp}/xdg-bin", "XDG_DATA_HOME": "{tmp}/xdg/data"}, "{tmp}/xdg-bin"),
        ({"XDG_DATA_HOME": "{tmp}/xdg/data"}, "{tmp}/xdg/data/../bin"),
    ],
)
def test_finds_uv_where_the_uv_installer_put_it(sandbox, variables, target):
    tmp_path, env, _, fake_bin = sandbox
    _executable(fake_bin / "curl", STUB_CURL)
    fill = {"tmp": str(tmp_path), "home": env["HOME"]}
    variables = {name: value.format(**fill) for name, value in variables.items()}

    result = _run(env, **variables, FAKE_INSTALL_TARGET=target.format(**fill))

    assert result.returncode == 0, result.stderr
    assert "uv not found: installing it" in result.stdout
    assert any(call.startswith("tool install vodafone-cli @ ") for call in _calls(env))


def test_an_editable_install_is_left_alone_unless_forced(sandbox):
    _, env, stub_uv, fake_bin = sandbox
    shutil.copy(stub_uv, fake_bin / "uv")
    receipt = Path(env["STUB_ROOT"]) / "tools" / "vodafone-cli" / "uv-receipt.toml"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('[tool]\nrequirements = [{ name = "vodafone-cli", editable = "/src/vodafone-cli" }]\n')

    refused = _run(env)
    forced = _run(env, VODAFONE_CLI_FORCE="1")

    assert refused.returncode == 1 and "editable" in refused.stderr
    assert forced.returncode == 0, forced.stderr
    installs = [call for call in _calls(env) if call.startswith("tool install")]
    assert installs == ["tool install vodafone-cli @ git+https://github.com/luisgonzaleznf/vodafone-cli.git@v0.1.0 --force"]
