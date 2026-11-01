"""
tests/test_collection_git_add.py -- the collector stages its data file,
and only its data file.

The collector's `git add -f` runs on the data branch inside the runner's
checkout. Two ways it goes wrong without a test noticing:
  - Widened to `-A` or a glob, it commits whatever the probe left behind in the
    working tree (targets.yaml, state, a database) to a public branch.
  - Without `-f`, a .gitignore that covers the data path (`collected.ndjson`
    already is) makes the add fail and every run ends with nothing stored.

Local git only: no network, no commits.
"""

from pathlib import Path
import re
import shlex
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "data-collection.yml"


def _data_file_and_add_commands():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    data_file = workflow["env"]["DATA_FILE"]
    path = Path(data_file)
    assert not path.is_absolute() and ".." not in path.parts
    commands = []
    for step in workflow["jobs"]["probe"]["steps"]:
        for line in step.get("run", "").splitlines():
            if re.match(r"^\s*git\s+add\b", line):
                commands.append(shlex.split(line.strip()))
    assert commands, "no step stages the collected data"
    return data_file, commands


def test_collector_force_add_is_limited_to_its_data_file():
    data_file, commands = _data_file_and_add_commands()
    for command in commands:
        assert command[:2] == ["git", "add"]
        args = command[2:]
        assert "-f" in args or "--force" in args
        paths = [a for a in args if a not in ("-f", "--force", "--")]
        assert paths in ([data_file], ["$DATA_FILE"], ["${DATA_FILE}"]), (
            "the collector must stage DATA_FILE alone: no globs, no -A"
        )


def test_collector_can_stage_data_even_when_the_path_is_ignored(tmp_path):
    data_file, commands = _data_file_and_add_commands()
    repo = tmp_path / "isolated-collector"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", "--no-optional-locks", *args], cwd=repo,
                              capture_output=True, text=True, timeout=15)

    assert git("init", "-q").returncode == 0
    (repo / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    # The real .gitignore does not cover DATA_FILE today. Ignore it explicitly
    # here, so the test proves the add survives that without imposing a rule
    # on the project's .gitignore.
    with (repo / ".gitignore").open("a", encoding="utf-8") as ignored:
        ignored.write("\n/" + data_file + "\n")
    data = repo / data_file
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text('{"body_bytes": 1}\n', encoding="utf-8")
    assert git("add", "--", data_file).returncode != 0, (
        "the scenario must start with DATA_FILE ignored")
    for command in commands:
        expanded = [data_file if a in ("$DATA_FILE", "${DATA_FILE}") else a
                    for a in command[1:]]
        result = git(*expanded)
        assert result.returncode == 0, result.stderr
    assert git("diff", "--cached", "--name-only").stdout.splitlines() == [data_file]
