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
- Optionally a Slack Incoming Webhook — alerts go to both at once, see
  "Multi-channel alerting"
- Two minutes of focused attention

---

## Deployment

### 1. Configure secrets

```bash
cp .env.example .env
```

Open `.env` and replace the placeholder with your real Discord Webhook URL. Add
`SLACK_WEBHOOK_URL` too if you want the second channel — every alert goes to every
configured channel. Do not commit this file. It is in `.gitignore` for a reason.

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
    environment:
      # state.json needs a directory mount, not a single-file one — see the
      # comment in the repo's docker-compose.yml for why.
      - STATE_FILE_PATH=/app/data/state.json
    volumes:
      - ./targets.yaml:/app/targets.yaml:ro
      - ./data:/app/data
      - ./history.db:/app/history.db
    restart: unless-stopped
    command: >
      sh -c "while true; do python main.py || exit 1; sleep 60; done"
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
DOWN, and two alerts will fire on every configured channel. This is not a
misconfiguration. It is a
smoke test. Confirm the alerts arrive, then replace those entries with your own
infrastructure.

---

## File Structure

```
.
├── main.py              Core service logic — async orchestrator
├── diagnostics.py       Latency/BDP diagnostic engine — phase timings + findings
├── history.py           SQLite historical persistence — isolated from the alert path
├── notifiers.py         Multi-channel alert delivery — Discord + Slack, in parallel
├── api.py               Read-only JSON endpoint — separate process, never writes
├── dashboard.py         HTML view over that JSON — no framework, no JS dependency
├── targets.yaml         URL configuration — edit freely, no restarts required
├── Dockerfile           Multi-stage, non-root, health-checked production image
├── docker-compose.yml   Standard deployment manifest
├── .dockerignore        Build context exclusion list — image contains no dev artifacts
├── requirements.txt     Three direct dependencies, nothing extraneous
├── requirements-dev.txt Development dependencies — test runner and mocking layer
├── pytest.ini           Test runner configuration
├── tests/               311-test suite covering probes, state, alerts, diagnostics, history
├── .github/             CI: pytest, then docker build + run a real health-check cycle
├── .env                 Secret store — never committed
├── .env.example         Template — committed, contains no secrets
├── state.json           Runtime artifact — auto-created, bind-mounted
├── history.db           Runtime artifact — auto-created, pruned by HISTORY_RETENTION_DAYS
└── health_checker.log   Runtime artifact — structured, rotates at 10 MiB × 5 backups
```

---

## Testing

We do not ship what we cannot prove works.

311 automated tests cover every component in isolation: target loading (including
`${VAR_NAME}` secret resolution), state transitions, probe logic, retry backoff
intervals, per-channel payload construction and delivery, the complete
orchestration pipeline —
including all four paths through the alert decision logic — the full diagnostic
engine, where every rule is tested on both sides of its threshold, and the
historical persistence layer, including forcing internal writes to fail to prove
the isolation boundary holds rather than assuming it does. A rule that fires is
only half a test; a rule that fails to stay quiet just below its trigger is how a
monitor learns to cry wolf, and a muted monitor is worse than none.

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
against Python 3.11, then a Docker job that builds the image, runs it to confirm
it actually starts (`import main`), and executes one real end-to-end health-check
cycle inside a container. Building an image without ever running it is how a
`ModuleNotFoundError` at container startup once survived twelve phases undetected
— CI now runs the thing, not just builds it. The last thing you want to discover
is that the tool watching your production services broke three commits ago and
nobody noticed. CI is not ceremony. It is the check that prevents you from being
the engineer who explains to stakeholders why the
monitor was down while the monitored service was also down.

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

`health_checker.log` rotates automatically at 10 MiB, keeping 5 backups (50 MiB cap).
The one service whose job is to notice disk space filling up should not be the one
that fills it — an unrotated log on a long-lived host is a slow-motion version of the
exact incident it exists to page you about.

---

## Latency & BDP Diagnostics

"UP" and "DOWN" are the first question. They are not the last one.

