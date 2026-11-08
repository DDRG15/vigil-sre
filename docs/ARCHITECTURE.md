# Architecture

Two diagrams: what runs on every round, and what builds, ships and analyses it.
Every node is a file, a function or a store that exists in this repository; every
arrow is an import, a read, a write or a request that the code performs.

Dashed arrows are a fallback, an opt-in profile or reused code; solid arrows are
reads, writes, calls and requests.

## 1. Runtime

Two processes share the `data/` volume and nothing else: neither imports the other.
`targetstore.py` is stdlib-only so both can hold it. The monitor runs one round and
exits; compose (or the Kubernetes container loop) starts the next one 60 seconds later.
The API is the only process that writes the target list.

```mermaid
flowchart TB
    subgraph config["Configuration"]
        seed["targets.yaml<br/>hand-edited seed"]
        env[".env<br/>webhooks · HEARTBEAT_URL · API_WRITE_TOKEN"]
    end

    subgraph shared["data/ — shared volume"]
        store["targets.json<br/>target store"]
        state["state.json<br/>last status per URL"]
        histdb["history/history.db<br/>SQLite, pruned by retention"]
    end

    tstore["targetstore.py<br/>validation + atomic write, stdlib only"]

    subgraph monitor["main.py — one round, restarted every 60 s"]
        load["load_targets()<br/>store first, seed until the first save"]
        check["check_url()<br/>concurrent aiohttp probes + backoff"]
        diag["diagnostics.py<br/>DNS · TCP · TLS · server phases"]
        statemgr["StateManager<br/>atomic tmp-then-replace"]
        gate["alert gate<br/>maintenance · reminders · owed alerts · trend · cert expiry"]
        notif["notifiers.py<br/>Discord + Slack in parallel"]
        recorder["history.py<br/>HistoryRecorder, never raises"]
        beat["heartbeat<br/>last act of a complete round"]
        report["main.py --report<br/>terminal summary"]
    end

    subgraph apiproc["api.py — long-lived, 127.0.0.1:8787"]
        http["HTTP server<br/>/api/status · /api/history · /api/targets"]
        dash["dashboard.py<br/>overview + target editor"]
        browse["history_browser.py<br/>history pages + CSV export"]
    end

    subgraph clients["Operators and agents"]
        browser["Browser"]
        ctl["targetctl.py<br/>list · add · remove · validate"]
        mcp["mcp_server.py<br/>read-only MCP over stdio"]
    end

    endpoints["Monitored endpoints"]
    channels["Discord · Slack webhooks"]
    watchdog["External watchdog<br/>healthchecks"]

    store -->|"read first"| load
    seed -.->|"until the first save"| load
    load -->|"validates entries"| tstore
    load --> check
    check -->|"HTTP(S) probe"| endpoints
    check --> diag
    diag -->|"status + findings"| statemgr
    statemgr -->|"writes"| state
    statemgr -->|"on a transition"| gate
    check -->|"round outcomes"| recorder
    recorder -->|"writes"| histdb
    histdb -->|"recent vs previous p95"| gate
    gate --> notif
    env -->|"webhook URLs"| notif
    notif --> channels
    notif -->|"undelivered = still owed"| statemgr
    env -->|"HEARTBEAT_URL"| beat
    state -->|"withheld if an alert reached nobody"| beat
    beat -->|"ping"| watchdog
    histdb -->|"read-only"| report

    browser --> http
    ctl -->|"HTTP, same API as the editor"| http
    ctl -->|"validates locally"| tstore
    http --> dash
    http --> browse
    env -->|"API_WRITE_TOKEN"| http
    http -->|"PUT, token-gated"| tstore
    tstore -->|"atomic write"| store
    state -->|"read"| http
    histdb -->|"read-only"| http
    histdb -->|"read-only"| browse

    mcp -.->|"reuses api.py read functions"| http
    state -->|"read"| mcp
    histdb -->|"read-only"| mcp
    env -->|"which channels are set"| mcp
```

## 2. Build, deploy and analysis

CI proves the image runs one real round and delivers its alert. The data-collection
workflow ran the same `main.py` hourly inside a fixed window and appended the rows to a
data branch; Grafana reads a database rebuilt from those rows, never the live one.

```mermaid
flowchart LR
    subgraph repo["Repository"]
        code["Python modules<br/>main · api · notifiers · …"]
        targetlist["targets.json / targets.yaml<br/>store first, then seed"]
        colltargets["collection-targets.yaml<br/>fixed collection list"]
    end

    subgraph ci["CI workflow — every push"]
        pytest["pytest<br/>full suite"]
        image["docker build<br/>multi-stage, non-root image"]
        ciround["one real round<br/>docker compose run main.py"]
        receiver["ci-webhook-receiver.py<br/>local alert sink"]
    end

    subgraph deploy["Deployment"]
        compose["docker-compose.yml<br/>health-checker + dashboard"]
        wdprofile["demo-watchdog profile<br/>guard + healthchecks"]
        initwd["init-watchdog.sh<br/>creates the check, prints the ping URL"]
        k8s["deploy/k8s<br/>one pod: monitor + dashboard, one PVC"]
    end

    subgraph analysis["Analysis"]
        collect["data-collection workflow<br/>hourly, fixed window"]
        export["export-history.py<br/>NDJSON since the last row"]
        branch["collected-data branch<br/>append-only NDJSON"]
        importer["import-history.py<br/>NDJSON → collected.db"]
        collecteddb["collected.db"]
        builder["build-grafana-dashboards.py<br/>panels from the probed list"]
        dashjson["grafana/dashboards/*.json"]
        grafana["Grafana<br/>analysis profile, SQLite datasource"]
    end

    code --> pytest
    code --> image
    image --> ciround
    ciround -->|"DISCORD_WEBHOOK_URL"| receiver
    builder -->|"--check in CI"| dashjson

    image --> compose
    image --> k8s
    compose -.->|"--profile demo-watchdog"| wdprofile
    initwd -->|"provisions"| wdprofile

    colltargets -->|"copied over targets.yaml"| collect
    code --> collect
    collect --> export
    export -->|"append, de-duplicated"| branch
    branch --> importer
    importer --> collecteddb
    collecteddb -->|"read-only mount"| grafana
    targetlist --> builder
    dashjson -->|"provisioned"| grafana
```
