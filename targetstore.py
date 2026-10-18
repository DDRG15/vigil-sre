"""
targetstore.py — the target list, when the dashboard owns it instead of a file.

Why a separate module, and JSON instead of the YAML
----------------------------------------------------
Two processes touch this list and they must not import each other. main.py
probes and reads it; api.py serves the dashboard and writes it. Putting the
store in main.py would drag the whole probe stack — aiohttp, the diagnostics
engine — into the dashboard process, undoing the isolation that made a
separate process worth having. This module depends on stdlib only, so both
sides can hold it without holding each other.

JSON and not the existing targets.yaml, for two reasons that are facts rather
than taste:

  1. **targets.yaml is 62 comment lines out of 81.** It is mostly
     documentation. A machine that rewrites it destroys that on the first
     save, and no YAML library round-trips comments faithfully.
  2. **targets.yaml is bind-mounted as a single FILE.** The atomic
     tmp-then-rename this store uses cannot work against one: the OS refuses
     to rename() onto an active mount point, which this project already
     learned the hard way with state.json. `data/` is a directory mount and
     already writable, so the store lives there.

targets.yaml therefore stays what it is: the hand-edited seed, and the
documentation of every field. The moment the dashboard writes for the first
time, this store becomes the single runtime source and the dashboard says so —
two files that both look authoritative is worse than either one alone.

Validation is the security boundary
-------------------------------------
Every entry here arrives over HTTP and becomes a URL this monitor will fetch
from its own network, on a schedule, forever. That is the interesting attack,
not vandalism of the list: an attacker who can add a target gets a probe
inside your perimeter with the results rendered on a page. Scheme is
restricted to http/https so the fetch cannot be pointed at file:// or any
other protocol handler, every numeric field is range-checked, and unknown keys
are rejected rather than ignored — a silently dropped field is a setting the
operator believes is in effect.

The seed has to be readable from this side too
----------------------------------------------
Until the first save the monitor runs on targets.yaml, and the editor has to
open on THAT list. An editor that opens on the store's answer for "no store"
-- an empty list -- turns "Save" into "delete every target": the save sends
what the editor drew, and the API replaces the whole list with it. read_seed()
is the dashboard's way of seeing the file the monitor is actually running on.

Python  : 3.11+
Depends : stdlib, plus PyYAML for read_seed (already a runtime dependency of
          main.py, so the dashboard image carries it).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yaml

logger = logging.getLogger("sre.targetstore")

def store_path_from_env() -> Path:
    """TARGETS_STORE_PATH, else data/targets.json. A function so an entry point
    that loads .env after this module was imported can resolve it again."""
    return Path(os.getenv("TARGETS_STORE_PATH", "data/targets.json"))


#: Where the dashboard-managed list lives. Inside `data/` because that is a
#: directory mount in docker-compose.yml, and an atomic rename needs one.
STORE_FILE: Path = store_path_from_env()

#: The hand-edited seed the monitor runs on until the first dashboard save.
#: Defined HERE and imported by main.py as TARGETS_FILE, for the reason
#: ENV_VAR_REF below is: the monitor and the editor must name the same file,
#: and two constants that both say "targets.yaml" are two chances to disagree.
#: Relative, so it resolves against each process's cwd -- /app in the image.
SEED_FILE: Path = Path("targets.yaml")


def _resolve(path: Path | None) -> Path:
    """
    The store path, read at CALL time and not at definition time.

    `def f(path=STORE_FILE)` binds the default once, when the module is
    imported, so reassigning STORE_FILE afterwards changes nothing and does
    so silently — the caller believes it redirected the store and did not.
    This project has been bitten by that exact shape before, in a test that
    patched `__init__.__defaults__` and quietly stopped working.
    """
    return STORE_FILE if path is None else path


def _resolve_seed(path: Path | None) -> Path:
    """The seed path, read at call time for the same reason _resolve reads the
    store's: a default bound at import cannot be redirected afterwards."""
    return SEED_FILE if path is None else path

#: Only these keys may appear in an entry. Anything else is rejected, never
#: ignored: a field that is silently dropped is a setting the operator
#: believes is in effect.
ALLOWED_KEYS: frozenset[str] = frozenset({
    "url", "expect_substring", "expected_status", "timeout_s",
    "degraded_ttfb_ms", "degraded_rtt_ms", "degraded_backend_ms",
    "remind", "trend_alerts", "maintenance",
})

#: (minimum, maximum) per numeric field. The same bounds load_targets already
#: enforces for the YAML path, restated here because this path never goes
#: through it — an API that accepted what the file rejects would be a way
#: around the validation, not a second door to it.
NUMERIC_BOUNDS: dict[str, tuple[float, float]] = {
    "expected_status" : (100, 599),
    "timeout_s"       : (0.1, 300),
    "degraded_ttfb_ms": (1, 600_000),
    "degraded_rtt_ms" : (1, 600_000),
    "degraded_backend_ms": (1, 600_000),
}