A service that answers 200 in 2.8 seconds is UP by every binary check ever written,
and it is also the reason a customer closed the tab. "It's up" is not an answer to
"why is it slow" — and "why is it slow" is the question that actually pages a human.
This service answers it. Every successful probe is decomposed into its phases — DNS
resolution, TCP round trip, TLS handshake, server processing, and body transfer —
and each phase is run through a diagnostic engine that names the bottleneck and the
fix.

The output is not "latency is high." The output is one of these:

| Finding | What it means | What it tells you to do |
|---|---|---|
| `HIGH_RTT_NEEDS_EDGE` | The round trip alone is >100 ms | Distance, not tuning. Put a CDN/edge closer to the probe. No sysctl shortens the speed of light. |
| `SLOW_DNS` | The resolver spent >200 ms | The resolver, not the target. Run a caching resolver or point at a faster upstream. |
| `TLS_HANDSHAKE_OVERHEAD` | The handshake cost more round trips than TLS 1.3 needs | Enable TLS 1.3, session resumption, OCSP stapling. |
| `SLOW_BACKEND` | Time-to-first-byte minus the network is >500 ms | The network delivered; the server sat on it. Profile the handler, not the pipe. |
| `WINDOW_LIMITED` | A large transfer never filled a 64 KB window | Throughput is window-limited, not pipe-limited. Enable window scaling / raise receive buffers. |
| `CERT_EXPIRING` | The TLS certificate expires in <30 days (<7 = critical) | Renew now. This outage has a scheduled date and it is printed on the certificate. |
| `HTTP2_NOT_SUPPORTED` | The server offers only HTTP/1.1 on a >100 ms path | Enable HTTP/2 (h2). On a high-RTT path, HTTP/1.1 head-of-line blocking serialises what h2 would multiplex. |

Every diagnosis carries the numbers that triggered it and the specific remediation.
It lands in two places: the structured log (one line per finding, at the severity the
finding warrants) and `state.json`, under a `diagnostics` block per URL carrying the
full phase breakdown, the derived BDP metrics, and the findings list. The 1 AM
engineer reads the verdict, not the raw timings.

### DEGRADED: up, and hurting

A 200 is not the same as healthy, so the state machine has three states, not two.

When a probe succeeds but a performance finding fired — high RTT, slow DNS, TLS
overhead, a window-limited transfer, a slow backend — or time-to-first-byte crossed
1.5 s, the target is **DEGRADED**, not UP. It gets an amber Discord alert on the
transition, distinct from the red DOWN alert, because paging a slow-but-alive service
in the same red as a dead one trains the on-call to distrust red. The colour carries
the triage: green recovered, amber degraded, red down. `CERT_EXPIRING` and
`HTTP2_NOT_SUPPORTED` deliberately do not trigger DEGRADED — an expiring cert is a
scheduled *future* outage and a missing h2 is an optimization, neither is a symptom
the user is feeling right now.

### Knowing whether the server speaks HTTP/2

aiohttp only ever speaks HTTP/1.1, so it can never tell you whether the server would
have spoken HTTP/2. The out-of-band sampler can: it offers both `h2` and `http/1.1`
in the ALPN extension of its own TLS handshake and records what the server picked.
That negotiated protocol lands in `state.json` as `alpn_protocol` / `h2_supported`,
and drives the `HTTP2_NOT_SUPPORTED` hint on slow paths. This costs nothing extra —
it rides on the same handshake the sampler already performs to time the TLS phase.

### On measurement honesty

The bandwidth-delay product needs two inputs, and only one of them is free.

RTT is sampled cleanly with a dedicated raw TCP connect — one round trip, no payload.
Bandwidth is not free: a health endpoint returning 5 KB finishes inside TCP slow-start
and tells you nothing about the path's capacity. Reporting a BDP off that sample would
be inventing a number and printing it with a straight face. So every bandwidth-derived
metric carries an explicit `bandwidth_confidence` field. It reads `HIGH` only when the
response body cleared 256 KB — enough to exercise the window. Below that it reads
`INSUFFICIENT_SAMPLE`, and no bandwidth verdict (`WINDOW_LIMITED`) is allowed to fire.
A diagnosis from a 5 KB sample is not a diagnosis. It is noise wearing a lab coat, and
this tool does not wear costumes.

