"""
mcp_server.py — a read-only MCP server over stdio (v0).

Lets an MCP client — an assistant, an IDE — ask the monitor what it knows:
the current status of every target, whether anyone would be told about an
outage (alert channels, heartbeat), uptime and TTFB percentiles over 1, 7 or
30 days, and the p95 trend. It answers questions; it changes nothing.

Why it is shaped this way
-------------------------
* **No new dependency.** MCP over stdio is newline-delimited JSON-RPC 2.0, and
  this server speaks four methods of it. The project dropped tenacity for a
  hand-rolled retry for the same reason: a package pulled in for a small,
  well-specified job is surface area that has to be audited and pinned forever.
* **No second query path.** Every number comes from the functions the
  dashboard API and `--report` already use (`api.read_state`,
  `api.stale_seconds`, `api.history_payload`, `latency_trend_windows` +
  `latency_trend`). A server that re-derived them would sooner or later answer
  differently from the dashboard for the same rows, and both would pass their
  own tests.
* **Read-only by construction.** The history functions open SQLite with
  `mode=ro`, which makes a write impossible rather than merely absent, and a
  missing database is an error, not an empty file created on the spot. No
  tool touches state.json except to read it.
* **Secrets are counted, never returned.** Whether a channel is configured is
  decided as the monitor decides it — the process environment first, then
  `.env` beside this file, an empty value meaning unset — and only the
  channel's name and a boolean leave the process. `.env` is read for that and
  never loaded into the environment.
* **stdout is the protocol.** One JSON message per line, nothing else. Logs go
  to stderr; a single stray print on stdout corrupts the stream for the client.
  `main` is not imported for the same reason — importing it loads `.env` and
  configures logging.

Run it where the data is:

    docker compose exec -T dashboard python mcp_server.py
    STATE_FILE_PATH=data/state.json HISTORY_DB_PATH=data/history/history.db \
        python mcp_server.py
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import IO, Any, Callable

import dotenv.main

import api
from diagnostics import latency_trend
from history import history_path_from_env, latency_trend_windows
from notifiers import ALL_NOTIFIERS

logger = logging.getLogger("sre.mcp")

SERVER_NAME   : str = "vigil-sre"
SERVER_VERSION: str = "0.4.0"     # 0.x until the owner calls it finished; 1.0.0 then

#: Newest first. An unknown version from the client is answered with the first.
PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")

#: Must equal main.TREND_WINDOW_DAYS (a test holds them together). Duplicated
#: rather than imported: importing main has side effects this process cannot have.
TREND_WINDOW_DAYS: int = 7

PARSE_ERROR      : int = -32700
INVALID_REQUEST  : int = -32600
METHOD_NOT_FOUND : int = -32601
INVALID_PARAMS   : int = -32602
INTERNAL_ERROR   : int = -32603

TOOLS: list[dict] = [
    {
        "name": "get_status",
        "description": (
            "Current status of every monitored target (UP, DEGRADED or DOWN), "
            "with the last check time, the last error and the phase diagnostics, "
            "plus stale_seconds: the age of the oldest check. A large "
            "stale_seconds means the monitor stopped, and the statuses are old. "
            "alerting names the configured alert channels and says whether a "
            "heartbeat watches the monitor; warnings repeats what the monitor "
            "logs every round when either is missing — all targets UP with no "
            "channel means nobody would hear about the next outage. A "
            "state_unreadable warning means targets is empty because the "
            "monitor's state file could not be used, not because nothing is "
            "monitored. This includes invalid UTF-8 as well as broken JSON."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_history",
        "description": (
            "Uptime percentage and p50/p95 time to first byte per target over a "
            "window of 1d, 7d or 30d (default 7d), from the stored probe history. "
            "The same numbers the dashboard API and --report give. warnings "
            "holds history_uninitialised when probe_results is absent, or "
            "history_unreadable with the reason when the database cannot be read, "
            "including when there are no targets. Empty numbers are not a quiet window."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"window": {"type": "string", "enum": sorted(api.WINDOWS),
                                      "default": api.DEFAULT_WINDOW}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_trend",
        "description": (
            f"p95 TTFB of the last {TREND_WINDOW_DAYS} days against the "
            f"{TREND_WINDOW_DAYS} before, per target: ok, degrading, improving, or "
            "why it could not be compared (too few samples, or windows whose "
            "response rates differ). The same verdict --report prints. "
            "warnings as in get_history: history_uninitialised or history_unreadable "
            "with the read failure reason, even with no targets."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


class _ToolError(Exception):
    """A tool that ran and could not answer: reported as a result with
    isError, not as a protocol error, so the client can show it to the model."""


class Server:
    """One instance per session. Holds only the two paths it reads."""

    def __init__(self, state_path: Path, history_path: Path,
                 env_file: Path | None = None) -> None:
        self.state_path   = Path(state_path)
        self.history_path = Path(history_path)
        # Where main.py's load_dotenv() finds it when run from the repository.
        # In a container the variables arrive through env_file instead.
        self.env_file     = Path(env_file) if env_file else Path(__file__).resolve().parent / ".env"
        self._methods: dict[str, Callable[[dict], dict]] = {
            "initialize": self._initialize,
            "ping"      : lambda params: {},
            "tools/list": lambda params: {"tools": TOOLS},
            "tools/call": self._tools_call,
        }

    # ------------------------------------------------------------------ protocol

    def handle_line(self, line: str) -> str | None:
        """One line in, at most one line out. None for a notification."""
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            return _dump(_error(None, PARSE_ERROR, f"parse error: {exc.msg}"))
        reply = self.handle(message)
        return None if reply is None else _dump(reply)

    def handle(self, message: Any) -> dict | None:
        # A batch (a JSON array) is not part of the protocol since 2025-06-18.
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            return _error(message.get("id") if isinstance(message, dict) else None,
                          INVALID_REQUEST, "not a JSON-RPC request")
        # Checked before anything is defaulted: `params or {}` used to turn
        # params=[] and params=false into a valid call, and a message with no
        # "jsonrpc" or with "1.0" was answered as if it were 2.0.
        if message.get("jsonrpc") != "2.0":
            id_ = message.get("id")
            return _error(id_ if _valid_id(id_) else None, INVALID_REQUEST,
                          'not a JSON-RPC 2.0 request ("jsonrpc" must be "2.0")')
        is_notification = "id" not in message
        if is_notification:
            return None
        id_ = message["id"]
        if not _valid_id(id_):
            # MCP: "the ID MUST NOT be null"; JSON-RPC: a string or a number.
            return _error(None, INVALID_REQUEST, "id must be a string or an integer")
        method = self._methods.get(message["method"])
        if method is None:
            return _error(id_, METHOD_NOT_FOUND, f"method not found: {message['method']}")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _error(id_, INVALID_PARAMS, "params must be an object")
        try:
            return {"jsonrpc": "2.0", "id": id_, "result": method(params)}
        except _InvalidParams as exc:
            return _error(id_, INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 — the loop must outlive any one request
            logger.exception("Request %r failed", message["method"])
            return _error(id_, INTERNAL_ERROR, f"internal error: {type(exc).__name__}")

    def _initialize(self, params: dict) -> dict:
        asked = params.get("protocolVersion")
        return {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities"   : {"tools": {"listChanged": False}},
            "serverInfo"     : {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions"   : (
                "Read-only view of an HTTP health monitor. Start with get_status; "
                "check stale_seconds before trusting a status."
            ),
        }

    def _tools_call(self, params: dict) -> dict:
        name      = params.get("name")
        arguments = params.get("arguments", {})
        tool = {"get_status": self._get_status, "get_history": self._get_history,
                "get_trend": self._get_trend}.get(name)
        if tool is None:
            raise _InvalidParams(f"unknown tool: {name!r}")
        if not isinstance(arguments, dict):
            raise _InvalidParams("arguments must be an object")
        try:
            payload = tool(arguments)
        except _ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except sqlite3.Error as exc:
            # The API answers this with a 500 and a plain message; the same
            # boundary, in this protocol's words.
            logger.error("History query failed for %s: %s", name, exc)
            return {"content": [{"type": "text", "text":
                                 f"could not read monitoring data ({type(exc).__name__}: {exc})"}],
                    "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "structuredContent": payload, "isError": False}

    # --------------------------------------------------------------------- tools

    def _targets(self) -> dict:
        return api.read_state(self.state_path)

    def _history_warnings(self) -> list[dict]:
        return api.history_warnings(self.history_path)

    def _require_history(self) -> None:
        # Checked here, not left to sqlite: with mode=ro a missing file is an
        # "unable to open" error, which is right, but the message should say
        # what is missing rather than what sqlite could not do.
        if not self.history_path.exists():
            raise _ToolError(f"no history database at {self.history_path}")

    def _is_set(self, var: str) -> bool:
        """The monitor's rule: the process environment wins, even when empty
        (load_dotenv does not override), then .env; an empty value is unset."""
        if var in os.environ:
            return bool(os.environ[var])
        return bool(self._dotenv_as_the_monitor_reads_it().get(var))

    def _dotenv_as_the_monitor_reads_it(self) -> dict:
        """.env resolved by the object load_dotenv(override=False) builds.

        dotenv_values() interpolates ${REF} with the FILE's value first; the
        monitor's load_dotenv(override=False) interpolates with the PROCESS
        value first. With REF empty in the process and set in .env, the first
        said "heartbeat configured" about a monitor that pings nobody. Same
        class, same flags, so the same answer -- and nothing is written into
        this process's environment."""
        if not self.env_file.is_file():
            return {}
        disabled = getattr(dotenv.main, "_load_dotenv_disabled", lambda: False)
        if disabled():
            return {}
        return dotenv.main.DotEnv(self.env_file, interpolate=True, override=False).dict()

    def _alerting(self) -> tuple[dict, list[dict]]:
        channels  = [cls.name for cls in ALL_NOTIFIERS if self._is_set(cls.env_var)]
        heartbeat = self._is_set("HEARTBEAT_URL")
        warnings: list[dict] = []
        # The words before " — " are the monitor's own log wording for the
        # same condition; a test holds the two together.
        if not channels:
            warnings.append({"code": "no_alert_channels", "message": (
                "No alert channels configured (set "
                + " or ".join(cls.env_var for cls in ALL_NOTIFIERS)
                + ") — any alert the monitor raises reaches nobody.")})
        if not heartbeat:
            warnings.append({"code": "heartbeat_unconfigured", "message": (
                "HEARTBEAT_URL is not set — nothing is watching this monitor; "
                "if it dies, no alert says so.")})
        return {"channels": channels, "heartbeat": heartbeat}, warnings

    def _get_status(self, arguments: dict) -> dict:
        targets, state_error = api.read_state_checked(self.state_path)
        alerting, warnings = self._alerting()
        if state_error:
            # First: it explains the empty targets above every other warning.
            warnings.insert(0, {"code": "state_unreadable", "message": (
                f"{state_error} — targets is empty because the monitor's state "
                "could not be used, not because nothing is monitored.")})
        return {"targets": targets, "stale_seconds": api.stale_seconds(targets),
                "alerting": alerting, "warnings": warnings}

    def _get_history(self, arguments: dict) -> dict:
        window = arguments.get("window", api.DEFAULT_WINDOW)
        if window not in api.WINDOWS:
            raise _ToolError(f"unknown window {window!r}; accepted: {', '.join(sorted(api.WINDOWS))}")
        self._require_history()
        payload = api.history_payload(self.history_path, window, list(self._targets()))
        return {**payload, "warnings": self._history_warnings()}

    def _get_trend(self, arguments: dict) -> dict:
        self._require_history()
        verdicts: dict[str, dict] = {}
        for url in self._targets():
            # The same two calls, with the same arguments, as main._trend_lines.
            recent, previous = latency_trend_windows(self.history_path, url, TREND_WINDOW_DAYS)
            verdicts[url] = latency_trend(
                recent["p95_ttfb_ms"]   if recent   else None,
                previous["p95_ttfb_ms"] if previous else None,
                recent["samples"]       if recent   else 0,
                previous["samples"]     if previous else 0,
                recent["total"]         if recent   else 0,
                previous["total"]       if previous else 0,
            )
        return {"window_days": TREND_WINDOW_DAYS, "targets": verdicts,
                "warnings": self._history_warnings()}


class _InvalidParams(Exception):
    pass


def _valid_id(id_: Any) -> bool:
    # bool is an int subclass in Python and never a request id.
    return isinstance(id_, str) or (isinstance(id_, int) and not isinstance(id_, bool))


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _dump(message: dict) -> str:
    # No embedded newlines: the line IS the message boundary.
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def run(stdin: IO[str], stdout: IO[str], server: Server) -> None:
    """Read requests until EOF. Each answer is flushed as it is written: a
    client waiting on a buffered reply waits forever."""
    for line in stdin:
        if not line.strip():
            continue
        reply = server.handle_line(line)
        if reply is not None:
            stdout.write(reply + "\n")
            stdout.flush()


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO,
        format="[%(asctime)s] [%(levelname)-8s] [%(name)s] %(message)s",
    )
    server = Server(
        Path(os.getenv("STATE_FILE_PATH", str(api.STATE_FILE))),
        history_path_from_env(),
    )
    logger.info("MCP server %s ready: state=%s history=%s",
                SERVER_VERSION, server.state_path, server.history_path)
    run(sys.stdin, sys.stdout, server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
