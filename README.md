# SRE Health Checker

**Engineering is the art of preventing the predictable.**

This is not a monitoring script. It is a monitoring microservice — the distinction
matters. A script does a job. A microservice has a contract. This one's contract is
simple: if something is wrong, you will know. Once. Not seventeen times.

---

## Project Vision

High-availability systems deserve high-availability monitoring. The irony of watching
a production API serve five-nines uptime while your health checker silently crashes
on a stale Python environment is not lost on this codebase.

This tool was built on a single premise: if you are going to monitor infrastructure,
do it without apology. No half-measures, no "good enough for now," no commented-out
TODOs that survive three release cycles. Every architectural decision in this project
traces back to one question: what happens at 3 AM when nobody is watching?

The answer should always be: the system handles it, logs it cleanly, and pages you
exactly once if your intervention is required.

---

## The Zero-Debt Architecture

The following decisions are not preferences. They are conclusions.

### Asynchronous Concurrency

Synchronous execution is a bottleneck we do not accept.

A sequential health checker that probes ten URLs with a five-second timeout takes
up to fifty seconds to complete a single run. That is not monitoring. That is a very
slow tour of your own infrastructure. By the time it finishes, the incident you were
meant to catch has already escalated.

This service uses `asyncio` with `aiohttp` to dispatch every probe simultaneously.
Wall-clock time becomes a function of your slowest target, not the sum of all of
them. A single shared `aiohttp.ClientSession` reuses the underlying TCP connector
and DNS resolution cache across all concurrent requests — the same reason you do not
instantiate a new database connection per query.

Time is the only resource you cannot recover. We do not spend it waiting in line.

### Atomic State Integrity

We do not tolerate corrupted state files.

The alert fatigue problem is well-understood: a service goes down at 2 AM, your
webhook fires every five minutes for six hours, and by morning your on-call engineer
has learned to ignore Discord notifications entirely. This is a monitoring failure
masquerading as an operational one.

State is persisted to `state.json` after every probe, and alerts fire only on
transitions: UP to DOWN, or DOWN to UP. The implementation uses two layers of
protection to ensure the system's memory is as reliable as its logic.

First, all concurrent writes pass through an `asyncio.Lock`. Because every coroutine
runs in a single OS thread, this is a zero-overhead cooperative yield point — not a
threading primitive — that prevents two probes from interleaving a read-modify-write
cycle on the shared state dictionary.

Second, all disk writes are atomic. State is serialized to a `.tmp` file and then
renamed into place. On POSIX systems, `rename(2)` is a single kernel syscall. A
process killed between the write and the rename leaves the previous `state.json`
intact. You do not get a zero-byte file. You do not get partial JSON. The state is
either the old version or the new version. There is no third option.

### Manual Retry Logic

We distinguish between a blip and a failure. Most monitoring tools do not.

A CDN under brief load returns a 503 for 800 milliseconds and self-heals. A Python
service with a misconfigured environment variable returns a 500 indefinitely. Firing
an identical alert for both is noise, and noise trains engineers to ignore alerts. The
consequences of that training are well-documented in post-mortems.

This service implements custom exponential backoff before declaring a target DOWN.
Up to three attempts are made per check cycle, with sleep intervals that grow
geometrically: two seconds, then four, capped at ten. The arithmetic is intentional.
Transient failures resolve within one retry window. Persistent failures do not.

The retry logic is hand-rolled rather than delegated to a library. This is a
deliberate choice. During an incident at 3 AM, the engineer reading this code needs
to understand exactly what it does without navigating third-party decorator internals.
The loop is fourteen lines. Every line is load-bearing.

Alerts are not a feature. They are a last resort. They should fire when, and only
when, a human is actually needed.

### Multi-Stage Docker Build

This is not about portability. Portability is a side effect. This is about security.

