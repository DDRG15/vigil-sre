"""
tests/test_install_instructions.py — a visitor can install from the README.

The README's only install line was `pip install -r requirements-dev.txt`, and
requirements-dev.txt did not include requirements.txt. Followed to the letter
in a fresh environment, that installed pytest but not PyYAML or python-dotenv
(the first test failed with "No module named 'yaml'"), and aiohttp arrived
unpinned, pulled in by aioresponses at whatever version was newest. Nothing in
the repo said how to run the service without Docker at all.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _lines(name: str) -> list[str]:
    text = (ROOT / name).read_text(encoding="utf-8")
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


def test_the_dev_requirements_include_the_runtime_ones():
    assert "-r requirements.txt" in _lines("requirements-dev.txt"), (
        "installing the test requirements must also install the pinned runtime ones")


def test_every_runtime_requirement_is_pinned():
    for line in _lines("requirements.txt"):
        assert "==" in line, f"unpinned runtime requirement: {line!r}"


def test_the_readme_says_how_to_install_and_run_without_docker():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "pip install -r requirements.txt" in readme, "no runtime install line"
    assert "python -m venv" in readme, "no virtual environment step"
    assert "Python 3.11" in readme, "the Python version is never stated"
