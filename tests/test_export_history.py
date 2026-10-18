"""
tests/test_export_history.py -- export-history.py and import-history.py.

Why this file exists
--------------------
History moved to data/history/history.db (HISTORY_DB_PATH under compose) and the
script's default stayed `history.db`. Run bare, it answered "no such file" with
a quiet success and an empty export, and the empty export is indistinguishable
from a monitor that measured nothing. A path that is wrong must say so.

The second half covers the NDJSON round trip between the two scripts: an
incremental export must not duplicate or drop rows, and an import must not mix
measurements taken from different vantage points.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import history

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "export-history.py"
URL = "https://example.invalid/health"


def _make_db(path: Path, url: str = URL) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(history.SCHEMA)
        con.execute(
            "INSERT INTO probe_results (run_started_at, url, checked_at, status) "
            "VALUES (?, ?, ?, ?)",
            ("2026-10-01T00:00:00+00:00", url, "2026-10-01T00:00:05+00:00", "UP"))
        con.commit()
    finally:
        con.close()
    return path


def _run(cwd: Path, *args: str, history_db_path: str | None = None
         ) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "HISTORY_DB_PATH"}
    if history_db_path is not None:
        env["HISTORY_DB_PATH"] = history_db_path
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=cwd, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=60)


def _exported_urls(proc: subprocess.CompletedProcess) -> list[str]:
    return [json.loads(line)["url"] for line in proc.stdout.splitlines()]


def test_the_default_is_the_directory_the_history_moved_to(tmp_path: Path) -> None:
    _make_db(tmp_path / "data" / "history" / "history.db")

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert _exported_urls(proc) == [URL]


def test_history_db_path_wins_over_the_default(tmp_path: Path) -> None:
    _make_db(tmp_path / "data" / "history" / "history.db", url="https://wrong.invalid/")
    elsewhere = _make_db(tmp_path / "elsewhere" / "h.db")

    proc = _run(tmp_path, history_db_path=str(elsewhere))

    assert proc.returncode == 0, proc.stderr
    assert _exported_urls(proc) == [URL]


def test_an_explicit_db_wins_over_the_environment(tmp_path: Path) -> None:
    _make_db(tmp_path / "env.db", url="https://wrong.invalid/")
    chosen = _make_db(tmp_path / "chosen.db")

    proc = _run(tmp_path, "--db", str(chosen),
                history_db_path=str(tmp_path / "env.db"))

    assert _exported_urls(proc) == [URL]


def test_a_blank_history_db_path_counts_as_unset(tmp_path: Path) -> None:
    """compose renders an unset ${VAR:-} as an empty string, and Path("") is the
    working directory -- a directory where a file belongs."""
    _make_db(tmp_path / "data" / "history" / "history.db")

    proc = _run(tmp_path, history_db_path="  ")

    assert proc.returncode == 0, proc.stderr
    assert _exported_urls(proc) == [URL]


def test_the_root_history_db_still_works_for_a_native_run(tmp_path: Path) -> None:
    """main.py outside compose writes ./history.db (history.HISTORY_DB_DEFAULT)
    and a native `python main.py` user exports it with no arguments. Moving the
    default must not strand that caller."""
    _make_db(tmp_path / "history.db")

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert _exported_urls(proc) == [URL]


@pytest.mark.parametrize("how", ["flag", "environment", "default"])
def test_a_missing_database_is_one_clear_line_not_an_empty_export(
        tmp_path: Path, how: str) -> None:
    missing = tmp_path / "nope" / "history.db"
    if how == "flag":
        proc = _run(tmp_path, "--db", str(missing))
    elif how == "environment":
        proc = _run(tmp_path, history_db_path=str(missing))
    else:
        proc = _run(tmp_path)

    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "Traceback" not in proc.stderr
    lines = proc.stderr.strip().splitlines()
    assert len(lines) == 1, proc.stderr
    assert "not found" in lines[0]
    if how != "default":
        assert str(missing) in lines[0]


def test_a_directory_where_the_database_belongs_is_named_as_such(tmp_path: Path) -> None:
    """The trap compose documents: a single-file bind mount from a path that
    does not exist makes Docker create a DIRECTORY there. sqlite then fails to
    open it, and that used to read as "no schema yet" -- an empty export."""
    trap = tmp_path / "history.db"
    trap.mkdir()

    proc = _run(tmp_path, "--db", str(trap))

    assert proc.returncode != 0
    assert proc.stdout == ""
    lines = proc.stderr.strip().splitlines()
    assert len(lines) == 1, proc.stderr
    assert "not a file" in lines[0]


# =============================================================================
# The export/import round trip -- append-only history that must not duplicate,
# lose its oldest rows or mix vantage points
#
# Anyone who keeps the NDJSON between runs (a cron job appending to a file under
# version control, say) depends on these properties, and none of them fails
# loudly when broken: a duplicated row only skews a percentile.
# =============================================================================

IMPORT_SCRIPT = SCRIPT.with_name("import-history.py")
ROOT = SCRIPT.parent.parent
SOURCE_URL = "https://collected.example"


def _script(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        timeout=60)


def _seed_db(path: Path, count: int, *, start: int = 0) -> list[str]:
    """A history.db with *count* hourly rows beginning *start* hours in. Called
    again on the same path it adds rows, the way successive monitor runs do."""
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    stamps = []
    con = sqlite3.connect(path)
    try:
        con.executescript(history.SCHEMA)
        with con:
            for i in range(start, start + count):
                stamp = (base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
                stamps.append(stamp)
                con.execute(
                    "INSERT INTO probe_results (run_started_at, url, checked_at, "
                    "status, ttfb_ms) VALUES (?, ?, ?, ?, ?)",
                    (stamp, SOURCE_URL, stamp, "UP", 100.0 + i),
                )
    finally:
        con.close()
    return stamps


def _append_new(ndjson: Path, db: Path) -> None:
    """One incremental export: ask for rows from the last stored instant on and
    append only the lines not already in the file. `--since` is inclusive, so
    the boundary row comes back every time and has to be filtered out."""
    lines = [ln for ln in ndjson.read_text(encoding="utf-8").splitlines() if ln]
    args = ["--db", str(db)]
    if lines:
        args += ["--since", json.loads(lines[-1])["checked_at"]]
    fresh = _script(SCRIPT, *args).stdout.splitlines()
    stored = set(lines)
    with ndjson.open("a", encoding="utf-8") as fh:
        for line in fresh:
            if line not in stored:
                fh.write(line + "\n")


def test_the_append_cycle_does_not_duplicate(tmp_path: Path) -> None:
    """`--since` is INCLUSIVE, so a second export always re-emits the boundary
    row, and the caller filters it against what is already stored. The file
    must come out with each row once: a duplicate per run would quietly skew
    every percentile computed from it, with nothing raising."""
    db = tmp_path / "history.db"
    _seed_db(db, 10)
    ndjson = tmp_path / "probes.ndjson"

    ndjson.write_text(_script(SCRIPT, "--db", str(db)).stdout, encoding="utf-8")
    first = ndjson.read_text(encoding="utf-8").splitlines()
    assert len(first) == 10

    last = json.loads(first[-1])["checked_at"]
    again = _script(SCRIPT, "--db", str(db), "--since", last).stdout.splitlines()
    assert again == [first[-1]], "--since is inclusive: exactly the boundary row"

    _append_new(ndjson, db)
    assert len(ndjson.read_text(encoding="utf-8").splitlines()) == 10, (
        "the boundary row must not be stored twice")


def test_importing_the_same_file_twice_changes_nothing(tmp_path: Path) -> None:
    """The other half of the same guarantee, on the analysis side. Doubling
    every measurement would halve every percentile without any error."""
    db = tmp_path / "history.db"
    _seed_db(db, 25)
    ndjson = tmp_path / "probes.ndjson"
    ndjson.write_text(_script(SCRIPT, "--db", str(db)).stdout, encoding="utf-8")

    rebuilt = tmp_path / "rebuilt.db"
    first = _script(IMPORT_SCRIPT, str(ndjson), "--db", str(rebuilt))
    second = _script(IMPORT_SCRIPT, str(ndjson), "--db", str(rebuilt))
    assert "25 new rows" in first.stdout
    assert "0 new rows" in second.stdout and "25 already present" in second.stdout

    con = sqlite3.connect(rebuilt)
    assert con.execute("SELECT COUNT(*) FROM probe_results").fetchone()[0] == 25
    con.close()


def test_the_round_trip_preserves_the_numbers(tmp_path: Path) -> None:
    """Data that survives the trip but not intact is worse than data that does
    not survive: the analysis runs and answers wrongly."""
    db = tmp_path / "history.db"
    _seed_db(db, 30)
    ndjson = tmp_path / "probes.ndjson"
    ndjson.write_text(_script(SCRIPT, "--db", str(db)).stdout, encoding="utf-8")
    rebuilt = tmp_path / "rebuilt.db"
    _script(IMPORT_SCRIPT, str(ndjson), "--db", str(rebuilt))

    def stats(path: Path) -> tuple:
        con = sqlite3.connect(path)
        row = con.execute(
            "SELECT COUNT(*), MIN(ttfb_ms), MAX(ttfb_ms), MIN(checked_at), "
            "MAX(checked_at) FROM probe_results").fetchone()
        con.close()
        return row

    assert stats(rebuilt) == stats(db)


def test_a_truncated_line_costs_only_itself(tmp_path: Path) -> None:
    """A write interrupted mid-line leaves half a record. Weeks of samples must
    not become unreadable because the last one was cut in half."""
    db = tmp_path / "history.db"
    _seed_db(db, 5)
    ndjson = tmp_path / "probes.ndjson"
    ndjson.write_text(_script(SCRIPT, "--db", str(db)).stdout + '{"url":"cut',
                      encoding="utf-8")

    result = _script(IMPORT_SCRIPT, str(ndjson), "--db", str(tmp_path / "out.db"))
    assert result.returncode == 0
    assert "5 new rows" in result.stdout
    assert "1 unreadable lines" in result.stdout, "counted, never silent"


def test_a_database_with_no_schema_exports_nothing(tmp_path: Path) -> None:
    """A file exists but no run ever wrote to it: nothing probed, nothing lost,
    and not an error -- unlike a database that is missing altogether."""
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    result = _script(SCRIPT, "--db", str(db))
    assert result.returncode == 0 and result.stdout == ""


def test_import_refuses_a_missing_file_loudly(tmp_path: Path) -> None:
    """Importing is a human action. A typo in the path should say so rather
    than hand back an empty database that looks like a result."""
    result = _script(IMPORT_SCRIPT, str(tmp_path / "nope.ndjson"))
    assert result.returncode == 1


def test_three_consecutive_runs_accumulate(tmp_path: Path) -> None:
    """Each run adds a few rows to the database and must ADD them to what is
    stored. A caller that rewrote the file instead would hold exactly one run's
    worth of data after any number of runs, and nothing would have failed."""
    db = tmp_path / "history.db"
    ndjson = tmp_path / "probes.ndjson"
    ndjson.touch()

    totals = []
    for run in range(3):
        _seed_db(db, 5, start=run * 5)
        _append_new(ndjson, db)
        totals.append(len(ndjson.read_text(encoding="utf-8").splitlines()))

    assert totals == [5, 10, 15], (
        f"each run must add to the file, not replace it -- got {totals}")


def test_the_earliest_rows_survive_to_the_end(tmp_path: Path) -> None:
    """The trend comparison needs the OLDEST window as its baseline. If early
    rows are lost on the way, it has nothing to compare against."""
    db = tmp_path / "history.db"
    ndjson = tmp_path / "probes.ndjson"
    ndjson.touch()

    _seed_db(db, 3)
    ndjson.write_text(_script(SCRIPT, "--db", str(db)).stdout, encoding="utf-8")
    first_line = ndjson.read_text(encoding="utf-8").splitlines()[0]

    for run in range(1, 4):
        _seed_db(db, 3, start=run * 3)
        _append_new(ndjson, db)

    final = ndjson.read_text(encoding="utf-8").splitlines()
    assert final[0] == first_line, "the very first row must still be there"
    assert len(final) == 12


def test_every_exported_row_can_carry_its_source(tmp_path: Path) -> None:
    """A latency figure without a vantage point is not a measurement.

    The same target from a home connection and from a datacenter are two
    numbers about two different network paths; averaged together they describe
    neither, and nothing about the resulting percentile looks wrong.
    """
    db = tmp_path / "history.db"
    _seed_db(db, 4)
    out = _script(SCRIPT, "--db", str(db), "--source", "datacenter").stdout
    assert out.splitlines(), "nothing was exported"
    for line in out.splitlines():
        assert json.loads(line)["source"] == "datacenter"


def test_importing_two_vantage_points_refuses_loudly(tmp_path: Path) -> None:
    """The guard that turns a silent contamination into a stopped command.

    Rows from a home laptop and from a datacenter once ended up in one file
    with nothing to tell them apart. Nothing failed, and the numbers looked
    fine.
    """
    db = tmp_path / "history.db"
    _seed_db(db, 4)
    mine = _script(SCRIPT, "--db", str(db), "--source", "laptop").stdout.splitlines()
    theirs = _script(SCRIPT, "--db", str(db), "--source", "datacenter").stdout.splitlines()

    mixed = tmp_path / "mixed.ndjson"
    mixed.write_text("\n".join(mine[:2] + theirs[2:]) + "\n", encoding="utf-8")

    result = _script(IMPORT_SCRIPT, str(mixed), "--db", str(tmp_path / "mixed.db"))
    assert result.returncode == 2, "a mixed file must stop the command"
    assert "MIXED SOURCES" in result.stderr
    assert "laptop" in result.stderr and "datacenter" in result.stderr


def test_a_single_source_imports_and_names_itself(tmp_path: Path) -> None:
    """The other half: the guard must not block the normal case, and it should
    say which vantage point the resulting database describes."""
    db = tmp_path / "history.db"
    _seed_db(db, 4)
    single = tmp_path / "single.ndjson"
    single.write_text(
        _script(SCRIPT, "--db", str(db), "--source", "datacenter").stdout,
        encoding="utf-8")

    result = _script(IMPORT_SCRIPT, str(single), "--db", str(tmp_path / "one.db"))
    assert result.returncode == 0
    assert "source: datacenter" in result.stdout