The production image is built in two stages. The builder stage installs dependencies
using pip, which may invoke a C compiler for native extensions. The runtime stage
copies only the compiled packages — it contains no pip, no compiler, no build
toolchain, and no mechanism for an attacker to install anything new. The attack
surface is a function of what is present. We keep that number low.

The process runs as a dedicated, unprivileged system user with no login shell and no
home directory. Root access inside a container is not a theoretical risk; it is a
practical one when combined with a misconfigured volume mount or a container runtime
vulnerability. We do not require root. We do not request it.

Application secrets and target configuration are passed in at runtime via environment
files and bind mounts. They do not appear in image layers. They do not appear in
`docker history`. They are not baked into an artifact that will be pushed to a
registry, scanned by a pipeline, cached by a CI runner, or rotated by a frustrated
platform engineer six months from now.

---

## Prerequisites

- Docker and Docker Compose
- A Discord Webhook URL (Server Settings > Integrations > Webhooks)
- Two minutes of focused attention

---

## Deployment

### 1. Configure secrets

```bash
cp .env.example .env
```

Open `.env` and replace the placeholder with your real Discord Webhook URL. Do not
commit this file. It is in `.gitignore` for a reason.

### 2. Configure targets

```bash
# Edit targets.yaml — add or remove URLs under the targets: key.
# No code changes required. The service reads this file at startup.
nano targets.yaml   # macOS / Linux
notepad targets.yaml   # Windows
code targets.yaml      # VS Code (any platform)
```

The file is plain text. Any editor works.

### 3. Deploy

```yaml
# docker-compose.yml
services:
  health-checker:
    build: .
    env_file: .env
    volumes:
      - ./targets.yaml:/app/targets.yaml:ro
      - ./state.json:/app/state.json
    restart: unless-stopped
    command: >
      sh -c "while true; do python main.py; sleep 60; done"
```

```bash
# Build the image and start the service.
docker compose up -d --build

# Confirm it is running.
docker compose ps

# Follow the log stream.
docker compose logs -f
```

To stop cleanly — the service handles SIGTERM and completes any in-flight checks
before exiting:

```bash
docker compose down
```

### 4. Verify the alerting pipeline

The default `targets.yaml` includes `https://httpbin.org/status/503` and a
non-existent domain. On first run, both will transition from an unknown state to
DOWN, and two Discord alerts will fire. This is not a misconfiguration. It is a
smoke test. Confirm the alerts arrive, then replace those entries with your own
infrastructure.

---

## File Structure

```
.
├── main.py              Core service logic — async orchestrator
├── targets.yaml         URL configuration — edit freely, no restarts required
├── Dockerfile           Multi-stage, non-root, health-checked production image
├── docker-compose.yml   Standard deployment manifest
├── .dockerignore        Build context exclusion list — image contains no dev artifacts
├── requirements.txt     Three direct dependencies, nothing extraneous
├── requirements-dev.txt Development dependencies — test runner and mocking layer
├── pytest.ini           Test runner configuration
├── tests/               30-test suite covering every probe, state, and alert path
├── .github/             CI: pytest + docker build on every push and pull request
├── .env                 Secret store — never committed
├── .env.example         Template — committed, contains no secrets
├── state.json           Runtime artifact — auto-created, bind-mounted
└── health_checker.log   Runtime artifact — structured log output
```

---

## Testing

We do not ship what we cannot prove works.

Thirty automated tests cover every component in isolation: target loading, state
transitions, probe logic, retry backoff intervals, Discord payload construction,
and the complete orchestration pipeline — including all four paths through the
alert decision logic.

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

The retry tests are worth noting specifically. A retry function that sleeps two
seconds instead of four passes every outcome-based test you write for it. It
also means your checker hammers a struggling server twice as fast as intended
during an outage — which is exactly the behavior that turns a recoverable
incident into a cascade. Every test that touches `_probe_with_backoff()` asserts
the exact sleep call count and precise interval values. We verify the intervals,
not just the results.