**This measurement runs entirely on the probe you already pay for.** No synthetic load
against the monitored server, no new dependency. RTT, TLS time, and ALPN/h2 come from
an out-of-band sampler — one plain TCP connect for RTT and, for HTTPS, one controlled
handshake for TLS time and the negotiated protocol. That is one extra handshake for
HTTP targets and two for HTTPS per target per run: SYN/FIN pairs, negligible.

### Boundary condition: the RTT and TLS samples come from different connects

The sampler's RTT (a plain TCP connect) and its TLS time (a separate TLS handshake)
are two connections opened moments apart. On a jittery path they will not always
agree — the RTT connect can read 281 ms while the TLS connect completes faster. When
that makes the derived TLS handshake time negative, it is reported as `null`, never as
a fabricated positive. Both figures now come from the same sampler back-to-back, so
they agree far more often than the old aiohttp-vs-sampler derivation did — but they
are still not one connection. The exit, when single-connection precision is required,
is a raw socket probe that performs the TLS handshake inline and times every phase on
one connection, at the cost of not reusing aiohttp's session.

### Boundary condition: the BDP estimate crosses two connections too

`effective_window_bytes` multiplies `goodput_bps` (measured on the probe's own
aiohttp connection, downloading the real response body) by `rtt_ms` (measured on the
out-of-band sampler's separate connection to the same host). This is the same class
of approximation as the RTT/TLS boundary condition above — two connections, not one —
and it is why every bandwidth-derived number already ships with an explicit
`bandwidth_confidence` field rather than being presented as measured fact. Treat the
BDP estimate as directional (good enough to say "this path is window-limited," not
precise enough to size a `tcp_rmem` value to the byte). The exit is the same raw
single-connection socket probe named above — one connection carrying the request,
the RTT sample, and the transfer all on the same TCP stream.

---

## Historical Persistence

`state.json` remembers exactly one thing per target: what it looks like right now.
That answers "is it up," which is necessary and not sufficient. It cannot answer
"how many minutes was it down last month," "did my p95 latency get worse this week,"
or "am I meeting my uptime SLA" — every question that turns a monitor into something
you can report on instead of something you only stare at during an incident.

Every run now writes a batch to `history.db`, a SQLite file living beside `state.json`.
Two tables: `probe_results` (one row per target per run — status, error, and the full
latency/BDP phase breakdown) and `probe_findings` (one row per diagnostic finding,
linked back to its probe). No new service, no new port, no new failure mode to
operate — it is a file, and the backup procedure is the same one already documented
for `state.json`: copy it.

```sql
-- p50/p95/p99 RTT and average TTFB per target, over everything history.db has kept.
-- CUME_DIST (rank/n) with MIN, not PERCENT_RANK ((rank-1)/(n-1)) with MAX: under
-- PERCENT_RANK the highest row always evaluates to exactly 1.0, so `<= 0.95` can
-- never include the maximum and every percentile reads lower than it is. This is
-- the same query --report runs, so the two agree by construction.
SELECT url, COUNT(*) n, AVG(ttfb_ms) avg_ttfb,
       MIN(CASE WHEN cd >= 0.50 THEN rtt_ms END) p50_rtt,
       MIN(CASE WHEN cd >= 0.95 THEN rtt_ms END) p95_rtt,
       MIN(CASE WHEN cd >= 0.99 THEN rtt_ms END) p99_rtt
FROM (SELECT url, ttfb_ms, rtt_ms,
             CUME_DIST() OVER (PARTITION BY url ORDER BY rtt_ms) cd
      FROM probe_results)
GROUP BY url;

-- Uptime % per target — DEGRADED counts as up (it is "up and hurting", not down;
-- see "Reading the history: --report" below for why)
SELECT url, 100.0 * SUM(status != 'DOWN') / COUNT(*) AS uptime_pct
FROM probe_results GROUP BY url;
```

### Reading the history: `--report`

```bash
python main.py --report
```

Prints a table — uptime % and TTFB p50/p95 per target, for every window (1d/7d/30d)
that fits inside `HISTORY_RETENTION_DAYS` — and exits. It never probes anything: it
is a pure read over `history.db`, safe to run as often as you want, including from
a second, unrelated cron job that only builds reports.

**DEGRADED counts as up.** Uptime is an availability question; a DEGRADED target
answered every request, just slowly. Folding it into "down" would make "up and
hurting" indistinguishable from "unreachable" — two different signals collapsed
into one number. Latency percentiles are where "hurting" actually shows up.

**A window longer than the configured retention is never shown.** If
`HISTORY_RETENTION_DAYS=7`, there is no `Up 30d` column, because there is no 30
days of data to compute it from — the alternative is a column that quietly means
something other than what its header says.

**A target with no probes yet in the window reads `no data`, never `0%`.** Zero
percent is a claim that the target was down for the entire window; a target
nobody has probed yet has made no such claim.

### Isolation boundary: history can fail; alerting cannot

History recording runs strictly after `state.json` has been updated and the Discord
alert (if any) has already been sent for the run. Every method on `HistoryRecorder`
catches its own exceptions, logs them, and returns — it never raises into
`run_health_checks`. A full disk, a corrupted `history.db`, or a stuck SQLite lock
degrades the history feature — a run's worth of rows goes unrecorded — and never
touches the part of this service whose job is to page a human. Losing a history row
is a gap in a chart. Losing an alert is an incident nobody heard about.

### Retention

`history.db` is pruned every run: rows older than `HISTORY_RETENTION_DAYS` (default
30) are deleted. Left unbounded, history grows without limit in the one service whose
job is to notice things filling up — that failure mode does not get to happen here.
Measured, not estimated: **~213 bytes per probe row** (schema above). At the
default 30-day retention:

| Targets | Rows/day | Disk/day | Disk at 30 days | Disk at 1 year (no retention) |
|---|---|---|---|---|
| 6       | 8,640     | 1.8 MB  | 55 MB  | 0.67 GB |
| 50      | 72,000    | 15.3 MB | 460 MB | 5.59 GB |

Raise `HISTORY_RETENTION_DAYS` if you need a longer window and have the disk for it;
lower it if you are running many targets on a constrained volume.

---

## Secret Redaction (`expect_substring`)

`expect_substring` is a content assertion, and content assertions are sometimes
secrets: a private health-check token nobody outside the team should be able to
guess from a log line. Written as a literal in `targets.yaml`, that string is
already public — the file is committed to git, forever, regardless of what
anything downstream does with it.

```yaml
- url: https://api.yourcompany.com/private-health
  expect_substring: ${HEALTH_TOKEN}   # resolved from .env, never logged
```

A value written as `${VAR_NAME}` is resolved from `.env` at load time and never
appears again — every sink a failed check can reach (console, `health_checker.log`,
the Discord alert, `state.json`, `history.db`) shows `${VAR_NAME} (from env)`
instead of the real value. Redaction happens once, at the single point where the
failure message is built, so all five sinks inherit it for free; a plain literal
is untouched, since redacting only some of the sinks for a value that is already
public in git would be security theater, not security. Referencing a variable
that isn't set in `.env` fails the run at startup with a clear message — a probe
that silently never matches because its secret failed to resolve is a worse
failure mode than refusing to start.

---

## Certificate expiry: the outage with a date on it

Every other failure this tool watches for is a surprise. A TLS certificate is
not: it announces its own expiry date months ahead, and when it arrives, every
client hard-fails the handshake at once. There is no degradation curve, no
partial outage — the service is fine, and then it is not.

vigil-sre has measured `tls_cert_days_left` on every HTTPS probe since the
diagnostics engine existed. What it does now is *tell you*: an amber alert at
30 days, a red one at 7.

**It does not change the target's status.** A service with a certificate
expiring in 20 days is UP, not DEGRADED — folding a scheduled future outage
into a measurement of present health would corrupt both, and would quietly
distort the uptime figures in `--report`.

**It alerts twice per certificate, not 43,200 times.** A certificate sits
below the 30-day threshold for a month, which at one run per minute is 43,200
checks. Two of them alert. The suppression state is keyed on the threshold
crossed rather than the days remaining — days change daily and are useless for
"have I already said this" — and it survives both status changes and process
restarts, because the alerting decision is worthless if it forgets itself
every time the service recovers or the container restarts.

**A renewed certificate resets the memory.** Without that, a renewal would
leave the old threshold recorded forever, and the *next* expiry would skip its
30-day warning — the monitor going quiet exactly at the notice that gives you
the most room to act.

---

## The dashboard

```bash
docker compose up -d          # probe + dashboard, both
python api.py                 # or standalone, without Docker
                              # then open http://127.0.0.1:8787
```

Compose runs it as its own service, not a thread inside the probe: the probe is
not a long-lived process — it runs one cycle and exits, and the loop restarts it
every 60 seconds — so a server living in there would die every minute. The
service mounts the state directory and the database **read-only**, and publishes
its port to `127.0.0.1` on the host rather than to every interface. This endpoint
has no authentication and it serves the operational map: which targets exist,
when they fail, with what error. Publishing that is a decision, and `8787:8787`
is how you back into it by accident.

One page. No navigation — if it needed a menu it would already be bigger than
this product is. A dense list rather than a grid of cards, because a grid looks
empty at six targets and unmanageable at fifty, while a list survives both.

**The freshness bar sits above everything, before a single target.** Almost no
monitoring dashboard tells you its own data is old: it shows green and you
assume green means now. When the probe process dies, this page would keep
answering 200 with data that is perfectly well-formed and simply stale. So past
two run cycles the bar turns amber, and past five it turns red **and dims every
row below it** — green that might be hours old has no business looking
reassuring. That behaviour is the visual counterpart of the dead-man's switch,
and it is the one thing here not borrowed from anyone else.

That bar has one blind spot, and the header covers it: **the bar only updates
when a poll succeeds.** If the server dies while a tab is open, the page would
freeze mid-sentence and go on claiming the data is thirty seconds old — the
freshness mechanism disabled by exactly the kind of failure it exists to catch.
So a failed poll writes its own line in the header, outside the fragment the
refresh replaces, and counts how many have failed in a row.

**Three states, and colour is never the only channel.** UP, DEGRADED and DOWN
carry the same palette Discord and Slack already use, so the reflex you train in
one place works in the others. But roughly 8% of men have a colour vision
deficiency and red/green is exactly the pair that fails, so every state also
carries its own glyph and its own word.

Clicking a row expands it — that is a native `<details>` element, not
JavaScript. It is keyboard-accessible for free.

**No framework, and no JavaScript dependency.** The roadmap picked FastAPI +
Jinja2 + HTMX while this was still hypothetical. By the time it was built, it
was two routes and two templates: FastAPI would have bought routing and
validation nothing here needs while dragging in four more packages, and HTMX
would have bought interactivity that came to ten lines of `fetch`. Three
dependencies. The exit is written down: the day this grows
filtering, silencing or acknowledgement, both earn their place.

---

## Read-only JSON endpoint

`--report` answers questions in a terminal. This answers them over HTTP, so
something else can ask — a dashboard, a status page, a script that does not want
to parse a table.

```bash
python api.py                          # 127.0.0.1:8787

curl localhost:8787/api/status         # current state per target + staleness
curl localhost:8787/api/history?window=7d   # uptime % and TTFB p50/p95
```

**It is a separate process, and that was not a preference.** `main.py` runs one
cycle and exits — an HTTP server inside it would die every sixty seconds. The
welcome consequence is that this endpoint cannot slow down, block, or crash a
probe run, because it is not in one.

**It cannot write, and that is enforced by SQLite rather than by intent.** Every
connection opens with `mode=ro`, so an accidental `INSERT` fails with *attempt to
write a readonly database*. It runs beside a live writer; "should not write" is a
weaker guarantee than "cannot".

**It binds to loopback.** The list of monitored URLs, when they fail, and with
what error is an operational map. Publishing it should take a deliberate act, so
`API_HOST` exists but you have to write it — and doing so logs a warning, because
there is no authentication in front of it yet.

**`stale_seconds` is the field to watch.** If the probe process dies, this
endpoint keeps answering `200` with data that is perfectly well-formed and simply
old — a green dashboard fed by yesterday. That number is also emitted as
`api_stale_state_seconds` in the logs, because a value only visible to whoever
happens to look is no defence against nobody looking.

The figures come from the same functions `--report` uses, and the test suite
asserts the two agree with *each other* rather than each with a fixture. A CLI and
an API deriving the same number from the same rows and printing different answers
is the kind of bug where both sides pass their own tests.

---

## Who watches the watchman

Every failure mode above is one this service can report on itself. There is
exactly one it cannot: its own death. If the process crashes, the container
enters a restart loop, or the host powers off, nobody is left to send the
alert — and total silence is indistinguishable from everything being fine.
That is the failure mode this project has been bitten by more than once, and
it is structural: no healthcheck inside a box
survives the box going away.

```bash
# .env — point at any watchdog that expects a ping on a schedule
HEARTBEAT_URL=https://hc-ping.com/your-uuid-here
```

vigil-sre pings once per **completed run**. The watchdog alerts when the
pings stop.

**It pings whether or not targets are down.** This watches the monitor, not
the targets — the same distinction that makes a bare `python main.py` exit 0
while a target is down. Gating the heartbeat on target health would make a
genuine outage also trip the watchdog, paging *"your monitor is dead"* on top
of the real incident, at the exact moment the on-call is least able to tell
the two apart.

**The ping is the last thing a run does**, after probes, alerts, state and
history. Pinging any earlier would tell the watchdog "alive and well" about a
cycle that had not finished — the one lie a dead-man's switch must never be
able to tell.

**A failed ping is logged and forgotten, never retried.** The next run is 60
seconds away and watchdog grace periods are minutes, so a single missed ping
self-heals long before it matters. A watchdog that is down cannot take the
real monitoring with it.

Set the watchdog's expected period to your run interval (60s in the shipped
`docker-compose.yml`) with a grace period of a few cycles.

