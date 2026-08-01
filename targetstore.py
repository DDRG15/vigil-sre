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

Python  : 3.11+
Depends : stdlib only (json, os, pathlib).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("sre.targetstore")

#: Where the dashboard-managed list lives. Inside `data/` because that is a
#: directory mount in docker-compose.yml, and an atomic rename needs one.
STORE_FILE: Path = Path(os.getenv("TARGETS_STORE_PATH", "data/targets.json"))


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

#: Only these keys may appear in an entry. Anything else is rejected, never
#: ignored: a field that is silently dropped is a setting the operator
#: believes is in effect.
ALLOWED_KEYS: frozenset[str] = frozenset({
    "url", "expect_substring", "expected_status", "timeout_s",
    "degraded_ttfb_ms", "degraded_rtt_ms", "remind", "maintenance",
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
        raise ValidationError("'maintenance' debe ser una lista de ventanas.")
    if len(raw) > MAX_WINDOWS:
        raise ValidationError(f"Máximo {MAX_WINDOWS} ventanas por target.")

    out: list[dict] = []
    for window in raw:
        if not isinstance(window, dict):
            raise ValidationError("Cada ventana debe ser un objeto.")
        if set(window) - {"days", "start", "end"}:
            raise ValidationError(
                "Una ventana solo admite 'days', 'start' y 'end'.")

        clean: dict = {}
        for field in ("start", "end"):
            value = window.get(field)
            if not isinstance(value, str) or not _HHMM.match(value):
                raise ValidationError(
                    f"'{field}' debe tener formato HH:MM en 24h (UTC).")
            clean[field] = value

        if clean["start"] == clean["end"]:
            # Otherwise it is either a zero-length window or a 24h one, and
            # which one it means is a coin flip. Make the operator say it.
            raise ValidationError(
                "'start' y 'end' no pueden ser iguales — para silenciar todo "
                "el día usá 00:00 a 23:59.")

        days = window.get("days")
        if days is not None:
            if (not isinstance(days, list) or not days
                    or any(not isinstance(d, int) or isinstance(d, bool)
                           or not 0 <= d <= 6 for d in days)):
                raise ValidationError(
                    "'days' debe ser una lista de enteros 0-6 (0 = lunes).")
            clean["days"] = sorted(set(days))

        out.append(clean)
    return out


class ValidationError(ValueError):
    """An entry the store refuses to hold, with a message meant for a human."""


def validate_entry(raw: object) -> dict:
    """
    Return a normalised entry, or raise ValidationError explaining why not.

    Rejects rather than coerces. A target list that quietly repaired what it
    was given would leave the operator reading a dashboard that disagrees with
    what is actually being probed.
    """
    if not isinstance(raw, dict):
        raise ValidationError("Cada target debe ser un objeto con al menos 'url'.")

    unknown = set(raw) - ALLOWED_KEYS
    if unknown:
        raise ValidationError(
            f"Campo(s) no reconocido(s): {', '.join(sorted(unknown))}. "
            f"Permitidos: {', '.join(sorted(ALLOWED_KEYS))}."
        )

    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValidationError("'url' es obligatorio y debe ser texto.")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValidationError(f"La URL supera {MAX_URL_LENGTH} caracteres.")

    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        # file://, gopher:// and friends would turn "monitor this endpoint"
        # into "read this path", using the monitor's own privileges.
        raise ValidationError(
            f"Esquema '{parsed.scheme or 'ninguno'}' no permitido. "
            f"Solo {' y '.join(sorted(ALLOWED_SCHEMES))}."
        )
    if not parsed.netloc:
        raise ValidationError("La URL no tiene host.")

    entry: dict = {"url": url}

    substring = raw.get("expect_substring")
    if substring is not None:
        if not isinstance(substring, str):
            raise ValidationError("'expect_substring' debe ser texto.")
        if substring:
            problem = env_reference_problem(substring)
            if problem:
                code, name = problem
                raise ValidationError({
                    "malformed": (
                        "'expect_substring' con '${' debe ser una referencia "
                        "COMPLETA a una variable de entorno, por ejemplo "
                        "'${HEALTH_TOKEN}'. No se admite concatenación."
                    ),
                    "lowercase": (
                        f"'expect_substring' referencia ${{{name}}}, pero debe "
                        f"ir en MAYÚSCULAS: ${{{name.upper()}}}. Windows las "
                        f"resuelve sin distinguir mayúsculas y Linux no, así "
                        f"que en minúsculas el mismo target se comporta "
                        f"distinto en tu máquina y en producción."
                    ),
                    "unset": (
                        f"'expect_substring' referencia ${{{name}}}, pero esa "
                        f"variable no está definida (o está vacía). Guardarla "
                        f"así detendría el monitor entero en la próxima "
                        f"corrida, no solo este target."
                    ),
                }[code])
            entry["expect_substring"] = substring

    for field, (low, high) in NUMERIC_BOUNDS.items():
        value = raw.get(field)
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"'{field}' debe ser un número.")
        if not (low <= value <= high):
            raise ValidationError(f"'{field}' debe estar entre {low} y {high}.")
        entry[field] = int(value) if field == "expected_status" else float(value)

    windows = raw.get("maintenance")
    if windows:
        entry["maintenance"] = _validated_windows(windows)

    remind = raw.get("remind", False)
    if not isinstance(remind, bool):
        raise ValidationError("'remind' debe ser true o false.")
    entry["remind"] = remind

    return entry


def validate_all(entries: object) -> list[dict]:
    """Validate a whole list, and reject duplicate URLs."""
    if not isinstance(entries, list):
        raise ValidationError("Se esperaba una lista de targets.")
    if len(entries) > MAX_TARGETS:
        raise ValidationError(f"Máximo {MAX_TARGETS} targets.")

    validated = [validate_entry(entry) for entry in entries]
    seen: set[str] = set()
    for entry in validated:
        if entry["url"] in seen:
            # Two rows for one URL would each write history under the same key
            # and each render a row, so the page would disagree with itself.
            raise ValidationError(f"Target duplicado: {entry['url']}")
        seen.add(entry["url"])
    return validated


def read_store(path: Path | None = None) -> list[dict] | None:
    """
    Return the stored targets, or None when the dashboard has never written.

    None and [] are different answers and must stay that way: None means "the
    store does not exist, fall back to targets.yaml", while [] means "the
    operator deliberately removed every target". Collapsing them would
    resurrect a deleted list from the YAML on the next run.
    """
    path = _resolve(path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(
            "No se pudo leer '%s': %s — se usará targets.yaml. Corregí o "
            "borrá el archivo para volver a administrar desde el dashboard.",
            path, exc,
        )
        return None
    entries = raw.get("targets") if isinstance(raw, dict) else raw
    try:
        return validate_all(entries)
    except ValidationError as exc:
        logger.error(
            "'%s' contiene una entrada inválida (%s) — se usará targets.yaml.",
            path, exc,
        )
        return None


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