CI runs on every push and pull request to `main` via GitHub Actions: `pytest`
against Python 3.11 and `docker build` to verify the image builds cleanly. The
last thing you want to discover is that the tool watching your production
services broke three commits ago and nobody noticed. CI is not ceremony. It is
the check that prevents you from being the engineer who explains to stakeholders
why the monitor was down while the monitored service was also down.

---

## Observability

Every probe result is written to both stdout and `health_checker.log` in a structured
format with ISO-8601 UTC timestamps. The log level semantics are strict:

| Level    | Meaning                                                            |
|----------|--------------------------------------------------------------------|
| INFO     | Probe succeeded, or a state transition alert was dispatched        |
| WARNING  | A retry attempt failed; final verdict pending                      |
| ERROR    | All retries exhausted; target is DOWN, or a webhook call failed    |
| CRITICAL | Configuration error; the service cannot operate as configured      |

In a production environment, ship `health_checker.log` to your log aggregator of
choice. The format is structured for ingestion without pre-processing.

---

## Dependency Philosophy

The `requirements.txt` contains exactly three entries: `aiohttp`, `PyYAML`, and
`python-dotenv`. `tenacity` was deliberately removed when async retry logic was
introduced — a hand-rolled loop is preferable to a dependency when the logic fits
in a screen and the readers are on-call engineers, not library authors. Every
dependency in a production service is a liability. We carry only the ones that pay
rent.

---

## On Production Readiness

This version represents a complete and deployable solution against its current
requirements. The architecture has no known shortcuts: concurrency is genuine, state
management is safe under failure conditions, alerting is calibrated to require human
attention only when human attention is warranted, and the deployment surface is as
narrow as the toolchain permits.

The system is production-ready today. It is also honest about what tomorrow looks
like.

---

## Known Ceilings and the Path Through Them

This is a single-container microservice. Not a global-scale enterprise monitoring
suite. That distinction is intentional, not apologetic. The scope was defined
deliberately, the ceilings are fully understood, and the path to each one is already
mapped. The following is not a list of regrets. It is an engineering backlog,
prioritised by the conditions under which each item actually becomes necessary.

A word on philosophy before the list: scope inflation is how maintainable services
become undeployable platforms. Every item below is absent because the present
requirements do not justify it, not because the implementation is unknown. When the
business need arrives, so does the solution.

### Current Limitations

**Single-instance state via `state.json`**
The file-based state store is correct and safe for one running container. It is
wrong the moment you deploy a second. Two instances writing to the same file through
a shared volume will produce a race condition no lock can fix, because the lock does
not span processes. This is not a bug. It is a documented boundary condition.

**No built-in scheduling**
The service performs one check cycle and exits. Continuous monitoring requires an
external scheduler: a shell loop in the Docker Compose command, a Kubernetes CronJob,
or a host-level cron entry. This is intentional. Embedding a scheduler couples timing
policy to application logic. They are separate concerns and should remain that way.
However, it does mean the operator must own that configuration explicitly.

**Single alerting channel**
Alerts go to Discord. Full stop. There is no routing logic, no severity tiering, no
escalation path. A P0 database outage and a non-critical staging endpoint returning
503 produce identical notification behaviour. At this scale, that is acceptable. At
the next scale, it is not.

**HTTP-only probe logic**
The checker performs an HTTP GET and evaluates the response code. It does not
evaluate response body content, does not check TLS certificate expiry, does not
measure latency against a baseline, and does not perform DNS resolution time
analysis independently of the HTTP request. It answers one question: did the server
respond with 200? This is the right first question. It is not the only question.

**No authentication support for protected endpoints**
Targets are assumed to be publicly accessible. Endpoints that require an
`Authorization` header, an API key, a client certificate, or a session cookie are
not supported in the current target schema. Monitoring internal services behind
authentication is a common production requirement that this version defers.

