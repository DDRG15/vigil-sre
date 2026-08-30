"""build-grafana-dashboards.py — genera los dashboards de Grafana desde targets.yaml.

Por qué generados y no escritos a mano
--------------------------------------
Un dashboard de Grafana nombra sus series en SQL. Con los targets escritos a
mano dentro de las queries, agregar un target al monitor lo deja invisible en
los gráficos, y nada falla: el panel sigue dibujando las series viejas y se ve
perfectamente sano. Es la misma clase de desincronización que dejó el Dockerfile
copiando tres de seis módulos durante cinco fases.

Acá `targets.yaml` es la única fuente. Regenerar es un comando, y el test de
regresión compara lo generado contra lo commiteado: si alguien agrega un target
y no regenera, el test falla en ese mismo commit.

Uso:
    python scripts/build-grafana-dashboards.py
    python scripts/build-grafana-dashboards.py --check   # no escribe, solo verifica
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "grafana" / "dashboards"

DS = {"type": "frser-sqlite-datasource", "uid": "vigil-sqlite"}

# El eje temporal es run_started_at, NO checked_at.
#
# Los cuatro targets de una corrida comparten run_started_at, pero cada uno
# termina en su propio segundo: checked_at se dispersa hasta 6 s dentro de la
# misma corrida. Agrupando por checked_at, 920 filas producen 505 timestamps
# distintos y cada uno trae un solo target con los otros tres en NULL --
# gráficos entrecortados que parecen datos faltantes. Con run_started_at son
# 230 filas densas de 4 valores, verificado: 230 grupos, todos de 4 filas.
#
# Para percentiles y agregados sigue usándose checked_at, que es cuándo se
# midió de verdad.
#
# SQLite parsea el formato '2026-08-03T17:31:32Z' y devuelve epoch en segundos;
# verificado con un round-trip a datetime antes de escribir una sola query.
TIME = "CAST(strftime('%s', run_started_at) AS INTEGER) AS time"

# Los umbrales que deciden DEGRADED, copiados de diagnostics.py. Se repiten acá
# a propósito y con un test que compara ambos: un dashboard de calibración que
# dibuja una línea de umbral distinta a la que el código aplica es peor que no
# tener dashboard, porque parece una medición.
THRESHOLDS = {
    "dns_ms": 200.0,
    "ttfb_ms": 1_500.0,
    "server_processing_ms": 500.0,
}


def short(url: str) -> str:
    """Nombre de serie legible. 'https://www.google.com' -> 'google.com'."""
    name = url.split("://", 1)[-1].split("/", 1)[0]
    return name[4:] if name.startswith("www.") else name


def load_targets() -> list[str]:
    doc = yaml.safe_load((ROOT / "targets.yaml").read_text(encoding="utf-8"))
    out = []
    for entry in doc.get("targets") or []:
        out.append(entry["url"] if isinstance(entry, dict) else entry)
    if not out:
        raise SystemExit("targets.yaml no declara targets")
    return out


def pivot(urls: list[str], column: str, agg: str = "AVG") -> str:
    """Una columna por target — formato ancho.

    Grafana dibuja una serie por columna sin configuración extra. La
    alternativa (formato largo con una columna 'metric') depende de cómo cada
    datasource decide agrupar, y ese comportamiento no es el mismo en todos.
    """
    cols = ",\n  ".join(
        f'{agg}(CASE WHEN url = {sql_str(u)} THEN {column} END) AS {sql_id(short(u))}'
        for u in urls
    )
    return f"SELECT\n  {TIME},\n  {cols}\nFROM probe_results\nGROUP BY time\nORDER BY time"


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_id(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def target(sql: str, fmt: str = "time series") -> dict:
    return {
        "refId": "A",
        "datasource": DS,
        "queryText": sql,
        "rawQueryText": sql,
        "queryType": fmt,
        "timeColumns": ["time"] if fmt == "time series" else [],
        "format": fmt,
    }


def panel(pid: int, title: str, ptype: str, sql: str, *, x: int, y: int,
          w: int = 12, h: int = 8, unit: str | None = None,
          fmt: str = "time series", description: str = "",
          extra: dict | None = None) -> dict:
    defaults: dict = {"custom": {}}
    if unit:
        defaults["unit"] = unit
    p = {
        "id": pid,
        "type": ptype,
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [target(sql, fmt)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {},
    }
    if extra:
        _merge(p, extra)
    return p


def _merge(base: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def dashboard(uid: str, title: str, description: str, panels: list) -> dict:
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["vigil-sre"],
        "timezone": "browser",
        "editable": True,
        "schemaVersion": 39,
        "version": 1,
        "refresh": "",
        # La ventana de recolección, no "últimas 6 horas": estos datos son un
        # período cerrado del 3 al 17 de agosto de 2026. Abrir el dashboard en
        # el rango por defecto de Grafana mostraría un panel vacío y la primera
        # conclusión sería que la conexión falló.
        "time": {"from": "2026-08-03T17:00:00.000Z", "to": "2026-08-17T16:00:00.000Z"},
        "panels": panels,
    }


# =============================================================================
# 1. Estado y disponibilidad
# =============================================================================

def build_estado(urls: list[str]) -> dict:
    uptime = (
        "SELECT\n"
        "  url AS target,\n"
        "  COUNT(*) AS sondeos,\n"
        # DEGRADED cuenta como arriba. Es la decisión de la Fase 16 y no un
        # detalle de esta query: disponibilidad y performance son dos señales
        # distintas, y mezclarlas hace que un target lento se reporte como caído.
        "  ROUND(100.0 * SUM(CASE WHEN status IN ('UP','DEGRADED') THEN 1 ELSE 0 END)\n"
        "        / COUNT(*), 2) AS uptime_pct,\n"
        "  SUM(CASE WHEN status='DEGRADED' THEN 1 ELSE 0 END) AS degradados,\n"
        "  SUM(CASE WHEN status='DOWN' THEN 1 ELSE 0 END) AS caidos\n"
        "FROM probe_results\nGROUP BY url\nORDER BY uptime_pct ASC"
    )

    estado_num = pivot(
        urls,
        "CASE status WHEN 'UP' THEN 2 WHEN 'DEGRADED' THEN 1 ELSE 0 END",
        agg="MIN",
    )

    caidas = (
        f"SELECT\n  {TIME},\n"
        "  SUM(CASE WHEN status='DOWN' THEN 1 ELSE 0 END) AS caidos\n"
        "FROM probe_results\nGROUP BY time\nORDER BY time"
    )

    return dashboard(
        "vigil-estado", "vigil-sre · Estado y disponibilidad",
        "Qué tan seguido respondió cada target, y cuándo no.",
        [
            panel(1, "Disponibilidad por target", "table", uptime,
                  x=0, y=0, w=24, h=7, fmt="table",
                  description=(
                      "DEGRADED cuenta como disponible: el target respondió. "
                      "La lentitud se mira en el dashboard de fases."),
                  extra={"fieldConfig": {"overrides": [{
                      "matcher": {"id": "byName", "options": "uptime_pct"},
                      "properties": [
                          {"id": "unit", "value": "percent"},
                          {"id": "custom.cellOptions",
                           "value": {"type": "color-background"}},
                          {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                              {"color": "red", "value": None},
                              {"color": "orange", "value": 95},
                              {"color": "green", "value": 99.5}]}},
                      ]}]}}),

            panel(2, "Estado en el tiempo  (2=UP  1=DEGRADED  0=DOWN)",
                  "state-timeline", estado_num, x=0, y=7, w=24, h=9,
                  description=(
                      "Una banda por target. Los huecos son corridas que el "
                      "scheduler de GitHub se salteó, no caídas."),
                  extra={"fieldConfig": {"defaults": {
                      "min": 0, "max": 2,
                      "thresholds": {"mode": "absolute", "steps": [
                          {"color": "red", "value": None},
                          {"color": "orange", "value": 1},
                          {"color": "green", "value": 2}]},
                      "custom": {"lineWidth": 0, "fillOpacity": 90}}},
                      "options": {"mergeValues": True, "showValue": "never"}}),

            panel(3, "Targets caídos simultáneamente", "timeseries", caidas,
                  x=0, y=16, w=24, h=7,
                  description=(
                      "Más de uno a la vez apunta al punto de observación "
                      "—el runner— y no a los targets."),
                  extra={"fieldConfig": {"defaults": {"custom": {
                      "drawStyle": "bars", "fillOpacity": 60}}}}),
        ])


# =============================================================================
# 2. Fases de latencia — lo que este proyecto mide y un ping no
# =============================================================================

def build_fases(urls: list[str]) -> dict:
    def percentiles(column: str) -> str:
        return (
            "WITH r AS (\n"
            f"  SELECT url, {column} AS v,\n"
            f"         CUME_DIST() OVER (PARTITION BY url ORDER BY {column}) AS cd\n"
            f"  FROM probe_results WHERE {column} IS NOT NULL\n)\n"
            "SELECT url AS target, COUNT(*) AS n,\n"
            "  ROUND(MIN(CASE WHEN cd >= 0.50 THEN v END)) AS p50,\n"
            "  ROUND(MIN(CASE WHEN cd >= 0.95 THEN v END)) AS p95,\n"
            "  ROUND(MIN(CASE WHEN cd >= 0.99 THEN v END)) AS p99,\n"
            "  ROUND(MAX(v)) AS max\n"
            "FROM r GROUP BY url ORDER BY p95 DESC"
        )

    panels = [
        panel(1, "TTFB — tiempo hasta el primer byte", "timeseries",
              pivot(urls, "ttfb_ms"), x=0, y=0, unit="ms",
              description="La medida que más se parece a 'cuánto tardó'."),
        panel(2, "RTT — ida y vuelta TCP", "timeseries",
              pivot(urls, "rtt_ms"), x=12, y=0, unit="ms",
              description=(
                  "Distancia física. No mejora con tuning de servidor; "
                  "mejora acercando el contenido.")),
        panel(3, "Resolución DNS", "timeseries",
              pivot(urls, "dns_ms"), x=0, y=8, unit="ms"),
        panel(4, "Handshake TLS", "timeseries",
              pivot(urls, "tls_ms"), x=12, y=8, unit="ms",
              description="Solo en conexiones nuevas; las reusadas no lo pagan."),
        panel(5, "Procesamiento del backend  (TTFB − RTT)", "timeseries",
              pivot(urls, "server_processing_ms"), x=0, y=16, w=24, unit="ms",
              description=(
                  "Nulo cuando hubo redirects: la atribución por fases no es "
                  "válida entre saltos, así que se reporta vacío en vez de "
                  "inventado.")),
        panel(6, "Percentiles de TTFB (ms)", "table", percentiles("ttfb_ms"),
              x=0, y=24, w=12, h=8, fmt="table",
              description="Nearest-rank con CUME_DIST — el mismo método que --report."),
        panel(7, "Percentiles de backend (ms)", "table",
              percentiles("server_processing_ms"),
              x=12, y=24, w=12, h=8, fmt="table"),
    ]
    return dashboard(
        "vigil-fases", "vigil-sre · Fases de latencia",
        "Dónde se va el tiempo: DNS, TCP, TLS, backend, transferencia.",
        panels)


# =============================================================================
# 3. Calibración de umbrales — el dashboard que responde una decisión
# =============================================================================

#: Candidatos a evaluar para el umbral de backend, en ms. El primero es el que
#: rige hoy, para que la tabla muestre el punto de partida al lado de las
#: alternativas en vez de obligar a recordarlo.
BACKEND_CANDIDATES = (500, 750, 1_000, 1_500, 2_000, 3_000)


def build_calibracion(urls: list[str]) -> dict:
    cols = ",\n".join(
        f"  ROUND(100.0 * SUM(CASE WHEN server_processing_ms > {c} THEN 1 ELSE 0 END)\n"
        f"        / COUNT(*), 1) AS {sql_id(f'{c} ms' + (' (hoy)' if c == BACKEND_CANDIDATES[0] else ''))}"
        for c in BACKEND_CANDIDATES
    )
    firing = (
        "SELECT\n  url AS target,\n  COUNT(*) AS muestras,\n"
        f"{cols}\n"
        "FROM probe_results\nWHERE server_processing_ms IS NOT NULL\n"
        "GROUP BY url ORDER BY url"
    )

    mediana = (
        "WITH r AS (\n"
        "  SELECT url, server_processing_ms AS v,\n"
        "         CUME_DIST() OVER (PARTITION BY url ORDER BY server_processing_ms) AS cd\n"
        "  FROM probe_results WHERE server_processing_ms IS NOT NULL\n)\n"
        "SELECT url AS target,\n"
        "  ROUND(MIN(CASE WHEN cd >= 0.50 THEN v END)) AS p50,\n"
        f"  {THRESHOLDS['server_processing_ms']:.0f} AS umbral_actual,\n"
        "  ROUND(MIN(CASE WHEN cd >= 0.50 THEN v END))\n"
        f"    - {THRESHOLDS['server_processing_ms']:.0f} AS margen\n"
        "FROM r GROUP BY url ORDER BY margen DESC"
    )

    return dashboard(
        "vigil-calibracion", "vigil-sre · Calibración de umbrales",
        "A qué porcentaje del tiempo dispararía cada umbral candidato, "
        "medido sobre 920 sondeos reales del 3 al 17 de agosto de 2026.",
        [
            panel(1, "Backend: % de sondeos que superarían cada umbral", "table",
                  firing, x=0, y=0, w=24, h=8, fmt="table",
                  description=(
                      "Cada celda es la tasa de disparo si ese fuera el umbral. "
                      "La columna '500 ms (hoy)' es el estado actual: en "
                      "cloudflare.com dispara el 63.8% de las veces, que es "
                      "por qué aparece DEGRADED dos de cada tres sondeos."),
                  extra={"fieldConfig": {"defaults": {
                      "unit": "percent",
                      "custom": {"cellOptions": {"type": "color-background"}},
                      "thresholds": {"mode": "absolute", "steps": [
                          {"color": "green", "value": None},
                          {"color": "orange", "value": 5},
                          {"color": "red", "value": 20}]}}}}),

            panel(2, "Mediana del backend contra el umbral vigente", "table",
                  mediana, x=0, y=8, w=12, h=8, fmt="table",
                  description=(
                      "Margen positivo significa que el umbral está POR DEBAJO "
                      "de la mediana del target: no mide una anomalía, mide el "
                      "funcionamiento normal. Un umbral así no se puede "
                      "silenciar sin apagar la regla entera."),
                  extra={"fieldConfig": {"overrides": [{
                      "matcher": {"id": "byName", "options": "margen"},
                      "properties": [
                          {"id": "custom.cellOptions",
                           "value": {"type": "color-background"}},
                          {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                              {"color": "green", "value": None},
                              {"color": "red", "value": 0}]}}]}]}}),

            panel(3, "Distribución del tiempo de backend", "histogram",
                  pivot(urls, "server_processing_ms"),
                  x=12, y=8, w=12, h=8, unit="ms",
                  description=(
                      "Dónde vive cada target. Si la masa de un target está a "
                      "la derecha del umbral, el umbral no le corresponde.")),

            panel(4, "Backend en el tiempo, con el umbral vigente marcado",
                  "timeseries", pivot(urls, "server_processing_ms"),
                  x=0, y=16, w=24, h=9, unit="ms",
                  description=(
                      "La línea roja es TTFB_BACKEND_SLACK_MS = 500 de "
                      "diagnostics.py. Todo lo que queda encima produjo un "
                      "SLOW_BACKEND."),
                  extra={"fieldConfig": {"defaults": {
                      "custom": {"thresholdsStyle": {"mode": "line"}},
                      "thresholds": {"mode": "absolute", "steps": [
                          {"color": "transparent", "value": None},
                          {"color": "red",
                           "value": THRESHOLDS["server_processing_ms"]}]}}}}),
        ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="No escribe: falla si lo commiteado no coincide "
                             "con lo que targets.yaml produce hoy.")
    args = parser.parse_args()

    urls = load_targets()
    built = {
        "estado.json": build_estado(urls),
        "fases.json": build_fases(urls),
        "calibracion.json": build_calibracion(urls),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, doc in built.items():
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        path = OUT / name
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != text:
                stale.append(name)
        else:
            path.write_text(text, encoding="utf-8")
            print(f"  {path.relative_to(ROOT)}  ({len(doc['panels'])} paneles)")

    if args.check:
        if stale:
            print("  desactualizados: " + ", ".join(stale), file=sys.stderr)
            print("  corré: python scripts/build-grafana-dashboards.py",
                  file=sys.stderr)
            return 1
        print(f"  {len(built)} dashboards al día con targets.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
