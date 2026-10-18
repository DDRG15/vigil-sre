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
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _services() -> dict:
    """docker-compose.yml, parsed. The shape assertions below read structure
    (profiles, mounts, environment) rather than grepping text: a mount that
    moved to the wrong service still contains the right string."""
    return yaml.safe_load(_read("docker-compose.yml"))["services"]


def _env(service: dict) -> dict[str, str]:
    """A service's `environment:` list as a dict (list-of-KEY=VALUE form)."""
    out: dict[str, str] = {}
    for item in service.get("environment", []):
        key, _, value = item.partition("=")
        out[key] = value
    return out


def _history_mount(service: dict) -> tuple[str, str, bool]:
    """(host, container, read_only) of the mount that carries the history
    directory. Fails if there is none: that is the defect."""
    for volume in service.get("volumes", []):
        host, container, *mode = volume.split(":")
        if container.rstrip("/").endswith("/history"):
            return host, container, "ro" in mode
    raise AssertionError("no volume mounts the history directory")


def _active_lines(text: str) -> str:
    """Executed lines only: comments in this file quote the forms they forbid."""
    return "\n".join(
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


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


def _dockerignore_admits(path: str) -> bool:
    """Whether the build context contains `path`, by .dockerignore's rules: the
    last matching pattern wins, `!` re-includes, and `*` stops at a slash. A
    path under an excluded directory is excluded unless a pattern names it, so
    each prefix is checked from the root down."""
    rules: list[tuple[bool, re.Pattern]] = []
    for raw in _read(".dockerignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        glob = line[1:] if negate else line
        regex = "".join("[^/]*" if c == "*" else re.escape(c) for c in glob)
        rules.append((negate, re.compile(regex.rstrip("/") + r"/?")))

    parts = path.split("/")
    for depth in range(1, len(parts) + 1):
        prefix = "/".join(parts[:depth])
        admitted = True
        for negate, pattern in rules:
            if pattern.fullmatch(prefix):
                admitted = negate
        if not admitted:
            return False
    return True


def test_the_image_never_bakes_in_operational_data() -> None:
    """`COPY *.py ./` moved the decision of what stays out of the image into
    .dockerignore, which is an allowlist: everything is excluded and only what
    the Dockerfile copies is let back in. state.json and history.db are host
    state, not image content, and history.db additionally holds every
    measurement ever taken. The cases are evaluated rather than grepped because
    an allowlist never names the files it keeps out."""
    for artefact in (
        "state.json", "history.db", ".env", "targets.yaml",
        "data/history/history.db", "tests/test_main.py", "requirements-dev.txt",
        ".git/config", "notes.md",
    ):
        assert not _dockerignore_admits(artefact), (
            f"{artefact} would be sent to the image build"
        )


def test_the_build_context_still_holds_what_the_dockerfile_copies() -> None:
    """The other direction: an allowlist that is too tight builds nothing. The
    context must carry the dependency manifest and every module at the root,
    since a module missing from it is a successful build of an image that
    cannot start."""
    assert _dockerignore_admits("requirements.txt")
    modules = sorted(p.name for p in ROOT.glob("*.py"))
    assert modules, "no application modules found"
    for module in modules:
        assert _dockerignore_admits(module), f"{module} is excluded from the image"


def test_compose_actually_runs_the_api() -> None:
    """The API was meant to run as a separate compose service. It shipped
    without one, so two whole features -- the JSON API and
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


def test_ci_runs_the_quickstart_without_hand_made_prerequisites() -> None:
    """The reason CI never saw a clean clone fail, as an assertion.

    The deployment job pre-created `history.db` and injected a watchdog key,
    which are exactly the two steps the README quickstart omits. A job that
    performs the steps the user is not told about proves the steps work, not
    the quickstart. With the demo behind a profile and the history in a
    directory the repo ships, neither is needed, and CI must not supply them.
    """
    steps = _active_lines(_read(".github/workflows/ci.yml"))
    assert "WATCHDOG_SECRET_KEY" not in steps, (
        "the default profile no longer needs a watchdog key; injecting one "
        "hides a regression where it needs it again"
    )
    assert ": > history.db" not in steps and "touch history.db" not in steps, (
        "CI creates the file the quickstart does not, so a clean clone's "
        "failure is invisible to it"
    )
    # `-e` sits between `--rm` and the service name, so the line is matched by
    # its two ends rather than as one literal.
    assert re.search(
        r"docker compose run --rm .*health-checker python main\.py", steps
    ), "CI no longer runs one real cycle through compose"


def _ci_run(step_prefix: str) -> str:
    """The executed lines of one docker-job step, found by name. A step that
    was renamed or deleted is the defect, so a miss fails loudly."""
    job = yaml.safe_load(_read(".github/workflows/ci.yml"))["jobs"]["docker"]
    for step in job["steps"]:
        if step.get("name", "").startswith(step_prefix):
            return _active_lines(step["run"])
    raise AssertionError(f"no docker-job step named {step_prefix!r}")


def test_ci_cycle_delivers_to_a_receiver_it_started() -> None:
    """The docker job went red on push for a reason that was not a bug.

    .env.example carries a placeholder Discord URL. One real cycle fires alerts,
    Discord answers 400 to the placeholder, nobody receives them, and an
    undelivered alert is a monitor failure: exit 1. The rule is right (an EMPTY
    URL exits 1 as well) and stays. What changes is the destination: the job
    starts a local sink and points the cycle at it, so a green run means the
    alert was delivered somewhere, not that nothing happened.
    """
    cycle = _ci_run("Run one real health-check cycle")

    assert "scripts/ci-webhook-receiver.py" in cycle, "no receiver is started"
    compose_run = next(
        (line for line in cycle.splitlines() if "docker compose run" in line), "")
    assert re.search(
        r'-e DISCORD_WEBHOOK_URL="?http://\$\{?HOST_IP\}?:\d+/', compose_run
    ), f"the cycle is not pointed at the local receiver: {compose_run!r}"
    assert "discord.com" not in cycle, "the placeholder URL is back in the job"


def test_ci_does_not_weaken_the_exit_rule_to_get_green() -> None:
    """Two ways to make this job pass without fixing anything: swallow the
    exit code, or hand the monitor a webhook that cannot fail. Both turn "an
    alert reached nobody" back into a green tick."""
    cycle = _ci_run("Run one real health-check cycle")

    assert "|| true" not in cycle, "the cycle's exit code is swallowed"
    assert "set +e" not in cycle, "the cycle's exit code is swallowed"
    assert not re.search(r'DISCORD_WEBHOOK_URL=("")?\s', cycle + " "), (
        "an empty webhook exits 1 by design; passing one only hides that"
    )


def test_ci_asserts_the_alert_actually_arrived() -> None:
    """Exit 0 alone does not say an alert was sent: a run that fired nothing
    also exits 0. The receiver's log is the observer that cannot be fooled."""
    arrived = _ci_run("The alert reached the receiver")

    assert re.search(r'test "\$HITS" -ge 1', arrived), (
        "no assertion that the receiver got at least one POST"
    )


def test_ci_guarantees_the_cycle_has_an_alert_to_deliver() -> None:
    """"At least one POST" is only a proof if something is always sent.

    The shipped targets are all healthy endpoints, and a healthy target seen
    for the first time is recorded, not announced. On a runner where all of
    them answer, the cycle alerts nobody and the receiver check fails for a
    network that worked. The job therefore appends one target that cannot
    answer before the cycle runs; its first sighting is a DOWN, which alerts.
    The appended line must also still parse as a target, or the guarantee is
    a YAML error instead.
    """
    cycle = _ci_run("Run one real health-check cycle")
    lines = cycle.splitlines()

    appended = next((l for l in lines if ">> targets.yaml" in l), None)
    assert appended is not None, "no guaranteed-DOWN target is added before the cycle"
    run_at = next(i for i, l in enumerate(lines) if "docker compose run" in l)
    assert lines.index(appended) < run_at, "the target is added after the cycle ran"

    added = re.search(r"printf '(.*)\\n' >> targets\.yaml", appended)
    assert added, f"unexpected form for the appended target: {appended!r}"
    seed = yaml.safe_load(_read("targets.yaml") + added.group(1) + "\n")
    urls = [t["url"] if isinstance(t, dict) else t for t in seed["targets"]]
    assert urls[-1].startswith("http://127.0.0.1:"), (
        f"the appended line did not become the last target: {urls[-1]!r}"
    )


def test_ci_asserts_the_runtime_files_landed_as_files() -> None:
    """A directory where a file belongs raised no error anywhere: the monitor
    logged and carried on. The only observer that cannot be fooled by that is
    one that looks at the host after the run."""
    steps = _active_lines(_read(".github/workflows/ci.yml"))
    assert "test -f data/state.json" in steps
    assert "test -f data/history/history.db" in steps


def test_stopping_does_not_depend_on_the_key_being_set() -> None:
    """Being able to stop a system must not depend on its config being valid.

    `${WATCHDOG_SECRET_KEY:?...}` was checked by compose interpolation, which
    runs before EVERY subcommand -- so the guard also broke `stop`, `down`,
    `logs`, `ps` and `config --services`. The configuration could not be
    inspected without the key, let alone shut down, and a broken config is
    exactly when stopping matters most.
    """
    # Executed lines only. The comment above the guard quotes the old `:?` form
    # to explain what was wrong with it, and a scan of the raw text matches that
    # quote -- the third time in this project a source scan has been fooled by a
    # comment describing the very thing it forbids.
    active = _active_lines(_read("docker-compose.yml"))
    assert "WATCHDOG_SECRET_KEY:?" not in active, (
        "the `:?` form aborts every compose subcommand, not just `up`"
    )


def test_an_empty_key_is_still_refused_before_the_watchdog_starts() -> None:
    """Moving the check out of interpolation must not delete it.

    `:-` alone would have been the one-line fix and it is wrong. Verified against
    healthchecks/healthchecks:latest: with SECRET_KEY empty the container STAYS
    UP rather than exiting, because uWSGI keeps its master process alive around
    Django's ImproperlyConfigured and the image ships no entrypoint to turn that
    into a non-zero exit. Under `restart: unless-stopped` it would look alive
    while signing session cookies with nothing.

    So a service has to own the refusal, and the watchdog has to wait for it.
    Putting both behind a profile changes WHEN the refusal runs (only when the
    demo is asked for), not whether it runs.
    """
    services = _services()
    guard, watchdog = services["watchdog-guard"], services["watchdog"]

    assert watchdog["depends_on"]["watchdog-guard"]["condition"] == (
        "service_completed_successfully"
    ), "the watchdog must wait for the guard to exit 0, or the guard is decoration"

    # The guard has to actually test the variable and fail, not just log.
    assert "WATCHDOG_SECRET_KEY" in _env(guard), "the guard does not read the variable"
    script = "\n".join(guard["command"])
    assert "-z" in script and "WATCHDOG_SECRET_KEY" in script, (
        "the guard does not test the variable for emptiness"
    )
    assert "exit 1" in script, "the guard never fails, so `up` would proceed"

    # Same profile on both: a guard left out of the activated set would let the
    # watchdog start (or fail to resolve its dependency) without the check.
    assert guard["profiles"] == watchdog["profiles"], (
        "guard and watchdog must be activated together"
    )


def test_the_watchdog_demo_is_opt_in() -> None:
    """`docker compose up -d --build` with the .env.example
    the README says to copy exited 1 on a clean clone, because that file has no
    WATCHDOG_SECRET_KEY and the guard ran unconditionally. A first install must
    not depend on a demo the reader did not ask for."""
    services = _services()
    assert services["watchdog-guard"].get("profiles") == ["demo-watchdog"]
    assert services["watchdog"].get("profiles") == ["demo-watchdog"]


def test_the_monitor_and_dashboard_start_without_any_profile() -> None:
    """The two services a first install needs must be in the default set. A
    profile on either would make `up -d --build` silently start less than the
    README promises."""
    services = _services()
    for name in ("health-checker", "dashboard"):
        assert "profiles" not in services[name], (
            f"{name} is behind a profile, so a plain `up` will not start it"
        )


def test_history_is_a_mounted_directory_in_both_services() -> None:
    """The history directory is mounted, never a single file.

    A single-file bind mount from a path that does not exist makes Docker
    create a directory there (history.db was born as one on every clean clone),
    and a lone file mount leaves SQLite's rollback journal beside it in the
    container layer, invisible to the host: SIGKILL during a commit plus a
    container recreate loses the hot journal SQLite needs to recover the file.
    A mounted directory fixes both, because the journal is created next to the
    database, inside the mount.
    """
    services = _services()
    monitor_host, monitor_target, monitor_ro = _history_mount(services["health-checker"])
    dash_host, dash_target, dash_ro = _history_mount(services["dashboard"])

    assert not monitor_host.endswith(".db") and not dash_host.endswith(".db"), (
        "history is mounted as a single file again"
    )
    assert monitor_host == dash_host and monitor_target == dash_target, (
        "monitor and dashboard must look at the same directory"
    )
    assert not monitor_ro, "the monitor writes the history"
    assert dash_ro, (
        "the dashboard must keep history read-only: its `./data` mount is "
        "writable now, and a nested :ro mount is what keeps it from writing here"
    )
    for name in ("health-checker", "dashboard"):
        for volume in services[name]["volumes"]:
            assert not volume.split(":")[1].endswith(".db"), (
                f"{name} still mounts a single database file: {volume}"
            )


def test_the_dashboard_sees_the_same_seed_the_monitor_runs_on() -> None:
    """The editor opens on targets.yaml while there is no store, and only if the
    dashboard can read it. Without this mount GET /api/targets answers with a
    seed_error under compose and the editor opens empty -- warned, but showing
    none of the targets the monitor is actually probing. Same host file, same
    path in the container, and read-only: the dashboard edits the store, never
    the seed."""
    services = _services()

    def seed_mount(name: str) -> tuple[str, bool]:
        for volume in services[name].get("volumes", []):
            host, container, *mode = volume.split(":")
            if container == "/app/targets.yaml":
                return host, "ro" in mode
        raise AssertionError(f"{name} does not mount /app/targets.yaml")

    monitor_host, _ = seed_mount("health-checker")
    dash_host, dash_ro = seed_mount("dashboard")
    assert dash_host == monitor_host, "the editor would show a different file"
    assert dash_ro, "the dashboard must not be able to write the seed"


def test_the_history_path_variable_points_inside_the_mounted_directory() -> None:
    """The variable and the mount are two halves of one fact. If the path
    named a file outside the mounted directory, the database would be written
    to the container layer and the mount would sit empty and look fine."""
    services = _services()
    for name in ("health-checker", "dashboard"):
        service = services[name]
        _, target, _ = _history_mount(service)
        value = _env(service).get("HISTORY_DB_PATH")
        assert value, f"{name} does not set HISTORY_DB_PATH"
        prefix = target.rstrip("/") + "/"
        assert value.startswith(prefix), (
            f"{name}: {value} is not inside the mounted directory {target}"
        )
        assert "/" not in value[len(prefix):], (
            f"{name}: {value} is nested below the mount; keep the file at its top"
        )


def test_the_image_creates_the_history_directory_for_the_sre_user() -> None:
    """Covers a run with no bind mount: the directory must exist and belong to
    the non-root user, or the first write fails on a permission error."""
    dockerfile = _read("Dockerfile")
    assert re.search(r"install -d -o sre -g sre[^\n]*/app/data/history", dockerfile), (
        "the image does not create /app/data/history for the sre user"
    )


def _runtime_stage() -> list[str]:
    """The runtime stage's instructions in order, continuation lines joined and
    comments dropped. The builder stage needs pip, so a line that is right
    there is wrong here and the two must not be confused."""
    joined = _active_lines(_read("Dockerfile")).replace("\\\n", " ")
    lines = joined.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if re.match(r"FROM\s+\S+\s+AS\s+runtime\b", l, re.IGNORECASE))
    return lines[start:]


def test_the_runtime_image_ships_no_package_manager() -> None:
    """The Dockerfile says the runtime image has "no pip" to keep the attack
    surface small, and the base image ships pip 24, setuptools and wheel in
    system site-packages regardless of what the builder stage did (measured).
    The dependencies arrive pre-built from the builder, so nothing at runtime
    uses them: they are a way to fetch and install code from inside a
    compromised container and have no function. Removed as root, before the
    privileges drop."""
    stage = _runtime_stage()
    uninstall = next((i for i, l in enumerate(stage)
                      if re.search(r"python -m pip uninstall -y pip setuptools wheel\b", l)),
                     None)
    assert uninstall is not None, "the runtime stage does not remove pip, setuptools and wheel"
    user = next(i for i, l in enumerate(stage) if l.startswith("USER "))
    assert uninstall < user, "pip is removed after privileges drop, where it cannot be"


def test_the_runtime_user_has_no_login_shell() -> None:
    """`useradd --system` without `-s` gives the account /bin/sh, so the
    comment above it ("no login shell") described something the build did not
    do."""
    stage = _runtime_stage()
    useradd = next((l for l in stage if "useradd" in l), None)
    assert useradd is not None, "the runtime stage creates no user"
    assert re.search(r"(?:\s-s|\s--shell)[ =]/usr/sbin/nologin(?:\s|$)", useradd), (
        f"the runtime user keeps a login shell: {useradd!r}"
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


def test_the_history_directory_exists_in_a_clean_clone(tmp_path: Path) -> None:
    """A bind mount of a missing host directory is created by the Docker daemon
    as root on Linux, and the non-root container user cannot write to it. The
    way to never hit that is for the directory to arrive with the clone, which
    needs a tracked file inside it and a .gitignore that lets exactly that file
    through while still keeping every database out of the repository.

    Run against git itself with the real .gitignore: this bug is invisible to a
    text search, because the ignore rule and the exception only conflict when
    git evaluates them together.
    """
    tracked = _git(ROOT, "ls-files", "--error-unmatch", "data/history/.gitkeep")
    assert tracked.returncode == 0, (
        "data/history/.gitkeep is not tracked, so a clean clone has no history "
        f"directory: {tracked.stderr}"
    )

    repo = tmp_path / "clone"
    (repo / "data" / "history").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text(_read(".gitignore"), encoding="utf-8")
    (repo / "data" / "history" / ".gitkeep").write_text("", encoding="utf-8")
    runtime_files = (
        "data/history/history.db", "data/history/history.db-journal",
        "data/state.json", "data/targets.json",
    )
    for runtime in runtime_files:
        (repo / runtime).write_text("x", encoding="utf-8")

    assert _git(repo, "add", "data/history/.gitkeep").returncode == 0, (
        ".gitignore blocks the file that makes the directory exist"
    )
    for runtime in runtime_files:
        assert _git(repo, "add", runtime).returncode != 0, (
            f"{runtime} is no longer ignored: runtime data can be committed"
        )


# ---------------------------------------------------------------------------
# `docker compose stop` ended in SIGKILL.
#
# PID 1 of the monitor is the `sh` running the loop. A signal that PID 1 has no
# handler for is ignored by the kernel, so SIGTERM did nothing; compose waited
# its 10 s default and sent SIGKILL (measured: 12.6 s, exit 137). main.py
# already finishes its in-flight checks on SIGTERM, but never got the signal.
# ---------------------------------------------------------------------------


def _monitor_script() -> str:
    """The shell script the health-checker service runs, as compose will see it
    (with `$$` still escaped)."""
    command = _services()["health-checker"]["command"]
    assert isinstance(command, list) and command[:2] == ["sh", "-c"], (
        "the loop must be an explicit `sh -c <script>` list: the script has to "
        f"carry a trap, and a folded string cannot hold one cleanly: {command!r}"
    )
    return command[2]


def _seconds(duration: str | int | float) -> float:
    """A compose duration (`150s`, `2m30s`, `1h`) in seconds."""
    if isinstance(duration, (int, float)):
        return float(duration)
    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", duration)
    assert parts and "".join(n + u for n, u in parts) == duration, (
        f"unparseable compose duration: {duration!r}"
    )
    return sum(float(n) * units[u] for n, u in parts)


def test_the_loop_forwards_sigterm_to_the_round_it_is_running() -> None:
    """A foreground child blocks the shell: sh defers a trap until the child
    exits, so `trap` over a plain `python main.py` still waits out the round and
    then the kill. The round runs in the background and the shell `wait`s, which
    a trapped signal interrupts at once."""
    script = _monitor_script()

    assert re.search(r"trap '[^']*kill -TERM[^']*' TERM", script), (
        "no trap forwards SIGTERM to the running round"
    )
    assert re.search(r"python main\.py\s*&", script), (
        "the round runs in the foreground, where a trap cannot interrupt it"
    )
    assert re.search(r"sleep 60\s*&", script), (
        "the 60 s pause runs in the foreground: a stop during it still waits "
        "for it to end"
    )


def test_a_failed_round_still_ends_the_loop_but_only_after_the_pause() -> None:
    """A failed round (an alert nobody received, a state file that did not
    persist) must still end the container with status 1, which is what makes
    restart bring it back. It must not end it AT ONCE: Docker's restart backoff
    resets after 10 s of uptime and a round outlasts that, so exiting straight
    away re-ran a persistently failing round back to back, with no pause at
    all, hammering the targets and the webhook. The behaviour is proved by the
    POSIX-sh tests below; this holds the shape where there is no sh to run."""
    script = _monitor_script()

    assert "exit 1" in script, "a failed round no longer ends the container"
    assert not re.search(r'wait "\$\$pid"\s*\|\|\s*exit 1', script), (
        "the round's failure is acted on before the pause is waited out"
    )
    pause = re.search(r"sleep 60\s*&", script)
    assert pause, "no pause in the loop"
    assert script.index("exit 1") > pause.start(), (
        "exit 1 comes before the pause, so a failed round skips it"
    )


def test_the_grace_period_outlasts_a_round_that_is_mid_delivery() -> None:
    """compose waits `stop_grace_period` and then SIGKILLs. A stop that lands
    while an alert is being delivered has to be allowed to finish delivering it:
    killing it there is the lost alert this project exists to prevent.

    The budget is computed from the constants, not copied: worst case per
    channel is every attempt timing out plus a capped Retry-After sleep between
    them (75 s today), channels run in parallel, and the in-flight probe that
    produced the alert can itself take up to the run budget.
    """
    import main
    import notifiers

    delivery = (notifiers.WEBHOOK_RETRY_ATTEMPTS * notifiers.WEBHOOK_TIMEOUT_S
                + (notifiers.WEBHOOK_RETRY_ATTEMPTS - 1)
                * notifiers.WEBHOOK_RETRY_AFTER_CAP_S)
    needed = delivery + main.RUN_BUDGET_S

    grace = _services()["health-checker"].get("stop_grace_period")
    assert grace is not None, "default 10 s grace: the first slow round is killed"
    assert _seconds(grace) >= needed, (
        f"stop_grace_period {grace} is shorter than one round's worst case "
        f"({needed:.0f} s: {delivery:.0f} s delivery + {main.RUN_BUDGET_S:.0f} s probe)"
    )


# The behaviour, not the shape: the real loop with `python main.py` swapped for
# a stand-in, run by a real POSIX shell. Skipped where there is none (Windows);
# CI's ubuntu runner has one. As a plain process the shell is not PID 1, so it
# has the kernel default and dies on SIGTERM unaided -- what these prove is that
# the round receives the signal and the exit codes survive. That PID 1 ignores
# an unhandled SIGTERM is the container half, which only Docker can show.
_needs_posix_sh = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None,
    reason="needs a POSIX sh",
)

_ROUND_WAITS_FOR_TERM = '''
import pathlib, signal, sys, time
work = pathlib.Path(sys.argv[1])
def on_term(*_):
    (work / "term").write_text("1")
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
(work / "started").write_text("1")
time.sleep(60)
'''

_ROUND_EXITS = '''
import pathlib, sys
work = pathlib.Path(sys.argv[1])
with (work / "runs").open("a") as handle:
    handle.write("x\\n")
sys.exit(int(sys.argv[2]))
'''


def _loop_with(tmp_path: Path, round_source: str, *round_args: str,
               pause: str = "60") -> subprocess.Popen:
    """The compose script, `$$` unescaped, with the round replaced and, when a
    test has to see the pause END, the pause shortened."""
    dummy = tmp_path / "round.py"
    dummy.write_text(round_source, encoding="utf-8")
    command = " ".join(f'"{a}"' for a in (sys.executable, dummy, tmp_path, *round_args))
    script = (_monitor_script().replace("$$", "$")
              .replace("python main.py", command)
              .replace("sleep 60", f"sleep {pause}"))
    return subprocess.Popen(["sh", "-c", script])


def _wait_for(path: Path, seconds: float = 15.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.05)


@_needs_posix_sh
def test_sigterm_reaches_the_running_round_and_the_loop_exits_cleanly(tmp_path: Path) -> None:
    loop = _loop_with(tmp_path, _ROUND_WAITS_FOR_TERM)
    try:
        _wait_for(tmp_path / "started")
        loop.terminate()
        assert loop.wait(timeout=10) == 0
    finally:
        if loop.poll() is None:
            loop.kill()
    assert (tmp_path / "term").exists(), "the round never received SIGTERM"


@_needs_posix_sh
def test_a_failing_round_ends_the_loop_with_status_1_after_one_run(tmp_path: Path) -> None:
    loop = _loop_with(tmp_path, _ROUND_EXITS, "3", pause="1")
    try:
        assert loop.wait(timeout=15) == 1
    finally:
        if loop.poll() is None:
            loop.kill()
    assert (tmp_path / "runs").read_text().count("x") == 1, (
        "the loop ran another round after a failure instead of ending"
    )


@_needs_posix_sh
def test_a_failing_round_waits_out_the_pause_before_the_container_exits(tmp_path: Path) -> None:
    """The cadence defect: exiting at once let Docker restart the container
    straight away, so a persistent failure re-ran rounds back to back. The exit
    has to come a full pause after the round, and the status has to survive the
    wait."""
    pause = 2
    started = time.monotonic()
    loop = _loop_with(tmp_path, _ROUND_EXITS, "3", pause=str(pause))
    try:
        assert loop.wait(timeout=20) == 1, "the round's status was lost across the pause"
    finally:
        if loop.poll() is None:
            loop.kill()
    elapsed = time.monotonic() - started
    assert elapsed >= pause - 0.3, (
        f"the container exited {elapsed:.1f} s after starting a round that "
        f"failed at once: the {pause} s pause was skipped"
    )
    assert (tmp_path / "runs").read_text().count("x") == 1


@_needs_posix_sh
def test_sigterm_during_the_pause_after_a_failed_round_still_exits_zero(tmp_path: Path) -> None:
    """A stop that was asked for is not a failure, even when the round before it
    was. The trap owns the exit status once the signal lands."""
    loop = _loop_with(tmp_path, _ROUND_EXITS, "3")
    try:
        _wait_for(tmp_path / "runs")
        time.sleep(0.5)                      # into the pause that follows the failure
        started = time.monotonic()
        loop.terminate()
        assert loop.wait(timeout=10) == 0
        assert time.monotonic() - started < 5
    finally:
        if loop.poll() is None:
            loop.kill()


@_needs_posix_sh
def test_a_passing_round_is_repeated_after_the_pause(tmp_path: Path) -> None:
    """The change must not turn the loop into a single run."""
    loop = _loop_with(tmp_path, _ROUND_EXITS, "0", pause="1")
    try:
        deadline = time.monotonic() + 20
        runs = tmp_path / "runs"
        while not runs.exists() or runs.read_text().count("x") < 2:
            assert time.monotonic() < deadline, "the loop never ran a second round"
            assert loop.poll() is None, "the loop ended after a passing round"
            time.sleep(0.1)
        loop.terminate()
        assert loop.wait(timeout=10) == 0
    finally:
        if loop.poll() is None:
            loop.kill()


@_needs_posix_sh
def test_sigterm_during_the_pause_ends_the_loop_at_once(tmp_path: Path) -> None:
    loop = _loop_with(tmp_path, _ROUND_EXITS, "0")
    try:
        _wait_for(tmp_path / "runs")
        time.sleep(0.5)                      # into the `sleep 60`
        started = time.monotonic()
        loop.terminate()
        assert loop.wait(timeout=10) == 0
        assert time.monotonic() - started < 5
    finally:
        if loop.poll() is None:
            loop.kill()


# ---------------------------------------------------------------------------
# Repository hygiene that CI depends on.
# ---------------------------------------------------------------------------


def test_the_workflow_actions_run_on_a_supported_node() -> None:
    """Node 20 reached end of life; runners force Node 24 and warn on every
    run. A warning nobody acts on is training to ignore warnings."""
    body = _read(".github/workflows/ci.yml")
    assert "actions/checkout@v4" not in body, "ci: checkout on Node 20"
    assert "actions/setup-python@v5" not in body, "ci: setup-python on Node 20"


def test_no_commit_in_this_repo_credits_a_stranger() -> None:
    """`<word>@users.noreply.github.com` resolves to the GitHub account whose
    username is <word>. A commit made with a descriptive word there is credited
    to a stranger, who is then listed as a contributor to this repository.

    Setting such an identity with `git config` and no --global writes it into
    .git/config, where it silently signs everything committed from that
    checkout afterwards. GitHub's own form carries the numeric user ID, which
    cannot collide with a human's login -- that is the whole reason for the
    number. Attribution is the record of who did the work, and putting someone
    else's name on a commit is wrong regardless of intent.

    Needs the full history: CI's default shallow clone fetches one commit, so
    ci.yml sets fetch-depth: 0 for this.
    """
    log = subprocess.run(
        ["git", "log", "--format=%H%x09%an <%ae>%x09%cn <%ce>"],
        cwd=str(ROOT), capture_output=True, text=True)
    if log.returncode != 0:
        pytest.skip("not a git checkout")

    noreply = re.compile(r"<(?P<local>[^<>@]+)@users\.noreply\.github\.com>")
    offenders = []
    for line in log.stdout.splitlines():
        sha, _, rest = line.partition("\t")
        for match in noreply.finditer(rest):
            if not re.fullmatch(r"\d+\+.+", match.group("local")):
                offenders.append(f"{sha[:10]}  {rest}")

    assert not offenders, (
        "commits credited to a GitHub account that may belong to someone "
        "else:\n  " + "\n  ".join(offenders) + "\n"
        "Fix the identity and rewrite, then `git config --local --unset "
        "user.email` so the next commit does not repeat it."
    )
