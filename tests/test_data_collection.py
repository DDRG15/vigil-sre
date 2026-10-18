"""
tests/test_data_collection.py — the 14-day collection workflow, as text.

The workflow probes a fixed list of public sites once an hour for two weeks
and appends the rows to a separate branch. Its failure modes are all silent:
a commit identity that GitHub maps to a stranger's account, a push that lands
on main, a schedule that outlives its window, random targets that make two
runs incomparable. None of them fails a run, so each one is pinned here.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import yaml

import targetstore

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "data-collection.yml"
TARGETS = ROOT / "collection-targets.yaml"

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_it_runs_only_on_its_schedule_or_by_hand():
    # PyYAML reads the key `on` as the boolean True.
    triggers = _workflow().get(True) or _workflow().get("on")
    assert set(triggers) == {"schedule", "workflow_dispatch"}, (
        "a push or pull_request trigger would probe on every commit")


def test_commits_are_made_only_as_the_actions_bot():
    """`<name>@users.noreply.github.com` is credited to the GitHub account with
    that name. A collector that once committed under an ordinary word put a
    stranger's account in this repository's contributors. The bot's own address
    is the only identity allowed."""
    text = _text()
    assert f'user.name  "{BOT_NAME}"' in text or f'user.name "{BOT_NAME}"' in text
    assert f'user.email "{BOT_EMAIL}"' in text
    emails = set(re.findall(r"[\w.+\[\]-]+@users\.noreply\.github\.com", text))
    assert emails == {BOT_EMAIL}, f"unexpected commit identity: {emails - {BOT_EMAIL}}"


def test_the_data_never_goes_to_main():
    env = _workflow()["env"]
    branch = env["DATA_BRANCH"]
    assert branch not in ("main", "master", ""), "data must live on its own branch"
    text = _text()
    assert re.search(r"git push origin \"?\$DATA_BRANCH\"?", text), "push target is not the data branch"
    assert not re.search(r"git push[^\n]*\b(main|master)\b", text), "a push to main is present"


def test_the_window_is_fourteen_days_and_the_schedule_switches_itself_off():
    env = _workflow()["env"]
    start = datetime.fromisoformat(env["COLLECT_FROM"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(env["COLLECT_UNTIL"].replace("Z", "+00:00"))
    assert (end - start).days == 14
    text = _text()
    assert "actions/workflows/data-collection.yml/disable" in text, (
        "the switch-off step must disable THIS workflow, by its own file name")
    steps = _workflow()["jobs"]["probe"]["steps"]
    off = next(s for s in steps if "Switch off" in s.get("name", ""))
    assert off.get("if") == "always()", "the switch-off must run even when a probe failed"


def test_it_never_reaches_a_real_channel():
    """Every run starts with no state, so every DOWN target is a first sighting
    and would alert. The workflow has no channels: the webhooks are blank on
    purpose, never the placeholder from .env.example (which Discord answers)."""
    steps = _workflow()["jobs"]["probe"]["steps"]
    probe = next(s for s in steps if s.get("name") == "Probe once")
    env = probe.get("env", {})
    for var in ("DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "HEARTBEAT_URL"):
        assert env.get(var) == "", f"{var} must be blank in the probe step"


def test_the_targets_are_a_fixed_valid_list_of_ten_to_twenty():
    """Fixed, not random: the trend and the daily summaries compare a target with
    itself over time, which needs the same targets in every run."""
    raw = yaml.safe_load(TARGETS.read_text(encoding="utf-8"))
    entries = raw["targets"]
    assert 10 <= len(entries) <= 20
    urls = [e["url"] if isinstance(e, dict) else e for e in entries]
    assert len(urls) == len(set(urls)), "duplicate target"
    assert all(u.startswith("https://") for u in urls)
    targetstore.validate_all([e if isinstance(e, dict) else {"url": e} for e in entries],
                             check_env=False)
    assert "collection-targets.yaml targets.yaml" in _text(), (
        "the probe must run on this list, not on the shipped targets.yaml")