#: The ${VAR_NAME} reference syntax, defined HERE and imported by main.py
#: rather than declared in both. Two copies of a rule are how a validator and
#: the thing it validates drift apart, and the cost of that drift is the
#: finding below.
ENV_VAR_REF     = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
ENV_VAR_SUSPECT = re.compile(r"\$\{")


def env_reference_problem(raw: str) -> tuple[str, str] | None:
    """
    Return ``(code, var_name)`` if *raw* is an unusable env reference, else None.

    Codes: ``malformed`` (contains "${" but is not a complete reference),
    ``lowercase`` (Windows resolves case-insensitively and Linux does not, so
    the same file behaves differently in dev and prod), ``unset`` (the variable
    does not exist or is empty).

    Shared because main.py answers the same question and answers it FATALLY:
    an unresolvable reference there is sys.exit(1). Before this existed, the
    write path accepted a value the reader would refuse, so a typo saved from
    the dashboard returned 200 and then killed the whole monitor on its next
    run -- every target, not just the edited one, and with no alert about it,
    because the process that would send the alert is the one that died.
    """
    match = ENV_VAR_REF.match(raw)
    if match is None:
        return ("malformed", raw) if ENV_VAR_SUSPECT.search(raw) else None
    var_name = match.group(1)
    if var_name != var_name.upper():
        return ("lowercase", var_name)
    if not os.getenv(var_name):
        return ("unset", var_name)
    return None


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

MAX_TARGETS: int = 200
MAX_URL_LENGTH: int = 2048


MAX_WINDOWS: int = 20


def _validated_windows(raw: object) -> list[dict]:
    """
    Validate maintenance windows, rejecting rather than repairing.

    A window that silently failed to parse would leave the operator believing
    a target is silenced while it pages them at 3am — the exact promise this
    feature exists to keep, broken in the direction that costs sleep.
    """
    if not isinstance(raw, list):
        raise ValidationError("'maintenance' must be a list of windows.")
    if len(raw) > MAX_WINDOWS:
        raise ValidationError(f"At most {MAX_WINDOWS} windows per target.")

    out: list[dict] = []
    for window in raw:
        if not isinstance(window, dict):
            raise ValidationError("Each window must be an object.")
        if set(window) - {"days", "start", "end"}:
            raise ValidationError(
                "A window only accepts 'days', 'start' and 'end'.")

        clean: dict = {}
        for field in ("start", "end"):
            value = window.get(field)
            if not isinstance(value, str) or not _HHMM.match(value):
                raise ValidationError(
                    f"'{field}' must be in HH:MM 24h format (UTC).")
            clean[field] = value

        if clean["start"] == clean["end"]:
            # Otherwise it is either a zero-length window or a 24h one, and
            # which one it means is a coin flip. Make the operator say it.
            raise ValidationError(
                "'start' and 'end' cannot be equal — to silence the whole "
                "day use 00:00 to 23:59.")

        days = window.get("days")
        if days is not None:
            if (not isinstance(days, list) or not days
                    or any(not isinstance(d, int) or isinstance(d, bool)
                           or not 0 <= d <= 6 for d in days)):
                raise ValidationError(
                    "'days' must be a list of integers 0-6 (0 = Monday).")
            clean["days"] = sorted(set(days))

        out.append(clean)
    return out


class ValidationError(ValueError):
    """An entry the store refuses to hold, with a message meant for a human."""


