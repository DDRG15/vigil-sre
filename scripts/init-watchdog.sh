#!/usr/bin/env bash
#
# init-watchdog.sh — provision the self-hosted watchdog and print its ping URL.
#
# Why a script and not "it just works"
# ------------------------------------
# healthchecks has no way to declare a check in configuration: it is a Django
# app whose checks live in its database, created through a UI or an API that
# needs a key that itself must be created through the UI. So a clone of this
# repo would otherwise start a watchdog with nothing in it, and the reader
# would have to click through a signup to see the dead-man's switch work.
#
# This closes that gap. It is idempotent — run it as many times as you like.
#
# Period and grace are 60s and 300s, matching the compose loop's cadence and
# the dashboard's own "dead" threshold of five missed cycles. The two agree on
# purpose: a watchdog and a page that disagree about when a monitor died would
# each undermine the other.
#
# Two runs, in this order. The first cannot be skipped and cannot be the second:
# the watchdog refuses to start without the key this script generates, so there
# is nothing to provision until the key exists and the service is up.
#
# Usage:
#   ./scripts/init-watchdog.sh     # 1st run: writes WATCHDOG_SECRET_KEY to .env
#   docker compose --profile demo-watchdog up -d watchdog
#   ./scripts/init-watchdog.sh     # 2nd run: creates the check, prints the URL
#   # paste the printed line into .env, then:
#   docker compose --profile demo-watchdog up -d
#
# The watchdog and its key guard live behind the "demo-watchdog" profile, so a
# plain `docker compose up` never starts them (and never needs the key). Every
# compose call below that names the watchdog carries the profile for the same
# reason: a service in a profile that is not enabled does not exist as far as
# `ps` and `exec` are concerned.
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE="docker compose --profile demo-watchdog"

# A key per install, not a literal shared by every clone of a public repo.
# Django signs session cookies with it, so a shared one lets anyone forge a
# session for the demo superuser. Generated here because compose refuses to
# start the service without it — which is the point: the failure is loud.
if ! grep -q '^WATCHDOG_SECRET_KEY=' .env 2>/dev/null; then
    KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    printf '\nWATCHDOG_SECRET_KEY=%s\n' "$KEY" >> .env
    echo "  Generated a WATCHDOG_SECRET_KEY unique to this install in .env" >&2
    echo "  Start the service now:" >&2
    echo "    docker compose --profile demo-watchdog up -d watchdog" >&2
    echo "  then run this script again to create the check." >&2
    exit 0
fi

if ! $COMPOSE ps watchdog --format '{{.Status}}' 2>/dev/null | grep -q Up; then
    echo "The 'watchdog' service is not running. Start it first:" >&2
    echo "  docker compose --profile demo-watchdog up -d watchdog" >&2
    exit 1
fi

# "Up" in `ps` means the container started, not that Django answers. Measured
# against healthchecks:latest right after `up -d watchdog`: the first two
# provisioning attempts came back empty and every later one worked; from a
# freshly created data/watchdog the first success took 76 s. Running this script
# straight after `up`, as the usage above says to, is therefore racing the app's
# own startup, so it retries for about two minutes instead of failing on the
# first try.
#
# The `|| true` is not cosmetic. Under `set -euo pipefail` a pipeline whose grep
# finds no UUID fails the assignment, and the script used to exit right there:
# exit 1, no message, and the "Could not create the check" branch below was
# unreachable.
provision() {
    $COMPOSE exec -T watchdog ./manage.py shell -c "
from datetime import timedelta
from django.contrib.auth.models import User
from hc.accounts.models import Project
from hc.api.models import Check

user, _    = User.objects.get_or_create(username='demo@vigil.local',
                                        defaults={'email': 'demo@vigil.local'})
project, _ = Project.objects.get_or_create(owner=user, defaults={'name': 'vigil-sre'})
check, _   = Check.objects.get_or_create(
    project=project, name='vigil-sre probe',
    defaults={'timeout': timedelta(seconds=60), 'grace': timedelta(seconds=300)},
)
print('UUID=' + str(check.code))
" 2>/dev/null | grep '^UUID=' | cut -d= -f2
}

UUID=""
for _ in $(seq 1 24); do
    UUID=$(provision || true)
    [ -n "$UUID" ] && break
    sleep 5
done

if [ -z "$UUID" ]; then
    echo "Could not create the check. Look at:  $COMPOSE logs watchdog" >&2
    exit 1
fi

cat <<EOF

  Watchdog ready. Paste this line into your .env:

    HEARTBEAT_URL=http://watchdog:8000/ping/$UUID

  Then recreate the monitor so it picks it up:

    docker compose --profile demo-watchdog up -d

  The host is 'watchdog', the service name on compose's internal network —
  not 127.0.0.1, which inside the probe's container points at itself.

  Watchdog UI:  http://127.0.0.1:8000
  User:         demo@vigil.local
  Password:     vigil-demo

  It notifies nobody: with no SMTP and no webhook configured, the evidence
  lives in its own event log. That is deliberate — see the service's comment
  in docker-compose.yml.

EOF
