"""
tests/test_gitattributes.py -- line endings are pinned by the repository, not by
whoever happens to clone it.

Why this exists
---------------
Without a .gitattributes, what ends up in a file's line endings depends on the
committer's `core.autocrlf`. A shell script committed with CRLF reaches a Linux
container as `#!/bin/sh\\r`, and the kernel answers "bad interpreter: No such
file or directory" for a file that is plainly there. The failure is at run time,
on the other machine, with a message that does not mention line endings.

These run real git against a scratch repository whose client setting is the
hostile one (`core.autocrlf=false`), because a text search of .gitattributes
would pass on a file git then ignores.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """A repository carrying this repo's .gitattributes and a client that does no
    conversion of its own, so any normalisation seen is the attributes'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "core.safecrlf", "false")
    attributes = ROOT / ".gitattributes"
    assert attributes.exists(), "the repository has no .gitattributes"
    (repo / ".gitattributes").write_bytes(attributes.read_bytes())
    return repo


@pytest.mark.parametrize("name", [
    "scripts/init-watchdog.sh", "main.py", "Dockerfile", "docker-compose.yml",
    "README.md",
])
def test_a_file_committed_with_crlf_is_stored_with_lf(scratch: Path, name: str) -> None:
    path = scratch / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\r\necho hello\r\n")

    assert _git(scratch, "add", name).returncode == 0
    eol = _git(scratch, "ls-files", "--eol", name).stdout

    assert "i/lf" in eol, f"{name} entered the index with its CRLF intact: {eol!r}"


def test_a_shell_script_is_checked_out_with_lf_whatever_the_clients_setting(
        scratch: Path) -> None:
    """The half that reaches the container: what the working tree holds after a
    checkout, from a client set to convert to CRLF."""
    script = scratch / "scripts" / "run.sh"
    script.parent.mkdir()
    script.write_bytes(b"#!/bin/sh\necho hello\n")
    assert _git(scratch, "add", "scripts/run.sh").returncode == 0
    assert _git(scratch, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                "commit", "-q", "-m", "x").returncode == 0

    script.unlink()
    assert _git(scratch, "-c", "core.autocrlf=true", "checkout", "--", "scripts/run.sh"
                ).returncode == 0

    assert script.read_bytes() == b"#!/bin/sh\necho hello\n", (
        "the shell script was checked out with CRLF; it would not run on Linux"
    )


def test_no_tracked_file_carries_crlf_in_the_index() -> None:
    """The repository as it stands: everything git stores is LF."""
    listing = _git(ROOT, "ls-files", "--eol")
    if listing.returncode != 0:
        pytest.skip("not a git checkout")
    offenders = [line for line in listing.stdout.splitlines()
                 if line.startswith(("i/crlf", "i/mixed"))]
    assert not offenders, "tracked files stored with CRLF:\n  " + "\n  ".join(offenders)
