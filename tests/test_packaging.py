"""The distribution files agree: install.sh and the Homebrew formula pin the same release, and the
README's install line pins the version in pyproject.toml."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_install_script_and_formula_pin_the_same_release():
    script = re.search(r"^DEFAULT_VERSION=(\S+)$", (ROOT / "install.sh").read_text(), re.MULTILINE)
    formula = re.search(r'^\s+tag:\s+"v([^"]+)"', (ROOT / "Formula" / "vodafone.rb").read_text(), re.MULTILINE)

    assert script and formula
    assert script.group(1) == formula.group(1)


def test_the_readme_install_lines_pin_the_pyproject_version():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    readme = (ROOT / "README.md").read_text()
    pins = re.findall(r"git\+https://github\.com/luisgonzaleznf/vodafone-cli(\S*)", readme)

    assert len(pins) >= 1  # the uv tool install line
    assert set(pins) <= {f"@v{version}", f'@v{version}",'}
