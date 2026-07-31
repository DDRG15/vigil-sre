"""
tests/test_deployment.py — guards on the packaging layer.

Why this file exists
--------------------
Reviewing a change against its own diff catches a lot, but a diff does not
contain the files that SHOULD have changed and did not. The Dockerfile is
exactly that kind of file: it has to be touched every time a module is born,
and nothing about writing that module points at it.

The cost of missing it is an image that cannot import its own entrypoint. A
`COPY` list naming three modules while the package holds six is a perfectly
successful build — nothing fails until the container runs.

CI covers this too, but CI only runs on push. The guard belongs where it also
runs on every `pytest tests/`.

These assert against the deployment files as text. That is deliberate: they
must fail in the same commit a module goes uncopied, and they must not need
Docker to be installed or running.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_the_image_copies_every_module_the_app_imports() -> None:
    """The packaging CRITICAL, in one assertion.

    A per-file COPY list desynchronises silently: it happened once, was
    fixed by naming the missing file, and happened again later because
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
    """The implementation plan, step 4, called for a separate compose
    service. It shipped without one, so two whole features -- the JSON API and
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
