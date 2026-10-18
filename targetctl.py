"""
targetctl.py — change the target list from a terminal, through the API.

Why this is a client of api.py and not a second editor
-------------------------------------------------------
api.py is the only writer of data/targets.json. A CLI that opened the file
itself would be a second writer with no coordination: its read-modify-write and
the dashboard's PUT interleave, and whichever replaces the file last discards
the other's change without a trace. So every write here is GET /api/targets,
change the list in memory, PUT the whole list back -- the same two calls the
browser editor makes, validated by the same code (targetstore) on the way in.

Editing targets.yaml is the other terminal path, and it fails silently: once
the store exists the monitor never reads the YAML again, so the edit is
accepted by the editor, committed to git, and ignored by the process.

Nothing the CLI does not touch is rewritten
--------------------------------------------
The list is PUT back whole, so every entry travels through this process. Each
one is passed on exactly as GET returned it -- a dict, never a rebuilt object
-- and only the entry being added or removed differs. An editor that redrew
only the fields it knew about deleted `maintenance` windows and `remind`
opt-ins it did not render, and the operator found out when a silenced target
paged at 3 AM. The CLI knows nothing about the fields beyond how to type a
`--set` value, so it cannot drop one.

The lost-update window, documented and not closed
--------------------------------------------------
The API offers no version, ETag or If-Match. Between the GET and the PUT
another client (the dashboard in a browser, a second terminal) can save, and
this PUT then replaces that save with a list built from the older read. The
window is two sequential local requests -- milliseconds, not the minutes a
human spends in a form -- and the CLI keeps it that way: validation of the
arguments and the token check happen BEFORE the GET, and nothing interactive
sits between the GET and the PUT. It cannot be closed from this side; closing
it needs the API to return a version with GET and refuse a PUT carrying a
stale one (412). That is a server change and is left as a follow-up.

The first write moves the list from the YAML to the store
----------------------------------------------------------
While data/targets.json does not exist, GET answers with the targets.yaml seed
and `managed: false`. The first PUT creates the store, and from then on
main.py reads the store and never the YAML (main.load_targets). The CLI
proceeds, says so, and refuses only when the seed could not be read at all
(`seed_error`): GET then returns an empty list that means "cannot show", and
PUTting that list plus one new URL would silently replace every real target
with one.

Token and address
-----------------
The write token is API_WRITE_TOKEN, from the environment or a `.env` in the
current directory. It is sent as X-Vigil-Token on PUT only, is never printed,
and a write without it exits before any request is made. The API address is
--api, else VIGIL_API, else http://127.0.0.1:8787; inside the dashboard
container that is the API itself (api.py binds 0.0.0.0 there), so
`docker compose exec dashboard python targetctl.py list` works unchanged.

Python  : 3.11+
Depends : stdlib, plus python-dotenv and PyYAML (already runtime dependencies).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

import targetstore

DEFAULT_API: str = "http://127.0.0.1:8787"
API_VAR: str = "VIGIL_API"
TOKEN_VAR: str = "API_WRITE_TOKEN"

#: The API answers a local file read in well under a second; waiting longer
#: than this means something other than the API is on the port.
REQUEST_TIMEOUT_S: float = 10.0

#: 200 targets are a few tens of KB. A body past this is not the API.
MAX_RESPONSE_BYTES: int = 4 * 1024 * 1024

_BOOL_KEYS: frozenset[str] = frozenset({"remind", "trend_alerts"})
_NUMBER_KEYS: frozenset[str] = frozenset(targetstore.NUMERIC_BOUNDS)

#: What `--set` may name. `url` is positional, so it is not a setting.
SETTABLE_KEYS: frozenset[str] = targetstore.ALLOWED_KEYS - {"url"}


class CliError(Exception):
    """An expected failure: one line on stderr, exit 1, no traceback."""


# ---------------------------------------------------------------------------
# --set parsing
# ---------------------------------------------------------------------------

def parse_settings(pairs: list[str]) -> dict:
    """
    Turn ``key=value`` words into entry fields.

    Only the SHAPE is judged here: a `=`, a known key, no key twice. Whether
    the value is acceptable is the API's question, answered by the code that
    will actually store it, so a value this function cannot type (``abc`` for
    a number) is passed through as text and comes back as the API's own 400.
    Judging it here as well would be a second copy of the bounds -- the drift
    targetstore exists to prevent.
    """
    settings: dict = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise CliError(f"bad --set {pair!r}: expected key=value.")
        if key not in SETTABLE_KEYS:
            raise CliError(
                f"bad --set: unknown field {key!r}. "
                f"Allowed: {', '.join(sorted(SETTABLE_KEYS))}."
            )
        if key in settings:
            raise CliError(f"bad --set: {key!r} given twice.")
        settings[key] = _typed(key, raw)
    return settings


def _typed(key: str, raw: str) -> object:
    """Type *raw* when the type is unambiguous, otherwise leave it as text."""
    if key in _BOOL_KEYS and raw.strip().lower() in ("true", "false"):
        return raw.strip().lower() == "true"
    if key in _NUMBER_KEYS:
        text = raw.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            return raw
        # NaN and infinity are not JSON; the API's answer for them is "must
        # be between ...", which needs them to arrive as something it can read.
        return number if math.isfinite(number) else raw
    if key == "maintenance":
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _opener(api: str) -> urllib.request.OpenerDirector:
    """
    An opener that skips the system proxy for a loopback API.

    urllib reads HTTP_PROXY and, on Windows, the registry's proxy settings. A
    corporate proxy configured there would receive the write token addressed to
    127.0.0.1 and could not forward it anywhere useful. A remote API keeps the
    default behaviour: there a proxy may be the only way to reach it.
    """
    host = urlparse(api).hostname or ""
    if _is_loopback(host):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _api_base(value: str) -> str:
    base = value.strip().rstrip("/")
    parsed = urlparse(base)
    # urlopen also speaks file:// and ftp://; a typo'd --api must not turn
    # into a local file read or carry the token to a protocol it was not for.
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CliError(
            f"bad API address {value!r}: expected http://host:port or "
            f"https://host:port."
        )
    return base


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _call(api: str, method: str, body: dict | None = None,
          token: str | None = None) -> dict:
    """
    One request to /api/targets. Returns the parsed JSON of a 2xx answer;
    everything else is a CliError carrying the API's own message.
    """
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["X-Vigil-Token"] = token
    request = urllib.request.Request(
        f"{api}/api/targets", data=data, method=method, headers=headers,
    )

    try:
        with _opener(api).open(request, timeout=REQUEST_TIMEOUT_S) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise CliError(_http_failure(exc.code, exc.read(65536))) from None
    except urllib.error.URLError as exc:
        raise CliError(
            f"cannot reach the API at {api} ({_one_line(exc.reason)}). "
            f"Is the dashboard running? Point at it with --api or {API_VAR}."
        ) from None
    except OSError as exc:
        # Timeout mid-read, connection reset: also "the API did not answer".
        raise CliError(
            f"the API at {api} did not answer ({_one_line(exc)})."
        ) from None
    except ValueError as exc:
        # http.client refuses a header value with a control character.
        raise CliError(f"the request could not be built ({_one_line(exc)}).") from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise CliError(f"the answer from {api} is too large to be the API.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CliError(
            f"the answer from {api}/api/targets is not JSON; "
            f"is that the vigil-sre API?"
        ) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("targets"), list):
        raise CliError(
            f"the answer from {api}/api/targets has no target list; "
            f"is that the vigil-sre API?"
        )
    return payload


def _http_failure(status: int, raw: bytes) -> str:
    """The API's message for a non-2xx answer, verbatim and on one line."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
        message = parsed.get("error") if isinstance(parsed, dict) else None
        store_error = parsed.get("store_error") if isinstance(parsed, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        message = store_error = None
    message = _one_line(message) if message else "no message"

    if store_error:
        # The API's store is present and unusable. Its answer carries no list,
        # and a write on top of it is refused, so the one honest outcome is to
        # say so and stop: the fix is on the file, not in this command.
        return (
            f"the API cannot use its target store ({_one_line(store_error)}). "
            f"Nothing was written. Fix the file, or delete it to return to the "
            f"targets.yaml seed, then retry."
        )

    if status == 401:
        # The hint in the API's own answer says to send a header the CLI
        # already sends, so it would mislead here. The two real causes are
        # these.
        return (
            f"the API refused the write (401: {message}). {TOKEN_VAR} here does "
            f"not match the token the API process holds, or the API has none "
            f"configured."
        )
    if status == 400:
        return f"the API rejected the list (400): {message}"
    return f"the API answered {status}: {message}"


# ---------------------------------------------------------------------------
# Read-modify-write
# ---------------------------------------------------------------------------

def _token() -> str:
    """The write token, or a CliError. Called before any request is made."""
    token = os.getenv(TOKEN_VAR) or ""
    if not token:
        raise CliError(
            f"{TOKEN_VAR} is not set (environment or .env); a write needs it. "
            f"Nothing was sent."
        )
    if not token.isascii() or any(ord(ch) < 32 or ord(ch) == 127 for ch in token):
        # Said without echoing the value: the whole point is that it is secret.
        raise CliError(
            f"{TOKEN_VAR} contains a control or non-ASCII character and cannot "
            f"be sent as a header. Nothing was sent."
        )
    return token


def _snapshot(api: str) -> dict:
    return _call(api, "GET")


def _guard_seed(snapshot: dict, api: str) -> None:
    if not snapshot.get("managed") and snapshot.get("seed_error"):
        raise CliError(
            f"the list cannot be read from the seed ({_one_line(snapshot['seed_error'])}). "
            f"Writing now would replace the real list with only your change. "
            f"Fix the seed or save the list once from the dashboard at {api}. "
            f"Nothing was written."
        )


def _put(api: str, token: str, targets: list, allow_empty: bool) -> list:
    body: dict = {"targets": targets}
    if allow_empty:
        body["allow_empty"] = True
    return _call(api, "PUT", body, token)["targets"]


def _seed_notice(snapshot: dict) -> str | None:
    if snapshot.get("managed"):
        return None
    return (
        "Note: the list was the targets.yaml seed. The dashboard store "
        "(data/targets.json) now owns it, and further edits to targets.yaml "
        "will not be read."
    )


def _url_of(entry: object) -> str | None:
    return entry.get("url") if isinstance(entry, dict) else None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _describe(entry: dict) -> str:
    """URL plus the settings that differ from what an absent field means."""
    parts = []
    for key in sorted(entry):
        if key == "url":
            continue
        value = entry[key]
        if key == "remind" and value is False:
            continue
        if key == "trend_alerts" and value is True:
            continue
        if isinstance(value, bool):
            shown = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            shown = json.dumps(value, separators=(",", ":"))
        else:
            shown = str(value)
        parts.append(f"{key}={shown}")
    return entry["url"] + (f"  {' '.join(parts)}" if parts else "")


def cmd_list(args: argparse.Namespace, api: str) -> int:
    snapshot = _snapshot(api)
    targets = snapshot["targets"]
    if snapshot.get("managed"):
        print(f"Source: dashboard store (data/targets.json), managed. API: {api}")
    else:
        print(
            f"Source: targets.yaml seed, not yet managed -- the first add or "
            f"remove moves the list to the dashboard store. API: {api}"
        )
    if snapshot.get("seed_error") and not snapshot.get("managed"):
        raise CliError(
            f"the seed cannot be shown: {_one_line(snapshot['seed_error'])}"
        )
    print(f"{len(targets)} target(s)")
    for entry in targets:
        if _url_of(entry) is None:
            raise CliError("the API returned an entry without a url.")
        print(f"  {_describe(entry)}")
    return 0


def cmd_add(args: argparse.Namespace, api: str) -> int:
    settings = parse_settings(args.set or [])
    url = args.url.strip()
    token = _token()

    snapshot = _snapshot(api)
    _guard_seed(snapshot, api)
    current = snapshot["targets"]
    if any(_url_of(entry) == url for entry in current):
        raise CliError(f"{url} is already a target. Nothing was written.")

    stored = _put(api, token, current + [{"url": url, **settings}], False)
    print(f"Added {url}. {len(stored)} target(s) now.")
    notice = _seed_notice(snapshot)
    if notice:
        print(notice)
    return 0


def cmd_remove(args: argparse.Namespace, api: str) -> int:
    url = args.url.strip()
    token = _token()

    snapshot = _snapshot(api)
    _guard_seed(snapshot, api)
    current = snapshot["targets"]
    kept = [entry for entry in current if _url_of(entry) != url]
    if len(kept) == len(current):
        raise CliError(f"{url} is not a target. Nothing was written.")
    if not kept and not args.allow_empty:
        raise CliError(
            f"{url} is the last target; removing it leaves the monitor with "
            f"nothing to probe. Pass --allow-empty if that is the intent. "
            f"Nothing was written."
        )

    stored = _put(api, token, kept, allow_empty=not kept)
    print(f"Removed {url}. {len(stored)} target(s) now.")
    notice = _seed_notice(snapshot)
    if notice:
        print(notice)
    return 0


def load_target_file(path: Path) -> list:
    """
    The raw entries of a target file, YAML seed or JSON store alike.

    A bare string is a URL, as in targets.yaml. Nothing is dropped here:
    main.load_targets ignores a key it does not know, and the store refuses
    it, so a validator that dropped it too would pass a file the API rejects.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError(f"{path}: cannot read ({_one_line(exc)}).") from None
    is_json = path.suffix.lower() == ".json"
    try:
        raw = json.loads(text) if is_json else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        kind = "JSON" if is_json else "YAML"
        raise CliError(f"{path}: not valid {kind} ({_one_line(exc)}).") from None

    entries = raw.get("targets") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise CliError(f"{path}: expected a 'targets' list.")
    if not entries and not is_json:
        # The monitor exits 1 on a seed with nothing to probe.
        raise CliError(f"{path}: the 'targets' list is empty.")
    return [{"url": e} if isinstance(e, str) else e for e in entries]


def cmd_validate(args: argparse.Namespace, api: str | None) -> int:
    path = Path(args.file) if args.file else targetstore.SEED_FILE
    entries = load_target_file(path)
    try:
        targetstore.validate_all(entries, check_env=args.check_env)
    except targetstore.ValidationError as exc:
        raise CliError(f"{path}: {_one_line(exc)}") from None
    print(f"OK: {path} has {len(entries)} valid target(s).")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="targetctl.py",
        description=(
            "Manage the vigil-sre target list through the dashboard API. "
            "Writes need API_WRITE_TOKEN (environment or .env)."
        ),
    )
    api_help = f"API address (default: ${API_VAR}, else {DEFAULT_API})"
    parser.add_argument("--api", metavar="URL", help=api_help)
    # The same flag after the subcommand, because that is where people type it.
    # SUPPRESS keeps the subparser from overwriting a value given before it.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--api", metavar="URL", default=argparse.SUPPRESS,
                        help=api_help)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    sub.add_parser("list", parents=[shared],
                   help="show the live list and where it comes from")

    add = sub.add_parser("add", parents=[shared], help="add a target")
    add.add_argument("url", help="http:// or https:// URL to monitor")
    add.add_argument(
        "--set", nargs="+", action="extend", metavar="key=value",
        help="per-target setting, e.g. --set timeout_s=5 remind=true "
             f"(fields: {', '.join(sorted(SETTABLE_KEYS))})",
    )

    remove = sub.add_parser("remove", parents=[shared], help="remove a target")
    remove.add_argument("url", help="the exact URL as listed")
    remove.add_argument(
        "--allow-empty", action="store_true",
        help="allow removing the last target (the monitor is then left "
             "with nothing to probe)",
    )

    validate = sub.add_parser(
        "validate", help="check a YAML or JSON target file locally, no network",
    )
    validate.add_argument(
        "file", nargs="?",
        help=f"file to check (default: {targetstore.SEED_FILE})",
    )
    validate.add_argument(
        "--check-env", action="store_true",
        help="also require every ${VAR} reference to be set in this environment",
    )
    return parser


_COMMANDS = {
    "list": cmd_list, "add": cmd_add, "remove": cmd_remove,
    "validate": cmd_validate,
}


def main(argv: list[str] | None = None) -> int:
    # Relative to the cwd on purpose: find_dotenv() would walk up from this
    # file and could pick a .env that belongs to another checkout.
    load_dotenv(".env")
    args = build_parser().parse_args(argv)
    try:
        api = None
        if args.command != "validate":
            api = _api_base(args.api or os.getenv(API_VAR) or DEFAULT_API)
        return _COMMANDS[args.command](args, api)
    except CliError as exc:
        print(f"targetctl: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
