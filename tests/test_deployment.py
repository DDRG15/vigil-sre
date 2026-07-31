"""
tests/test_deployment.py — the packaging layer no phase audit ever opened.

Why this file exists
--------------------
Every phase audited its own diff, and many releases came out clean that way. But
a diff does not contain the files that SHOULD have changed and did not, and the
Dockerfile is exactly that: a file to touch each time a module is born, which no
audit of a new module had any reason to open.

The cost was a CRITICAL. `COPY main.py diagnostics.py history.py ./` went five
phases without gaining notifiers.py, so the image could not import its own
entrypoint from an earlier release onward — a crash-loop behind a build that succeeded
every single time, because copying three of six files is a successful copy.

CI has a step that would have caught it on day one. It never ran: CI had not run. So the guard belongs where it runs on every `pytest tests/` too, not
only where it runs on push.

These assert against the deployment files as text. That is deliberate — they
must fail in the same commit a module goes uncopied, not in the audit five
phases later, and they must not need Docker to be installed or running.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_the_image_copies_every_module_the_app_imports() -> None:
    """The an earlier release CRITICAL, in one assertion.

    A per-file COPY list desynchronises silently: it happened in an earlier release, was
    fixed by naming the missing file, and happened again in an earlier release because
    naming files is a step somebody has to remember. Either the Dockerfile
    copies the package wholesale, or it names every module that exists -- and
    this test is what notices when neither is true.
    """
    dockerfile = _read("Dockerfile")
    modules    = {p.name for p in ROOT.glob("*.py")}
    assert modules, "no application modules found — the glob is wrong, not the Dockerfile"

    wholesale = "COPY *.py ./" in dockerfile
    itemised  = all(m in dockerfile for m in modules)
    missing   = sorted(m for m in modules if m not in dockerfile)
    assert wholesale or itemised, (
        f"Dockerfile does not copy every module; missing: {missing}. "
        "The image will build fine and then fail to start."
    )


def test_the_image_never_bakes_in_operational_data() -> None:
    """`COPY *.py ./` moved the decision of what stays out of the image into
    .dockerignore. state.json and history.db are host state, not image content,
    and history.db additionally holds every measurement ever taken."""
    ignored = _read(".dockerignore")
    for artefact in ("state.json", "history.db", ".env", "tests/"):
        assert artefact in ignored, f"{artefact} is not excluded from the image"


def test_compose_actually_runs_the_api() -> None:
    """an earlier release's implementation plan, step 4, called for a separate compose
    service. It shipped without one, so two whole phases -- the JSON API and
    the dashboard on top of it -- existed only in the test suite. Passing tests
    say nothing about whether anything is running."""
    compose = _read("docker-compose.yml")
    assert "api.py" in compose, "no compose service runs api.py"
    assert "8787" in compose, "the API port is never published"


def test_the_api_port_is_published_only_to_loopback() -> None:
    """This endpoint has no authentication and serves the operational map:
    which targets exist, when they fail, with what error. A bare "8787:8787"
    publishes that on every interface of the host, which is how an operator
    backs into exposure rather than choosing it."""
    # Matched by SHAPE, not by searching for the port number anywhere. Comments
    # are stripped (the file explains in prose why a bare "8787:8787" is wrong,
    # which a text search would read as the mistake it warns about) and only
    # compose's short-syntax port entries are considered -- the healthcheck
    # command mentions 8787 too, and it is not a publication.
    entries = [
        line.split("#", 1)[0].strip()
        for line in _read("docker-compose.yml").splitlines()
    ]
    published = [
        m.group(1)
        for line in entries
        if (m := re.fullmatch(r'-\s*"?([\d.]*:?\d+:\d+)"?', line))
    ]
    assert published, "the API port is never published"
    for entry in published:
        assert entry.startswith("127.0.0.1:"), f"port published off-loopback: {entry}"
