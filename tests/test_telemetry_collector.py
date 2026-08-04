"""
tests/test_telemetry_collector.py — the thing that runs 336 times unattended.

Why this file exists
--------------------
The collector runs hourly for two weeks with nobody watching. Every other piece
of this project has a human in front of it within minutes of misbehaving; this
one has fourteen days of silence in which to be wrong.

The two failures that matter are not crashes. A crash is loud and costs one
sample.

  - **Silent duplication.** If the append logic re-adds rows it already stored,
    every percentile computed from the file is quietly wrong, no error is
    raised, and the calibration those numbers were collected for is built on
    them. Failing loudly would be better than this.
  - **A collector that never stops.** The date gate is the only thing standing
    between "two weeks of data" and a job firing forever. It gets tested from
    both sides of its boundary, like every threshold in this project.

Everything here runs without Docker, without network, and without GitHub — the
logic is in the scripts, and the workflow is asserted as text.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT     = Path(__file__).resolve().parent.parent
EXPORT   = ROOT / "scripts" / "export-history.py"
IMPORT   = ROOT / "scripts" / "import-history.py"
WORKFLOW = ROOT / ".github" / "workflows" / "telemetry.yml"
URL      = "https://collected.example"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )


def _seed_db(path: Path, count: int, *, minutes_apart: int = 60) -> list[str]:
    """A history.db with *count* rows, one per hour, like the collector makes."""
    sys.path.insert(0, str(ROOT))
    from history import SCHEMA

    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    stamps = []
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    with con:
        for i in range(count):
            stamp = (base + timedelta(minutes=i * minutes_apart)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            stamps.append(stamp)
            con.execute(
                "INSERT INTO probe_results (run_started_at, url, checked_at, "
                "status, ttfb_ms) VALUES (?, ?, ?, ?, ?)",
                (stamp, URL, stamp, "UP", 100.0 + i),
            )
    con.close()
    return stamps


# =============================================================================
# A. Duplication — the failure that stays silent
# =============================================================================


def test_the_append_cycle_does_not_duplicate(tmp_path: Path) -> None:
    """The collector's inner loop, run twice against unchanged data.

    `--since` is INCLUSIVE, so the second export always re-emits the boundary
    row. The workflow filters it against what is already stored. If that filter
    is wrong the file grows a duplicate every hour — 336 of them over the run —
    and every percentile computed from it is wrong with nothing raising.
    """
    db   = tmp_path / "history.db"
    _seed_db(db, 10)
    ndjson = tmp_path / "probes.ndjson"

    ndjson.write_text(_run(EXPORT, "--db", str(db)).stdout, encoding="utf-8")
    first = ndjson.read_text(encoding="utf-8").splitlines()
    assert len(first) == 10

    last  = json.loads(first[-1])["checked_at"]
    again = _run(EXPORT, "--db", str(db), "--since", last).stdout.splitlines()
    assert again == [first[-1]], "--since is inclusive: exactly the boundary row"

    stored = set(first)
    merged = first + [line for line in again if line not in stored]
    assert len(merged) == 10, "the boundary row must not be stored twice"


def test_importing_the_same_file_twice_changes_nothing(tmp_path: Path) -> None:
    """The other half of the same guarantee, on the analysis side. Doubling
    every measurement would halve every percentile without any error."""
    db     = tmp_path / "history.db"
    _seed_db(db, 25)
    ndjson = tmp_path / "probes.ndjson"
    ndjson.write_text(_run(EXPORT, "--db", str(db)).stdout, encoding="utf-8")

    rebuilt = tmp_path / "rebuilt.db"
    first   = _run(IMPORT, str(ndjson), "--db", str(rebuilt))
    second  = _run(IMPORT, str(ndjson), "--db", str(rebuilt))
    assert "25 filas nuevas" in first.stdout
    assert "0 filas nuevas" in second.stdout and "25 ya presentes" in second.stdout

    con = sqlite3.connect(rebuilt)
    assert con.execute("SELECT COUNT(*) FROM probe_results").fetchone()[0] == 25
    con.close()


def test_the_round_trip_preserves_the_numbers(tmp_path: Path) -> None:
    """Data that survives the trip but not intact is worse than data that does
    not survive: the analysis runs and answers wrongly."""
    db = tmp_path / "history.db"
    _seed_db(db, 30)
    ndjson = tmp_path / "probes.ndjson"
    ndjson.write_text(_run(EXPORT, "--db", str(db)).stdout, encoding="utf-8")
    rebuilt = tmp_path / "rebuilt.db"
    _run(IMPORT, str(ndjson), "--db", str(rebuilt))

    def stats(path: Path) -> tuple:
        con = sqlite3.connect(path)
        row = con.execute(
            "SELECT COUNT(*), MIN(ttfb_ms), MAX(ttfb_ms), MIN(checked_at), "
            "MAX(checked_at) FROM probe_results").fetchone()
        con.close()
        return row

    assert stats(rebuilt) == stats(db)


# =============================================================================
# B. Degrading instead of dying
# =============================================================================


def test_a_truncated_line_costs_only_itself(tmp_path: Path) -> None:
    """A push interrupted mid-write leaves half a line. Two weeks of samples
    must not be unreadable because the last one was cut in half."""
    db = tmp_path / "history.db"
    _seed_db(db, 5)
    ndjson = tmp_path / "probes.ndjson"
    ndjson.write_text(_run(EXPORT, "--db", str(db)).stdout + '{"url":"cut',
                      encoding="utf-8")

    result = _run(IMPORT, str(ndjson), "--db", str(tmp_path / "out.db"))
    assert result.returncode == 0
    assert "5 filas nuevas" in result.stdout
    assert "1 lineas ilegibles" in result.stdout, "counted, never silent"


def test_a_missing_database_exports_nothing_quietly(tmp_path: Path) -> None:
    """The first scheduled run has no history.db yet. That is a fresh
    checkout, not a fault, and it must not fail the job."""
    result = _run(EXPORT, "--db", str(tmp_path / "absent.db"))
    assert result.returncode == 0 and result.stdout == ""


def test_a_database_with_no_schema_exports_nothing(tmp_path: Path) -> None:
    """A file exists but no run ever wrote to it — the CI job touches one."""
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    result = _run(EXPORT, "--db", str(db))
    assert result.returncode == 0 and result.stdout == ""


def test_import_refuses_a_missing_file_loudly(tmp_path: Path) -> None:
    """Reading is a human action, unlike collecting. A typo in the path should
    say so rather than hand back an empty database that looks like a finding."""
    result = _run(IMPORT, str(tmp_path / "nope.ndjson"))
    assert result.returncode == 1


# =============================================================================
# C. The gate that stops it
# =============================================================================


@pytest.mark.parametrize("today,expired", [
    ("2026-08-10", False),
    ("2026-08-16", False),   # day before the end
    ("2026-08-17", False),   # the end date itself still collects
    ("2026-08-18", True),    # the day after, it stops
    ("2026-12-01", True),
])
def test_the_collection_window_closes_on_time(today, expired) -> None:
    """Both sides of the boundary, like every threshold here.

    The workflow compares ISO dates as STRINGS in bash. That is correct only
    because ISO-8601 sorts lexicographically — this asserts the property the
    comparison silently depends on, so a change to a different date format
    fails here rather than in week three of a run nobody is watching.
    """
    until = "2026-08-17"
    assert (today > until) is expired


def test_the_workflow_cannot_run_forever() -> None:
    """Three independent guards, asserted as present.

    A self-terminating collector that only terminates on the happy path is one
    that runs forever the first time something breaks — so the shutdown step
    has to be `always()`, not the last line of a successful job.
    """
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job  = spec["jobs"]["probe"]

    assert job.get("timeout-minutes"), "a stuck run would burn the 6h default"
    assert job["timeout-minutes"] <= 30

    gated = [s for s in job["steps"] if "expired" in str(s.get("if", ""))]
    assert gated, "no step is gated on the collection window"

    always = [s for s in job["steps"] if s.get("if") == "always()"]
    assert always, "the shutdown must run whatever happened above"
    assert "disable" in yaml.dump(always[0]), "and it must actually disable"

    assert spec["permissions"].get("actions") == "write", (
        "disabling itself needs this permission, and failing at that would "
        "leave the collector running past its window"
    )


def test_the_collector_never_pages_anyone() -> None:
    """It measures; it does not alert. A data-collection job that woke the
    owner 336 times would be removed within a day, and rightly."""
    body = WORKFLOW.read_text(encoding="utf-8")
    assert ".env.example" in body, "placeholders only — no real webhooks"
    assert "secrets.DISCORD" not in body and "secrets.SLACK" not in body


def test_the_collector_is_hourly_not_faster() -> None:
    """336 runs give 168 samples per window against the 20 the comparison
    needs. Doubling the rate buys nothing and costs twice the runner time."""
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    crons = [s["cron"] for s in spec[True]["schedule"]]
    assert len(crons) == 1, f"expected one schedule, found {crons}"

    # The cadence is the claim here, not the minute -- which minute it lands on
    # is a separate concern with its own test. Pinning the literal string made
    # this fail when the minute moved off the congested :00 slot, reporting a
    # cadence change that had not happened.
    minute, hour, dom, month, dow = crons[0].split()
    assert minute.isdigit(), f"{crons[0]!r} does not run once per hour"
    assert (hour, dom, month, dow) == ("*", "*", "*", "*"), (
        f"{crons[0]!r} is not a plain hourly schedule")


# =============================================================================
# D. Append, never replace — the whole point of collecting for two weeks
# =============================================================================


def test_three_consecutive_runs_accumulate(tmp_path: Path) -> None:
    """The property the two weeks depend on.

    Each run probes a few more rows and must ADD them to what is stored. A run
    that rewrote the file instead would leave, after fourteen days, exactly one
    hour of data — and nothing would have failed, so nobody would look until
    the analysis came back empty.

    Simulates three runs of the workflow's inner loop against a database that
    grows between them, which is what actually happens.
    """
    db     = tmp_path / "history.db"
    ndjson = tmp_path / "probes.ndjson"
    ndjson.touch()

    totals = []
    for run in range(3):
        _seed_db_append(db, start=run * 5, count=5)

        lines = [l for l in ndjson.read_text(encoding="utf-8").splitlines() if l]
        last  = json.loads(lines[-1])["checked_at"] if lines else None

        args = ["--db", str(db)] + (["--since", last] if last else [])
        fresh = _run(EXPORT, *args).stdout.splitlines()

        stored = set(lines)
        with ndjson.open("a", encoding="utf-8") as fh:
            for line in fresh:
                if line not in stored:
                    fh.write(line + "\n")

        totals.append(len(ndjson.read_text(encoding="utf-8").splitlines()))

    assert totals == [5, 10, 15], (
        f"each run must add to the file, not replace it — got {totals}"
    )


def test_the_earliest_rows_survive_to_the_end(tmp_path: Path) -> None:
    """Two weeks of collecting exist to make the OLDEST window readable. If
    early rows are lost the trend comparison has nothing to compare against,
    which is the one thing this whole exercise is for."""
    db     = tmp_path / "history.db"
    ndjson = tmp_path / "probes.ndjson"
    ndjson.touch()

    _seed_db_append(db, start=0, count=3)
    ndjson.write_text(_run(EXPORT, "--db", str(db)).stdout, encoding="utf-8")
    first_line = ndjson.read_text(encoding="utf-8").splitlines()[0]

    for run in range(1, 4):
        _seed_db_append(db, start=run * 3, count=3)
        lines = ndjson.read_text(encoding="utf-8").splitlines()
        last  = json.loads(lines[-1])["checked_at"]
        fresh = _run(EXPORT, "--db", str(db), "--since", last).stdout.splitlines()
        stored = set(lines)
        with ndjson.open("a", encoding="utf-8") as fh:
            for line in fresh:
                if line not in stored:
                    fh.write(line + "\n")

    final = ndjson.read_text(encoding="utf-8").splitlines()
    assert final[0] == first_line, "the very first row must still be there"
    assert len(final) == 12


def test_the_workflow_appends_and_never_truncates() -> None:
    """Asserted against the workflow text, because a single `>` where a `>>`
    belongs is a one-character edit that silently discards two weeks."""
    body   = WORKFLOW.read_text(encoding="utf-8")
    # Derived from the workflow, not hardcoded: renaming the collected
    # file must not quietly turn this guard into a no-op.
    match  = re.search(r"telemetry/[\w.-]+\.ndjson", body)
    assert match, "the workflow names no collected file"
    target = re.escape(match.group(0))

    # A single `>` NOT preceded by another one. Matching the plain substring
    # would flag every correct `>>`, because ">> file" contains "> file" --
    # the test would fail on the very thing it exists to require.
    truncating = re.compile(rf"(?<!>)>\s*{target}")
    for line in body.splitlines():
        assert not truncating.search(line), (
            f"this line truncates the collected file: {line.strip()}"
        )
    assert re.search(rf">>\s*{target}", body), (
        "nothing appends to the collected file"
    )


def test_the_window_is_bounded_at_both_ends() -> None:
    """A single end leaves the other open. A fork, a manual re-enable, or a
    clock that disagrees must not start collecting outside the window."""
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    env  = spec["env"]
    assert "COLLECT_FROM" in env and "COLLECT_UNTIL" in env

    start = datetime.strptime(env["COLLECT_FROM"], "%Y-%m-%dT%H:%M:%SZ")
    end   = datetime.strptime(env["COLLECT_UNTIL"], "%Y-%m-%dT%H:%M:%SZ")
    assert end > start
    assert (end - start) == timedelta(days=14), (
        "the window is meant to be exactly two weeks — the trend comparison "
        "needs two full seven-day halves"
    )


@pytest.mark.parametrize("now,expected", [
    ("2026-08-03T14:59:00Z", "early"),
    ("2026-08-03T15:00:00Z", "collect"),
    ("2026-08-10T12:00:00Z", "collect"),
    ("2026-08-17T14:59:00Z", "collect"),
    ("2026-08-17T15:00:00Z", "collect"),
    ("2026-08-17T15:01:00Z", "expired"),
])
def test_the_window_boundaries_from_both_sides(now, expected) -> None:
    """Timestamps, not bare dates: "the 17th" leaves it ambiguous whether the
    day starts or ends the window, and an unattended job is exactly where an
    ambiguity gets resolved the wrong way for a fortnight."""
    start, end = "2026-08-03T15:00:00Z", "2026-08-17T15:00:00Z"
    verdict = "expired" if now > end else "early" if now < start else "collect"
    assert verdict == expected


def _seed_db_append(path: Path, *, start: int, count: int) -> None:
    """Add rows to an existing history.db, the way a real run does."""
    sys.path.insert(0, str(ROOT))
    from history import SCHEMA

    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    with con:
        for i in range(start, start + count):
            stamp = (base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            con.execute(
                "INSERT INTO probe_results (run_started_at, url, checked_at, "
                "status, ttfb_ms) VALUES (?, ?, ?, ?, ?)",
                (stamp, URL, stamp, "UP", 100.0 + i),
            )
    con.close()


# =============================================================================
# E. Vantage points must not mix
# =============================================================================


def test_every_exported_row_can_carry_its_source(tmp_path: Path) -> None:
    """A latency figure without a vantage point is not a measurement.

    The same target from a home connection and from a datacenter are two
    numbers about two different network paths; averaged together they describe
    neither, and nothing about the resulting percentile looks wrong.
    """
    db = tmp_path / "history.db"
    _seed_db(db, 4)
    out = _run(EXPORT, "--db", str(db), "--source", "github-actions").stdout
    for line in out.splitlines():
        assert json.loads(line)["source"] == "github-actions"


def test_importing_two_vantage_points_refuses_loudly(tmp_path: Path) -> None:
    """The guard that turns a silent contamination into a stopped command.

    This actually happened: collected rows leaked into the source branch, the
    runner cloned them, and one file ended up holding measurements from a home
    laptop and from GitHub's datacenters with nothing to tell them apart.
    Nothing failed, and the numbers looked fine.
    """
    db = tmp_path / "history.db"
    _seed_db(db, 4)
    mine   = _run(EXPORT, "--db", str(db), "--source", "laptop").stdout.splitlines()
    theirs = _run(EXPORT, "--db", str(db), "--source", "runner").stdout.splitlines()

    mixed = tmp_path / "mixed.ndjson"
    mixed.write_text("\n".join(mine[:2] + theirs[2:]) + "\n", encoding="utf-8")

    result = _run(IMPORT, str(mixed), "--db", str(tmp_path / "mixed.db"))
    assert result.returncode == 2, "a mixed file must stop the command"
    assert "MEZCLA DE FUENTES" in result.stderr
    assert "laptop" in result.stderr and "runner" in result.stderr


def test_a_single_source_imports_and_names_itself(tmp_path: Path) -> None:
    """The other half: the guard must not block the normal case, and it should
    say which vantage point the resulting database describes."""
    db = tmp_path / "history.db"
    _seed_db(db, 4)
    single = tmp_path / "single.ndjson"
    single.write_text(
        _run(EXPORT, "--db", str(db), "--source", "github-actions").stdout,
        encoding="utf-8")

    result = _run(IMPORT, str(single), "--db", str(tmp_path / "one.db"))
    assert result.returncode == 0
    assert "fuente: github-actions" in result.stdout


def test_collected_data_never_lives_in_the_source_branch() -> None:
    """The leak, as an assertion.

    `git add -A` swept the collected file into main. The runner then cloned it,
    found the file already full of a laptop's measurements, and appended its
    own — two network paths in one dataset, unrecoverably.
    """
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "telemetry/" in ignored, (
        "collected data must be ignored in the source branch, or a broad "
        "`git add` will commit it and the collector will clone it back"
    )
    tracked = subprocess.run(
        ["git", "ls-files", "telemetry/"], cwd=str(ROOT),
        capture_output=True, text=True).stdout.strip()
    assert not tracked, f"collected data is tracked in the source branch: {tracked}"


def test_the_collector_labels_what_it_writes() -> None:
    """Its file says where it came from, and so does every row inside it."""
    body = WORKFLOW.read_text(encoding="utf-8")
    assert "--source github-actions" in body
    assert "telemetry/github-actions.ndjson" in body


def test_the_actions_run_on_a_supported_node() -> None:
    """Node 20 reached end of life; runners force Node 24 and warn on every
    run. A warning nobody acts on is training to ignore warnings."""
    for name in ("telemetry", "ci"):
        body = (ROOT / ".github" / "workflows" / f"{name}.yml").read_text(
            encoding="utf-8")
        assert "actions/checkout@v4" not in body, f"{name}: checkout on Node 20"
        assert "actions/setup-python@v5" not in body, f"{name}: setup-python on Node 20"


def test_the_collector_does_not_impersonate_a_real_account() -> None:
    """`<word>@users.noreply.github.com` resolves to the GitHub account whose
    username is <word>. Picking a descriptive word credited every collected
    commit to a stranger and listed them as a contributor to this repo.

    Attribution is not cosmetic: it is the record of who did the work, and
    putting someone else's name on a machine's commits is wrong regardless of
    intent. The bot identity carries a numeric ID precisely so it cannot
    collide with a human's username.
    """
    body = WORKFLOW.read_text(encoding="utf-8")
    emails = re.findall(r"user\.email\s+\"([^\"]+)\"", body)
    assert emails, "the collector sets no commit identity"
    for email in emails:
        assert email.startswith("41898282+github-actions[bot]@"), (
            f"{email} may resolve to a real GitHub account — use the "
            "numeric bot identity"
        )


def test_no_commit_in_this_repo_credits_a_stranger() -> None:
    """The same mistake, caught in the history instead of in the workflow.

    Setting that identity with `git config` and no --global writes it into
    .git/config, where it silently signs everything committed from this
    checkout afterwards. Four commits reached the public repo that way and
    GitHub listed the stranger as a contributor. The workflow test above
    guards the file; this one guards what actually shipped, which is the only
    place the damage is visible.

    Needs the full history: CI's default shallow clone fetches one commit, so
    ci.yml sets fetch-depth: 0 for this.
    """
    log = subprocess.run(
        ["git", "log", "--format=%H%x09%an <%ae>%x09%cn <%ce>"],
        cwd=str(ROOT), capture_output=True, text=True)
    if log.returncode != 0:
        pytest.skip("not a git checkout")

    # Anything@users.noreply.github.com resolves to the account with that
    # username. GitHub's own form carries the numeric user ID, which cannot
    # collide with a human's login -- that is the whole reason for the number.
    noreply = re.compile(r"<(?P<local>[^<>@]+)@users\.noreply\.github\.com>")
    offenders = []
    for line in log.stdout.splitlines():
        sha, _, rest = line.partition("\t")
        for match in noreply.finditer(rest):
            local = match.group("local")
            if not re.fullmatch(r"\d+\+.+", local):
                offenders.append(f"{sha[:10]}  {rest}")

    assert not offenders, (
        "commits credited to a GitHub account that may belong to someone "
        "else:\n  " + "\n  ".join(offenders) + "\n"
        "Fix the identity and rewrite, then `git config --local --unset "
        "user.email` so the next commit does not repeat it."
    )


def test_the_runner_can_actually_commit_the_file_it_collects(
        tmp_path: Path) -> None:
    """Runs git for real, because this bug is invisible to a text search.

    telemetry/ is gitignored so a broad `git add` cannot sweep collected data
    into main. The collector's orphan branch starts from main's working tree
    and inherits that .gitignore -- where the same rule blocks the one file
    the branch exists to hold. Both rules are correct; they collide.

    A scheduled run probed every target, exported the rows, and then died on
    `git add` with the measurements in memory. Nothing in the YAML looks
    wrong, which is why this asserts against git itself: the real .gitignore,
    the real filename, the real command.
    """
    repo = tmp_path / "runner"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    (repo / ".gitignore").write_text(
        (ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")

    # The exact path the workflow writes, taken from the workflow.
    body = WORKFLOW.read_text(encoding="utf-8")
    target = re.search(r"git add -f (\S+)", body)
    assert target, "the collector no longer force-adds a named path"
    data = repo / target.group(1)
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text('{"body_bytes": 1}\n', encoding="utf-8")

    plain = subprocess.run(
        ["git", "add", target.group(1)], cwd=str(repo),
        capture_output=True, text=True)
    assert plain.returncode != 0, (
        "the .gitignore no longer covers collected data — a broad `git add` "
        "on main can now commit it, which is the leak this rule prevents"
    )

    forced = subprocess.run(
        ["git", "add", "-f", target.group(1)], cwd=str(repo),
        capture_output=True, text=True)
    assert forced.returncode == 0, (
        f"the collector cannot stage its own data file: {forced.stderr}")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=str(repo),
        capture_output=True, text=True).stdout.split()
    assert staged == [target.group(1)], (
        f"expected exactly the data file staged, got {staged}")


def test_checking_out_the_data_branch_deletes_the_source_tree(
        tmp_path: Path) -> None:
    """The mechanism behind runs #4-#9, reproduced with real git.

    An orphan branch holds one file. `git checkout` makes the working tree
    match the branch, so switching to it REMOVES everything the branch does
    not contain -- scripts/, main.py, all of it. Nothing about the command
    says so, and the workflow read as if the source tree were still there.

    Run #3 passed because the branch did not exist yet and took the
    `--orphan` path, which creates it from the current tree. Every run after
    it found the branch, checked it out, and died on a missing script. A bug
    that only appears from the second run on is invisible to a first run.
    """
    repo = tmp_path / "runner"
    repo.mkdir()

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True,
            check=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "export-history.py").write_text("print(1)\n",
                                                        encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "source")

    # The data branch, exactly as the workflow builds it.
    git("checkout", "-q", "--orphan", "data")
    git("rm", "-rq", "--cached", ".")
    (repo / "telemetry").mkdir()
    (repo / "telemetry" / "rows.ndjson").write_text('{"a": 1}\n',
                                                     encoding="utf-8")
    git("add", "-f", "telemetry/rows.ndjson")
    git("commit", "-qm", "data")

    # -f because `git rm --cached` left the source files UNTRACKED in the
    # working tree, and git refuses to overwrite an untracked file when
    # switching to a branch that tracks one. The runner never hits this -- it
    # takes the orphan path once and never switches back -- so it is setup
    # noise here, not behaviour under test.
    git("checkout", "-q", "-f", "main")

    script = repo / "scripts" / "export-history.py"
    assert script.exists(), "precondicion: el script existe en main"

    git("checkout", "-q", "data")

    assert not script.exists(), (
        "git checkout of a one-file branch no longer removes the source tree "
        "— if that is genuinely true, the ordering guard below is obsolete; "
        "verify before deleting it"
    )


def test_the_export_runs_before_any_branch_switch() -> None:
    """The fix, as an assertion on order rather than on presence.

    Both commands can be in the file and the step still fails: what broke was
    the sequence. So this compares positions, and treats the whole class --
    anything that needs the source tree must precede any checkout.
    """
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()

    def first_command(pattern: str) -> tuple[int, str] | None:
        """Line number of the first EXECUTED line matching pattern.

        Comments are skipped deliberately. An earlier version of this test
        searched the whole file for "scripts/export-history.py" and matched the
        comment block explaining the ordering, which sits above the checkout —
        so it compared a comment's position and passed on the broken order.
        """
        for number, raw in enumerate(lines, start=1):
            text = raw.strip()
            if not text or text.startswith("#"):
                continue
            if re.search(pattern, text):
                return number, text
        return None

    export = first_command(r"^python\s+scripts/export-history\.py")
    assert export, "the collector no longer runs the export script"

    checkout = first_command(r"^git\s+checkout\b")
    assert checkout, "the collector no longer switches to the data branch"

    assert export[0] < checkout[0], (
        f"line {export[0]} `{export[1]}` runs after line {checkout[0]} "
        f"`{checkout[1]}`. Checking out the data branch deletes scripts/ from "
        "the working tree, so the export has to come first."
    )


def test_the_last_row_is_read_without_checking_out() -> None:
    """Reading the stored tail is what forced the bad ordering.

    The obvious way to find the last stored row is to check out the branch and
    `tail` the file — and that checkout is the deletion. `git show ref:path`
    reads the same bytes out of the commit with the working tree untouched,
    which is what lets the export stay first.
    """
    body = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r'git show "origin/\$DATA_BRANCH:', body), (
        "the collector reads the stored rows some other way — if it tails the "
        "checked-out file again, the ordering fix is undone"
    )


def test_the_schedule_avoids_the_busiest_minute() -> None:
    """:00 is where every hourly cron in the world lands.

    Measured on the first eight hours at "0 * * * *": not one run started on
    the hour, the median delay was ~50 minutes, and one hour was skipped
    outright. A delay costs nothing here; a skip costs a sample.
    """
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # `on:` is the YAML 1.1 boolean True, which is why this is not doc["on"].
    crons = [entry["cron"] for entry in doc[True]["schedule"]]
    assert crons, "the collector is no longer scheduled"
    for cron in crons:
        minute = cron.split()[0]
        assert minute not in ("0", "00", "*"), (
            f"cron {cron!r} runs on the congested :00 slot"
        )


def test_the_collector_never_stages_a_wildcard() -> None:
    """-f is safe only because it names one path.

    `git add -A -f` or `git add -f .` would override the ignore rule for
    everything at once, which is the sweep that leaked the data in the first
    place — with the protection now explicitly disabled.
    """
    body = WORKFLOW.read_text(encoding="utf-8")
    for command in re.findall(r"^\s*git add .*$", body, re.MULTILINE):
        assert not re.search(r"git add\b.*(-A|--all|\s\.\s*$)", command), (
            f"the collector stages a wildcard: {command.strip()}"
        )