**Leaving `HEARTBEAT_URL` unset is allowed, and it logs a warning on every
run.** That is deliberate. Disabling the watchdog is a legitimate choice, but
from inside the process a deliberate choice and a forgotten variable look
exactly the same — and what goes unprotected is the one failure this service
cannot report on itself, because a dead monitor's only symptom is silence. The
warning is what makes the gap noticeable at all. If you meant it, the line
costs you one row per run; if you did not, it is the only thing standing
between you and a monitor that died three weeks ago.

---

## Multi-channel alerting

An alert can be delivered perfectly and still fail. The POST returns 204, Discord
accepts it, the message is in the channel — and the on-call never sees it, because
it landed under forty other notifications, or the phone deferred it, or a
notification setting nobody remembers configuring silenced that app. Nothing failed.
Delivery worked and attention did not, and no retry policy has anything to retry.

So every alert goes to every configured channel, simultaneously:

```bash
# .env — configure one, or both
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

**There are no routing rules, and that is the design.** A rule that sends an alert
to only one place puts back exactly the single point of failure the second channel
was added to remove. The duplication is the feature. An unconfigured channel is
skipped silently; configuring none means every alert is lost, and vigil-sre says so
loudly rather than staying quiet about it.

Delivery is parallel, not sequential, and the reason is arithmetic rather than
elegance. Worst case per channel is three attempts at a 5 s timeout plus two 30 s
capped `Retry-After` sleeps: 75 s. Two channels sequentially is 150 s against a
60 s run budget — a guaranteed overrun. In parallel it stays at 75 s, which is what
one channel already cost.

Channels are isolated from each other and from the probe. A channel that is down,
rate-limited, or outright buggy cannot delay a healthy one and cannot fail the
check that triggered the alert. Adding a channel — PagerDuty, email — is one
subclass of `Notifier` supplying three things: its environment variable, its
payload shape, and which HTTP status means success. That last one is not a
formality: Discord answers 204 and Slack answers 200, and a channel that assumed
its sibling's success code would report every successful delivery as a permanent
failure.

The metric worth alerting on is `alerts_lost_all_channels_total`. Per-channel
losses are diagnosis; that one means an alert was generated and no human is going
to hear about it.

### A lost alert is retried, not forgotten

Alerts fire on transitions, which means a target that goes DOWN is announced
once. That is the right behaviour and it had a hole in it: if every channel was
unreachable during the exact cycle the target went down, the status still
advanced, the next run read "no change, already alerted", and the alert was gone
for good. The outage and the silence about it had the same cause, so nothing
else in the system could catch it.

Delivery is now recorded alongside the status. If an alert reached nobody, the
target is marked `alert_pending` and the **next run alerts again**, sixty
seconds later, until a channel accepts it. Then it goes quiet. Both halves
matter: without the retry an outage can pass unannounced, and without the
disarm a channel coming back would re-alert every minute for as long as the
target stayed down.

Two consequences worth knowing:

- **The process exits non-zero when an alert reached nobody**, with or without
  `--strict`. Targets being down is normal operation; an alert nobody received
  is a failure of the monitor, and it is invisible by construction — so
  whatever supervises the process needs to see it.
- **A channel that fails repeatedly within one run stops being retried for the
  rest of that run.** With a channel down, every target otherwise pays the full
  retry policy on its own; six targets absorb that inside the run budget, thirty
  do not, and then probing slows down because *alerting* is slow. Breaking is
  per-channel, so a dead Discord never delays a healthy Slack, and the breaker
  resets every run — a single bad minute must not mute a channel for good.

### Tune the thresholds to the path you are measuring

`degraded_rtt_ms` defaults to 100 ms, which assumes the probe sits near what it
watches. Over an ordinary internet link it does not. Measured against the
targets shipped here, round-trip times run 109–484 ms depending on the hour, so
under the default GitHub and httpbin sat permanently DEGRADED for having normal
latency — a claim about the network the probe runs on, not about them.

Chronic false amber is how an operator learns to ignore amber. Set
`degraded_rtt_ms` per target to something that clears your observed range with
headroom; the shipped `targets.yaml` uses 400 ms and still catches genuine
spikes above it.

---

## Dependency Philosophy

The `requirements.txt` contains exactly three entries: `aiohttp`, `PyYAML`, and
`python-dotenv`. `tenacity` was deliberately removed when async retry logic was
introduced — a hand-rolled loop is preferable to a dependency when the logic fits
in a screen and the readers are on-call engineers, not library authors. Every
dependency in a production service is a liability. We carry only the ones that pay
rent. Historical persistence (`history.py`) is the largest feature added since v3
and it added zero: `sqlite3` and `asyncio.to_thread` are both standard library — an
async driver like `aiosqlite` would buy back a sub-millisecond duration at the cost
of a permanent dependency, which is not a trade this project makes.

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

**No severity tiering or escalation path**
Every alert reaches every configured channel with the same urgency. A P0 database
outage and a non-critical staging endpoint returning 503 produce identical
notification behaviour. At this scale, that is acceptable. At the next scale, it is
not.

Note what is *not* on this list any more: the single alerting channel. Alerts now go
to Discord and Slack in parallel, and the absence of routing between them is a
decision, not a gap — see "Multi-channel alerting" above.

**Probe logic does not evaluate response body content**
The checker performs an HTTP GET, evaluates the response code, and — as of the
latency/BDP diagnostics — decomposes the connection into DNS, RTT, TLS, server, and
transfer phases, checks TLS certificate expiry, and flags the bottleneck. What it
still does not do is inspect the response *body* for correctness: a server returning
200 with an error page or an empty JSON object reads as healthy. Body-content
assertions (expected string present, schema valid) are the next question this probe
does not yet answer.

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

**Notification abstraction layer: done — `notifiers.py`**
This ceiling has been broken. A `Notifier` base class owns the delivery policy and
concrete channels supply only what differs: the environment variable holding their
webhook, their payload shape, and which HTTP status means success. Adding PagerDuty
or email is one subclass and one entry in `ALL_NOTIFIERS`.

What was *not* built is routing, and that is deliberate — see "Multi-channel
alerting". Severity tiers remain a real ceiling: when a P0 must escalate differently
from a staging blip, tiers belong in the YAML alongside the targets, and the
`AlertKind` enum is where that distinction would attach.

**Richer probe results: done — `_probe_once()` returns `ProbePhases`**
This ceiling has been broken. `_probe_once()` now returns a `ProbePhases` dataclass
carrying status code, per-phase timings, body size, goodput, and TLS expiry days,
which the diagnostic engine turns into findings. The retry and state logic that wraps
it did not change — only the data it carries did, exactly as planned. The remaining
extension on this axis is body-content assertion (see the probe-body limitation above).

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