**`targets.yaml` requires a process restart to take effect**
Configuration is loaded once at startup. Adding or removing a target requires
stopping and restarting the container. There is no hot-reload, no inotify watcher,
no configuration API. For a single-operator deployment on a fixed target set, this
is a non-issue. For a team managing a growing service catalogue, it becomes friction.

**No multi-region probe capability**
All probes originate from wherever the container is running. A service that is
degraded in one AWS region but healthy in another will appear healthy to a checker
running in the healthy region. Distributed probe execution — running the same checks
from multiple geographic vantage points — requires infrastructure this version
intentionally does not include.

**Log output is unstructured text**
The log format is human-readable and informative. It is not machine-parseable JSON.
Shipping these logs to a structured aggregator like Loki, Datadog, or CloudWatch
Logs Insights requires either a parsing rule on the ingestion side or a format
change on this side. Both are straightforward. Neither is done yet.

---

### The Path Forward

Each item above has a known resolution. The following are not hypothetical
directions — they are defined next steps, ordered by the scale trigger that makes
them necessary.

**When you need a second instance: replace `state.json` with Redis**
`StateManager` has a clean interface: `set_up()`, `set_down()`, and `_write_sync()`.
Replacing the JSON file backend with a Redis client requires changes in exactly one
class. The `asyncio.Lock` becomes a Redis distributed lock via `aioredis`. The rest
of the codebase is unchanged. This is the first ceiling to break, and it is a
two-hour task, not a rewrite.

**When you need scheduling ownership: adopt APScheduler or a Kubernetes CronJob**
For container-native deployments, a Kubernetes CronJob is the correct primitive — it
handles retries, history, and concurrency policy natively. For non-Kubernetes
environments, `APScheduler` with an `AsyncIOScheduler` integrates directly into the
existing event loop without threading concerns. Either path requires fewer than
twenty lines of new code.

**When you need alert routing: introduce a notification abstraction layer**
The `send_discord_alert()` function is the single point where all alert output
exits the system. Replacing it with a `Notifier` base class and concrete
implementations for Discord, PagerDuty, Slack, and email is a standard strategy
pattern application. Severity tiers and routing rules live in the YAML configuration
alongside the targets.

**When you need richer probe results: extend `_probe_once()`**
The probe function returns nothing on success and raises on failure. Extending it to
return a `ProbeResult` dataclass — carrying status code, response time, TLS expiry
days remaining, and body hash — adds observability without changing the retry or
state logic that wraps it. The check pipeline stays identical; only the data it
carries changes.

**When you need authenticated targets: extend the target schema**
The YAML schema accepts a URL string today. Accepting a target object with optional
`headers`, `auth_type`, and `secret_ref` fields requires a schema version bump and
a small change to how `_probe_once()` constructs the request. The secret values
themselves should reference environment variables, not be stored in the YAML file.

**When you need hot-reload: add a SIGHUP handler**
The signal handling infrastructure is already in place. Adding `SIGHUP` as a
reload trigger that calls `load_targets()` and updates the active target list
without restarting the process is an afternoon task. The harder part is deciding
what to do with in-progress checks against targets that were just removed. The
correct answer is: let them finish, then stop scheduling them.

**When you need geographic distribution: move to a probe agent model**
This is the architectural shift that changes the service's fundamental shape. A
central coordinator distributes probe tasks to lightweight agents deployed in each
target region. Agents report results back to the coordinator, which owns state and
alerting. The application-level logic does not change. The deployment topology does.
At this point you are building a distributed system, and it should be treated as one.

**When you need structured logs: switch the formatter**
Python's `logging` module accepts custom formatters. A twelve-line `JsonFormatter`
class that serialises each `LogRecord` to a JSON object is the entire change. Every
downstream log consumer — Loki, Datadog, Splunk, CloudWatch — benefits immediately.
This is the lowest-effort item on the list and the one most likely to be skipped
until a production incident makes log parsing painful enough to motivate it. Do not
wait for the incident.

---

## License

MIT. Use it, adapt it, deploy it. If it prevents one preventable outage, it has
done its job.
