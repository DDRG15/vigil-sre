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
# Usage:
#   docker compose up -d watchdog
#   ./scripts/init-watchdog.sh
#   # paste the printed line into .env, then:
#   docker compose up -d
set -euo pipefail

cd "$(dirname "$0")/.."

if ! docker compose ps watchdog --format '{{.Status}}' 2>/dev/null | grep -q Up; then
    echo "El servicio 'watchdog' no está corriendo. Levantalo primero:" >&2
    echo "  docker compose up -d watchdog" >&2
    exit 1
fi

UUID=$(docker compose exec -T watchdog ./manage.py shell -c "
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
" 2>/dev/null | grep '^UUID=' | cut -d= -f2)

if [ -z "$UUID" ]; then
    echo "No se pudo crear el check. Revisá:  docker compose logs watchdog" >&2
    exit 1
fi

cat <<EOF

  Watchdog listo. Pegá esta línea en tu .env:

    HEARTBEAT_URL=http://watchdog:8000/ping/$UUID

  El host es 'watchdog', el nombre del servicio en la red interna de compose —
  no 127.0.0.1, que dentro del contenedor del probe apunta a sí mismo.

  UI del watchdog:  http://127.0.0.1:8000
  Usuario:          demo@vigil.local
  Contraseña:       vigil-demo

  No notifica a nadie: sin SMTP y sin webhook configurados, la evidencia vive
  en su propio log de eventos. Eso es deliberado — ver el comentario del
  servicio en docker-compose.yml.

EOF
