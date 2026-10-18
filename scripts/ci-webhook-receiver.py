"""
ci-webhook-receiver.py -- a webhook sink for the CI end-to-end cycle.

Why this exists
---------------
The CI job runs one real health-check cycle. A first cycle fires alerts, and an
alert that reaches no channel is a monitor failure: main.py exits 1. With the
placeholder URL from .env.example Discord answers 400, nothing is delivered, and
the job goes red for the right reason and the wrong cause. With an EMPTY URL it
exits 1 as well, by design, and that rule is not to be weakened to make a job
pass.

So the job points DISCORD_WEBHOOK_URL at this process instead. It answers 204,
which is what Discord answers on success, and writes one line per POST to a
log file. The job then asserts the monitor exited 0 AND the log is not empty:
delivery proved end to end, not merely "nothing crashed".

A GET answers 200 and is not logged. The job polls it to learn the sink is up,
and a readiness probe that counted as a delivery would let a monitor that
delivered nothing pass.

Standard library only: it runs on the bare runner, before any pip install.

Usage:
    python3 scripts/ci-webhook-receiver.py --port 8089 --log /tmp/hits.ndjson
    python3 scripts/ci-webhook-receiver.py --port 0   # any free port; printed

It binds 0.0.0.0 by default because the caller is a container, and a container
reaches the host through an interface address, never through the host's
127.0.0.1. That is acceptable on a runner that lives for one job; do not run it
on a machine that stays.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY_BYTES = 1024 * 1024  # an alert is a few hundred bytes; refuse to buffer more


def make_handler(log: Path, lock: threading.Lock) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 -- http.server's naming
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            body = self.rfile.read(min(max(length, 0), MAX_BODY_BYTES))
            # json.dumps escapes newlines, so one POST is always one line: the
            # job counts hits with `wc -l`.
            record = json.dumps(
                {"path": self.path, "body": body.decode("utf-8", "replace")},
                ensure_ascii=False,
            )
            with lock, log.open("a", encoding="utf-8") as handle:
                handle.write(record + "\n")
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            # The default writes every request to stderr. The log file is the
            # record; a second copy in the job output only buries it.
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 -- see module docstring
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True,
                        help="NDJSON file; one line is appended per POST.")
    args = parser.parse_args()

    # Created up front so "no POST arrived" is an empty file, not a missing one:
    # the job's `wc -l` then reports 0 instead of failing on the open.
    args.log.touch()

    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(args.log, threading.Lock()))
    # flush: the caller reads this line through a pipe to learn the port.
    print(f"listening on {args.host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
