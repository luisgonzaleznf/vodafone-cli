"""The distribution files agree: install.sh and the Homebrew formula pin the same release."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_install_script_and_formula_pin_the_same_release():
    script = re.search(r"^DEFAULT_VERSION=(\S+)$", (ROOT / "install.sh").read_text(), re.MULTILINE)
    formula = re.search(r'^\s+tag:\s+"v([^"]+)"', (ROOT / "Formula" / "vodafone.rb").read_text(), re.MULTILINE)

    assert script and formula
    assert script.group(1) == formula.group(1)