def validate_entry(raw: object, *, check_env: bool = True) -> dict:
    """
    Return a normalised entry, or raise ValidationError explaining why not.

    Rejects rather than coerces. A target list that quietly repaired what it
    was given would leave the operator reading a dashboard that disagrees with
    what is actually being probed.

    ``check_env=False`` skips only the "is this ${VAR} set in THIS process"
    question, and exists for read_seed(): showing the list must not depend on
    which variables the dashboard process happens to hold. The syntax checks
    (complete reference, uppercase) still run, and saving still runs the full
    check, so an unset variable is refused at the PUT with its own message.
    """
    if not isinstance(raw, dict):
        raise ValidationError("Each target must be an object with at least 'url'.")

    unknown = set(raw) - ALLOWED_KEYS
    if unknown:
        raise ValidationError(
            f"Unrecognised field(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(ALLOWED_KEYS))}."
        )

    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValidationError("'url' is required and must be text.")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValidationError(f"The URL exceeds {MAX_URL_LENGTH} characters.")

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        # An unclosed "[" is read as an IPv6 literal and refused here.
        raise ValidationError(f"The URL cannot be parsed ({_one_line(exc)}).") from None
    if parsed.scheme not in ALLOWED_SCHEMES:
        # file://, gopher:// and friends would turn "monitor this endpoint"
        # into "read this path", using the monitor's own privileges.
        raise ValidationError(
            f"Scheme '{parsed.scheme or 'none'}' is not allowed. "
            f"Only {' and '.join(sorted(ALLOWED_SCHEMES))}."
        )
    if not parsed.netloc:
        raise ValidationError("The URL has no host.")

    # A non-empty netloc is not a host. ":8080" and "user@" have one and no
    # hostname, and the host and port are parsed lazily: asking for them is what
    # raises on "host:80a0" or ":99999". Accepting these at save time moved the
    # failure into the probe, where it raised on every round for as long as the
    # entry stayed in the list.
    try:
        hostname, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ValidationError(
            f"The URL has an invalid port ({_one_line(exc)}). Use a number "
            f"from 1 to 65535."
        ) from None
    if not hostname:
        raise ValidationError("The URL has no host.")
    if port == 0:
        # In range for urllib, but reserved: port 0 is never a destination, so
        # every probe of it fails and the target reads as DOWN forever.
        raise ValidationError("The URL port cannot be 0. Use 1 to 65535.")
    try:
        hostname.encode("idna")
    except UnicodeError:
        # The resolver encodes the host this way before it asks DNS, so a name
        # that cannot be encoded (an empty label as in "example..com", a label
        # over 63 characters) fails there. Checked with the same codec.
        raise ValidationError(
            f"The host '{hostname}' is not a valid domain name."
        ) from None

    entry: dict = {"url": url}

    substring = raw.get("expect_substring")
    if substring is not None:
        if not isinstance(substring, str):
            raise ValidationError("'expect_substring' must be text.")
        if substring:
            problem = env_reference_problem(substring)
            if problem and (check_env or problem[0] != "unset"):
                code, name = problem
                raise ValidationError({
                    "malformed": (
                        "'expect_substring' containing '${' must be a "
                        "COMPLETE reference to an environment variable, for "
                        "example '${HEALTH_TOKEN}'. Concatenation is not "
                        "supported."
                    ),
                    "lowercase": (
                        f"'expect_substring' references ${{{name}}}, but it "
                        f"must be UPPERCASE: ${{{name.upper()}}}. Windows "
                        f"resolves variables case-insensitively and Linux does "
                        f"not, so in lowercase the same target behaves "
                        f"differently on your machine and in production."
                    ),
                    "unset": (
                        f"'expect_substring' references ${{{name}}}, but "
                        f"that variable is not defined (or is empty). Saving "
                        f"it like this would stop the whole monitor on the "
                        f"next run, not just this target."
                    ),
                }[code])
            entry["expect_substring"] = substring

    for field, (low, high) in NUMERIC_BOUNDS.items():
        value = raw.get(field)
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"'{field}' must be a number.")
        if not (low <= value <= high):
            raise ValidationError(f"'{field}' must be between {low} and {high}.")
        entry[field] = int(value) if field == "expected_status" else float(value)

    windows = raw.get("maintenance")
    if windows:
        entry["maintenance"] = _validated_windows(windows)

    remind = raw.get("remind", False)
    if not isinstance(remind, bool):
        raise ValidationError("'remind' must be true or false.")
    entry["remind"] = remind

    # Default True, unlike 'remind': a new target's trend is assumed
    # interpretable until a measurement says otherwise. Opting OUT is the
    # decision that needs evidence, and it is recorded on the Target field.
    trend_alerts = raw.get("trend_alerts", True)
    if not isinstance(trend_alerts, bool):
        raise ValidationError("'trend_alerts' must be true or false.")
    entry["trend_alerts"] = trend_alerts

    return entry


def validate_all(entries: object, *, check_env: bool = True) -> list[dict]:
    """Validate a whole list, and reject duplicate URLs."""
    if not isinstance(entries, list):
        raise ValidationError("Expected a list of targets.")
    if len(entries) > MAX_TARGETS:
        raise ValidationError(f"At most {MAX_TARGETS} targets.")

    validated = [validate_entry(entry, check_env=check_env) for entry in entries]
    seen: set[str] = set()
    for entry in validated:
        if entry["url"] in seen:
            # Two rows for one URL would each write history under the same key
            # and each render a row, so the page would disagree with itself.
            raise ValidationError(f"Duplicate target: {entry['url']}")
        seen.add(entry["url"])
    return validated


