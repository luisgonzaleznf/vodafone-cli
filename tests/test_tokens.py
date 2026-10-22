"""FileTokenStore writes: exclusive private temp files, never through a precreated name. Synthetic values only."""

import json
import os
import stat

import pytest

from vodafone.tokens import FileTokenStore

ACCESS = "A" * 28
CSRF = "c" * 36
FRESH = {"accessToken": ACCESS, "userCsrfToken": CSRF}


def _write(store: FileTokenStore, how: str) -> None:
    if how == "write":
        store.write(FRESH)
    else:
        store.path.write_text(json.dumps({"accessToken": "O" * 28, "userCsrfToken": CSRF}))
        os.chmod(store.path, 0o600)
        store.update({"accessToken": ACCESS})


@pytest.mark.parametrize("how", ["write", "update"])
def test_a_precreated_loose_temp_file_is_neither_used_nor_touched(tmp_path, how):
    store = FileTokenStore(tmp_path / "tokens.json")
    decoy = tmp_path / ".tokens.json.tmp"  # the name the previous version always used
    decoy.write_text("decoy")
    os.chmod(decoy, 0o644)

    _write(store, how)

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.load().access_token == ACCESS
    assert decoy.read_text() == "decoy" and stat.S_IMODE(decoy.stat().st_mode) == 0o644


@pytest.mark.parametrize("how", ["write", "update"])
def test_a_temp_name_symlinked_elsewhere_is_never_written_through(tmp_path, how):
    store = FileTokenStore(tmp_path / "tokens.json")
    victim = tmp_path / "unrelated.txt"
    victim.write_text("keep me")
    (tmp_path / ".tokens.json.tmp").symlink_to(victim)

    _write(store, how)

    assert victim.read_text() == "keep me"
    assert store.path.is_file() and not store.path.is_symlink()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_writes_leave_no_temp_files_behind(tmp_path):
    store = FileTokenStore(tmp_path / "sub" / "tokens.json")
    store.write(FRESH)
    store.update({"refreshToken": "r" * 32})

    assert sorted(p.name for p in store.path.parent.iterdir()) == ["tokens.json"]


def test_concurrent_writers_use_distinct_temp_files(tmp_path, monkeypatch):
    store = FileTokenStore(tmp_path / "tokens.json")
    temps = []
    real_replace = os.replace

    def spy(src, dst):
        temps.append(src)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    store.write(FRESH)
    store.write(FRESH)

    assert len(set(temps)) == 2
