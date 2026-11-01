"""
tests/test_data_mounts.py -- every directory compose mounts exists before compose does.

When a bind-mount source does not exist, Docker creates it, as root, mode 755.
On a Linux host that directory is then unwritable for the containers, which run
as uid 999, and nothing in `docker compose up -d` says so: it exits 0 while the
service restarts forever. It has happened three times here -- history.db born as
a directory, data/history, and then data/watchdog on a clean clone, where the
self-hosted watchdog crash-looped with "unable to open database file" and
init-watchdog.sh gave up two minutes later with "Could not create the check".

Two defences, one per half of the failure:
  - the directory ships in the repository, so it exists before Docker touches it
    and the README's setfacl step covers it;
  - the watchdog's guard writes into that directory as the watchdog's uid before
    the watchdog may start, and stops `up` with the command that fixes it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def _data_dir_mounts() -> set[str]:
    """Host directories under ./data that any service bind-mounts."""
    dirs = set()
    for svc in _compose()["services"].values():
        for vol in svc.get("volumes", []):
            source = vol.split(":")[0]
            if source.startswith("./data") and not Path(source).suffix:
                dirs.add(source.removeprefix("./"))
    return dirs


def _tracked() -> set[str]:
    out = subprocess.run(["git", "ls-files", "data"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return set(out.split())


def test_the_mounted_data_directories_are_found() -> None:
    """Guards the guard: a parser that found nothing would pass everything."""
    assert {"data", "data/history", "data/watchdog"} <= _data_dir_mounts()


def test_every_mounted_data_directory_ships_in_the_repository() -> None:
    tracked = _tracked()
    for directory in _data_dir_mounts():
        assert any(f.startswith(directory + "/") for f in tracked), (
            f"{directory}/ is bind-mounted but not in the repository: on a clean "
            f"clone Docker creates it as root and the container cannot write it. "
            f"Add {directory}/.gitkeep and its .gitignore exception."
        )


def _service(name: str) -> dict:
    return _compose()["services"][name]


def test_the_guard_checks_the_watchdogs_directory_as_the_watchdogs_uid() -> None:
    guard, watchdog = _service("watchdog-guard"), _service("watchdog")
    watchdog_source = next(v.split(":")[0] for v in watchdog["volumes"])
    guard_mounts = {v.split(":")[0]: v.split(":")[1] for v in guard.get("volumes", [])}
    assert watchdog_source in guard_mounts, "the guard does not see the watchdog's directory"
    # The image's user: hc, uid 999, in v4.4 and v5.0.3 alike (checked 2026-11-01).
    assert str(guard.get("user", "")).split(":")[0] == "999", guard.get("user")
    script = guard["command"][2]
    target = guard_mounts[watchdog_source]
    assert re.search(rf"touch\s+\"?{re.escape(target)}/", script), "the guard never tries a write"
    assert "setfacl" in script, "the refusal must say how to fix it"
    assert "exit 1" in script


def test_the_watchdog_still_waits_for_the_guard() -> None:
    depends = _service("watchdog")["depends_on"]["watchdog-guard"]
    assert depends["condition"] == "service_completed_successfully"