class StoreError(Exception):
    """The store file is there and cannot be used; the message is one line."""


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def read_store(path: Path | None = None, *, check_env: bool = False) -> list[dict] | None:
    """
    Return the stored targets, or None when the dashboard has never written.

    Three answers that must never be collapsed into each other:

      None        no store file. The only case where the YAML seed is the list.
      []          a valid store holding no targets: the operator deliberately
                  removed every target. Falling back to the YAML here would
                  resurrect the list on the next run.
      StoreError  a file that exists and is not a usable list (bad JSON, an
                  entry the store refuses, unreadable, a directory in its
                  place). Answering None for this made the monitor probe the
                  stale seed: targets added from the dashboard stopped being
                  probed, deleted ones came back, the round exited 0 with a
                  green heartbeat and the pruner deleted the state of every
                  target missing from the seed. The caller decides how loudly
                  to fail; it must not guess the seed is a substitute.

    ``check_env`` defaults to False because showing the list must not depend on
    which variables the reading process holds. The monitor, which needs them
    resolved, resolves each reference itself and fails naming the variable.
    """
    path = _resolve(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"'{path}' cannot be read ({_one_line(exc)})") from exc

    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise StoreError(f"'{path}' is not valid JSON ({_one_line(exc)})") from exc

    entries = raw.get("targets") if isinstance(raw, dict) else raw
    try:
        return validate_all(entries, check_env=check_env)
    except ValidationError as exc:
        raise StoreError(
            f"'{path}' holds an entry the store refuses ({_one_line(exc)})"
        ) from exc


def read_seed(path: Path | None = None) -> tuple[list[dict], str | None]:
    """
    Return ``(entries, problem)``: the seed as the store would hold it, or an
    empty list and a message saying why it cannot be shown.

    Entries come back normalised by the store's own rules and with every
    ``${VAR}`` still a reference. The read is open -- no token -- so a resolved
    secret here would be published to whatever can reach the port, and a save
    of what the editor drew has to leave the reference in the store, where
    main.py resolves it at load time. The environment is therefore not
    consulted (``check_env=False``): the dashboard process may not hold the
    variable, and not holding it must not hide the list.

    A key the store does not know is dropped, because load_targets does
    exactly that with the same YAML. Rejecting it instead would make a file
    the monitor runs on perfectly well unshowable, and the editor would open
    empty -- the state this function exists to end.

    ``problem`` is never swallowed into an empty list: an empty list with no
    explanation reads as "nothing is monitored", and saving over it is how the
    real list was lost.
    """
    path = _resolve_seed(path)

    if not path.exists():
        logger.warning("Seed file '%s' does not exist for the editor.", path)
        return [], (
            f"'{path}' does not exist for the dashboard process. If it runs "
            f"in a container, the file has to be mounted in that service too."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        logger.warning("Seed file '%s' is not valid YAML: %s", path, exc)
        return [], f"'{path}' is not valid YAML ({' '.join(str(exc).split())})."
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Seed file '%s' could not be read: %s", path, exc)
        return [], f"Could not read '{path}' ({exc})."

    entries = raw.get("targets", []) if isinstance(raw, dict) else []
    if not isinstance(entries, list):
        return [], f"'targets' in '{path}' must be a list."

    coerced: list[dict] = []
    for entry in entries:
        if isinstance(entry, str):
            coerced.append({"url": entry})
        elif isinstance(entry, dict) and entry.get("url"):
            coerced.append({
                "url": str(entry["url"]),
                **{k: v for k, v in entry.items()
                   if k in ALLOWED_KEYS and k != "url"},
            })
        else:
            return [], (
                f"'{path}' has an invalid entry ({entry!r}): each entry "
                f"must be a URL or an object with 'url'."
            )

    try:
        return validate_all(coerced, check_env=False), None
    except ValidationError as exc:
        logger.warning("Seed file '%s' has an entry the store refuses: %s", path, exc)
        return [], (
            f"'{path}' has an entry the editor cannot display ({exc}). "
            f"The monitor loads it with looser rules than the store."
        )


def write_store(entries: list[dict], path: Path | None = None) -> list[dict]:
    """
    Validate and persist *entries*, atomically. Returns what was stored.

    Same tmp-then-replace the state file uses, and for the same reason: the
    probe reads this file on a schedule, and a reader that catches a partial
    write would either crash or silently probe half a list. The temporary file
    is created in the target's own directory so the replace stays on one
    filesystem, where it is atomic.
    """
    path      = _resolve(path)
    validated = validate_all(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"targets": validated}, indent=2, ensure_ascii=False)

    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with handle as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    logger.info(
        "Target list saved: %d target(s) in '%s'. event_type=metric "
        "metric=targets_written_total value=%d",
        len(validated), path, len(validated),
    )
    return validated
