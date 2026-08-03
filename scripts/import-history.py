"""
import-history.py — rebuild a history.db from collected NDJSON.

The other half of export-history.py. Turns the append-only file the scheduled
collector commits back into a real SQLite database, so `--report` and the trend
comparison read it with no special case — the analysis path does not need to
know the data arrived by a different road.

Idempotent: rows already present are skipped, so importing the same file twice
does not double every measurement and quietly halve every percentile.

Usage:
    python scripts/import-history.py telemetry/probes.ndjson --db collected.db
    python main.py --report          # then point HISTORY_DB_FILE at it
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from history import SCHEMA  # noqa: E402

COLUMNS = (
    "run_started_at", "url", "checked_at", "status", "error", "http_status",
    "rtt_ms", "dns_ms", "connect_total_ms", "tls_ms", "ttfb_ms",
    "server_processing_ms", "transfer_ms", "body_bytes", "goodput_bps",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ndjson", type=Path)
    parser.add_argument("--db", default=Path("collected.db"), type=Path)
    args = parser.parse_args()

    if not args.ndjson.exists():
        print(f"No existe {args.ndjson}", file=sys.stderr)
        return 1

    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)

    # The de-duplication key. (url, checked_at) is unique in practice because a
    # target is probed once per run and a run stamps each result with its own
    # completion time -- the same reason checked_at exists separately from
    # run_started_at.
    seen = {
        (url, checked_at)
        for url, checked_at in con.execute(
            "SELECT url, checked_at FROM probe_results")
    }

    inserted = skipped = malformed = 0
    sources: set[str] = set()
    with con:
        for line in args.ndjson.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # One truncated line -- a push interrupted mid-write -- must not
                # cost the other 2,700. Counted and reported, never silent.
                malformed += 1
                continue
            sources.add(row.get("source") or "(sin etiqueta)")
            key = (row.get("url"), row.get("checked_at"))
            if key in seen:
                skipped += 1
                continue
            con.execute(
                f"INSERT INTO probe_results ({', '.join(COLUMNS)}) "  # noqa: S608
                f"VALUES ({', '.join('?' * len(COLUMNS))})",
                tuple(row.get(c) for c in COLUMNS),
            )
            seen.add(key)
            inserted += 1
    con.close()

    print(f"  {inserted} filas nuevas, {skipped} ya presentes, "
          f"{malformed} lineas ilegibles -> {args.db}")

    if len(sources) > 1:
        # Loud, and a non-zero exit. Two vantage points in one file average two
        # different network paths into percentiles that describe neither, and
        # nothing about the resulting number looks wrong. Refusing is the only
        # way this stays a mistake somebody notices.
        print(
            "\n  *** MEZCLA DE FUENTES: " + ", ".join(sorted(sources)) + "\n"
            "  Un percentil sobre dos puntos de observacion distintos no\n"
            "  describe ninguno de los dos. Filtra el archivo por 'source'\n"
            "  antes de analizarlo, o importa cada fuente a su propia base.",
            file=sys.stderr,
        )
        return 2

    if sources:
        print(f"  fuente: {sources.pop()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
