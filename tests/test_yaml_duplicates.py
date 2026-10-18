"""
tests/test_yaml_duplicates.py — one URL, one target, in the YAML seed too.

targetstore.validate_all already refuses a duplicate URL for the store.
load_targets did not, for the YAML. Both entries then shared one state record
(the state is keyed by URL), so two failures in the SAME round counted as two
consecutive failures and confirmed DOWN without the two-round hysteresis, the
history gained two rows per round, and two entries with different settings
fought over one record.

Identity is the exact string, the same as the state key: paths, queries and
trailing slashes are not normalised, because normalising here and not there
would let two entries that the state treats as different be rejected, or two
that it treats as one pass.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main

URL = "https://dup.example/health"


def _load(tmp_path: Path, body: str):
    path = tmp_path / "targets.yaml"
    path.write_text(body, encoding="utf-8")
    return main.load_targets(path, store_path=tmp_path / "no-store.json")


FORMS = {
    "string-string": f"targets:\n  - {URL}\n  - {URL}\n",
    "object-object": (
        f"targets:\n  - url: {URL}\n    timeout_s: 5\n"
        f"  - url: {URL}\n    timeout_s: 20\n"
    ),
    "string-then-object": f"targets:\n  - {URL}\n  - url: {URL}\n    remind: true\n",
    "object-then-string": f"targets:\n  - url: {URL}\n    remind: true\n  - {URL}\n",
    "not-adjacent": (
        f"targets:\n  - {URL}\n  - https://other.example\n"
        f"  - url: {URL}\n"
    ),
}


@pytest.mark.parametrize("body", FORMS.values(), ids=FORMS.keys())
def test_a_duplicate_url_fails_the_load_naming_it(
        tmp_path: Path, body, caplog) -> None:
    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(SystemExit) as exit_info:
            _load(tmp_path, body)

    assert exit_info.value.code == 1
    critical = [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]
    assert any(URL in message and "duplicate" in message.lower() for message in critical), critical


@pytest.mark.parametrize("a,b", [
    ("https://same.example", "https://same.example/"),     # trailing slash
    ("https://same.example/x", "https://same.example/x?a=1"),   # query
    ("https://same.example/x", "https://SAME.example/x"),  # host case
    ("https://same.example/x", "http://same.example/x"),   # scheme
])
def test_urls_that_differ_as_strings_are_different_targets(tmp_path: Path, a, b) -> None:
    """The state keys these as two records, so the loader must too."""
    got = _load(tmp_path, f"targets:\n  - {a}\n  - {b}\n")
    assert [t.url for t in got] == [a, b]


def test_distinct_urls_still_load(tmp_path: Path) -> None:
    got = _load(tmp_path, "targets:\n  - https://a.example\n  - url: https://b.example\n")
    assert [t.url for t in got] == ["https://a.example", "https://b.example"]
