"""
tests/test_image_pinning.py -- the same commit builds the same stack.

`healthchecks/healthchecks:latest` moved from v4 to v5 on 20 October 2026, and
nothing in this repository changed: the next `docker compose --profile
demo-watchdog up` would have run a different major version under a provisioning
script that imports that image's internals. The Python base image and the
Grafana plugin had the same property -- a tag, or no version at all, is a
pointer someone else moves.

Every image is now `name:tag@sha256:digest`: the tag says what it is to a
reader, the digest is what Docker actually pulls. The plugin carries a version.
Moving any of them is a commit, and the commit is where a broken upgrade shows up.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

PINNED = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")


def _from_lines() -> list[str]:
    lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    return [l.split()[1] for l in lines if l.startswith("FROM ")]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_every_dockerfile_base_image_is_pinned_by_digest() -> None:
    images = _from_lines()
    assert images, "the Dockerfile has no FROM"
    for image in images:
        assert PINNED.match(image), f"not pinned to tag@sha256 digest: {image!r}"


def test_the_build_and_runtime_stages_share_one_base() -> None:
    """The builder installs into site-packages that the runtime stage copies
    verbatim. Two bases pinned separately can drift to different Python patch
    releases, and compiled wheels then load against an interpreter they were
    not built for -- with no error at build time."""
    assert len(set(_from_lines())) == 1, _from_lines()


def test_every_compose_image_is_pinned_by_digest() -> None:
    images = {name: svc["image"] for name, svc in _compose()["services"].items()
              if "image" in svc}
    assert images, "compose names no third-party image"
    for name, image in images.items():
        assert PINNED.match(image), f"{name}: not pinned to tag@sha256 digest: {image!r}"


def test_every_grafana_plugin_has_a_version() -> None:
    env = _compose()["services"]["grafana"]["environment"]
    pairs = dict(e.split("=", 1) for e in env)
    plugins = pairs["GF_INSTALL_PLUGINS"].split(",")
    for plugin in plugins:
        parts = plugin.strip().split()
        assert len(parts) == 2 and re.fullmatch(r"\d+\.\d+\.\d+", parts[1]), (
            f"plugin without a pinned version: {plugin!r}")
