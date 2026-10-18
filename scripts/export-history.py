"""
export-history.py — dump probe rows as NDJSON, for append-only collection.

Why NDJSON and not the SQLite file
------------------------------------
The obvious move is to commit history.db after each run. At ~213 bytes a row
that file is small, but git stores a FULL copy of a binary blob on every
change: 672 runs over two weeks would leave a few hundred megabytes of objects
behind to hold half a megabyte of data.

One line per row, appended, diffs the way git is built for — each commit adds a
few lines and stores a few lines. The same two weeks costs well under a
megabyte of history.

The import side (import-history.py) turns it back into a real history.db, so
--report and the trend comparison work on it unchanged.

Where it looks
--------------
--db wins. Without it, HISTORY_DB_PATH (the variable compose sets and the
monitor honours), else data/history/history.db, where the history now lives.
A native `python main.py` run with neither set still writes
./history.db (history.HISTORY_DB_DEFAULT), so that file is used when the new
default is absent.

A database that is not there is an error, not an empty export. "Probed
nothing" and "looked in the wrong place" produce the same empty file, and only
one of them is a fresh checkout.

Usage:
    python scripts/export-history.py [--since ISO8601] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path("data/history/history.db")
LEGACY_DB = Path("history.db")

COLUMNS = (
    "run_started_at", "url", "checked_at", "status", "error", "http_status",
    "rtt_ms", "dns_ms", "connect_total_ms", "tls_ms", "ttfb_ms",
    "server_processing_ms", "transfer_ms", "body_bytes", "goodput_bps",
)


def default_db() -> Path:
    """HISTORY_DB_PATH, else data/history/history.db, else ./history.db."""
    # A blank value counts as unset, as history.history_path_from_env does:
    # compose renders an unset ${VAR:-} as "", and Path("") is the working
    # directory itself.
    raw = os.environ.get("HISTORY_DB_PATH", "").strip()
    if raw:
        return Path(raw)
    if not DEFAULT_DB.exists() and LEGACY_DB.exists():
        return LEGACY_DB
    return DEFAULT_DB


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--db", default=None, type=Path,
        help="The history database. Default: $HISTORY_DB_PATH, else "
             "data/history/history.db (else ./history.db if only that exists).",
    )
    parser.add_argument(
        "--source", default=None,
        help="Where these measurements were taken. A latency figure is "
             "meaningless without it: the same target measured from a home "
             "connection and from a datacenter are two numbers about two "
             "different paths, and averaged together they describe neither.",
    )
    parser.add_argument(
        "--since", default=None,
        help="ISO-8601 UTC. Only rows at or after this instant. Omitted "
             "exports everything, which is what a first run wants.",
    )
    args = parser.parse_args()

    if args.db is None:
        args.db = default_db()

    # One line on stderr, nothing on stdout: a caller redirecting stdout to a
    # file must not get an empty file that looks like a successful export.
    if not args.db.exists():
        print(f"export-history.py: history database not found: {args.db} "
              "(pass --db or set HISTORY_DB_PATH)", file=sys.stderr)
        return 1
    if not args.db.is_file():
        # The trap compose documents: a single-file bind mount from a path that
        # did not exist makes Docker create a DIRECTORY there. sqlite fails to
        # open it with the same error as an empty schema, which used to read
        # below as "nothing probed".
        print(f"export-history.py: history database is not a file: {args.db}",
              file=sys.stderr)
        return 1

    con = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    try:
        sql = f"SELECT {', '.join(COLUMNS)} FROM probe_results"  # noqa: S608
        params: tuple = ()
        if args.since:
            sql += " WHERE checked_at >= ?"
            params = (args.since,)
        sql += " ORDER BY checked_at"
        rows = con.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return 0                      # no schema yet: nothing probed, nothing lost
    finally:
        con.close()

    out = sys.stdout
    try:
        _write(out, rows, args.source)
    except (BrokenPipeError, OSError):
        # `export-history.py | head` closes the pipe mid-write. That is a
        # normal thing to type and must not look like the export failed, so it
        # exits quietly instead of on a traceback that says nothing useful.
        try:
            sys.stdout.close()
        except OSError:
            pass
    return 0


def _write(out, rows, source: str | None = None) -> None:
    for row in rows:
        record = dict(zip(COLUMNS, row))
        if source:
            # Stamped on every row, not just named in the filename. A file can
            # be concatenated with another, and once two vantage points share
            # one file with nothing distinguishing them there is no way back:
            # every percentile averages two different network paths and
            # describes neither, and nothing about the number looks wrong.
            record["source"] = source
        # ensure_ascii=False keeps a URL or an error message readable in the
        # committed file; sort_keys makes two exports of the same row
        # byte-identical, so a re-run cannot produce a spurious diff.
        out.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
