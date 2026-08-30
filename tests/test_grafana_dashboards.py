"""Los dashboards de Grafana: que sigan al día, que sus queries corran, y que
las líneas de umbral que dibujan sean las que el código aplica.

Ninguno de estos fallos es visible mirando Grafana. Un panel cuya query no
devuelve filas se ve idéntico a uno cuyo datasource está caído; un panel que
dibuja una línea de umbral desactualizada se ve exactamente como una medición
correcta. Por eso se afirman acá y no a ojo.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GEN = ROOT / "scripts" / "build-grafana-dashboards.py"
DASH_DIR = ROOT / "grafana" / "dashboards"
PROV_DS = ROOT / "grafana" / "provisioning" / "datasources" / "sqlite.yml"

from history import SCHEMA  # noqa: E402

COLUMNS = (
    "run_started_at", "url", "checked_at", "status", "error", "http_status",
    "rtt_ms", "dns_ms", "connect_total_ms", "tls_ms", "ttfb_ms",
    "server_processing_ms", "transfer_ms", "body_bytes", "goodput_bps",
)


def dashboards() -> list[tuple[str, dict]]:
    return [(p.name, json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(DASH_DIR.glob("*.json"))]


def panels():
    for name, doc in dashboards():
        for panel in doc["panels"]:
            yield name, panel


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> Path:
    """Una base con la forma real: 4 targets, varias corridas, valores nulos.

    Los nulos importan. tls_ms falta cuando la conexión se reusa y
    server_processing_ms se anula ante redirects, así que una base sin nulos
    dejaría pasar una query que estalla contra los datos verdaderos.
    """
    db = tmp_path_factory.mktemp("grafana") / "seed.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    urls = [e["url"] if isinstance(e, dict) else e
            for e in yaml.safe_load(
                (ROOT / "targets.yaml").read_text(encoding="utf-8"))["targets"]]
    rows = []
    for run in range(8):
        started = f"2026-08-{3 + run:02d}T10:00:00Z"
        for i, url in enumerate(urls):
            rows.append((
                started, url, f"2026-08-{3 + run:02d}T10:00:0{i}Z",
                "DOWN" if (run + i) % 7 == 0 else
                ("DEGRADED" if (run + i) % 3 == 0 else "UP"),
                None, 200,
                40.0 + i * 10, 2.0 + i, 60.0 + i, None if i == 1 else 20.0 + i,
                120.0 + run * 50, None if i == 2 else 300.0 + run * 90,
                80.0, 1000, 5000.0,
            ))
    with con:
        con.executemany(
            f"INSERT INTO probe_results ({', '.join(COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(COLUMNS))})", rows)
    con.close()
    return db


# =============================================================================
# A. Las queries corren y devuelven algo
# =============================================================================

@pytest.mark.parametrize("name,panel", list(panels()),
                         ids=[f"{n}:{p['id']}" for n, p in panels()])
def test_every_panel_query_runs_and_returns_rows(name, panel, seeded) -> None:
    """Una query rota y una query vacía se ven igual en Grafana: un panel gris.

    Ninguna de las dos falla en ningún lado — Grafana dibuja el panel, no
    reporta nada, y la primera hipótesis de quien lo mira es que el datasource
    perdió conexión.
    """
    sql = panel["targets"][0]["queryText"]
    con = sqlite3.connect(f"file:{seeded.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    assert rows, f"{name} panel {panel['id']} ({panel['title']}) no devuelve filas"


def test_the_time_axis_groups_by_run_not_by_probe() -> None:
    """checked_at dispersa los 4 targets en segundos distintos.

    Agrupando por él, cada fila trae un target y tres NULL: el gráfico sale
    entrecortado y se lee como datos faltantes. run_started_at es el único
    valor que los cuatro sondeos de una corrida comparten.
    """
    series = [p for _, p in panels()
              if p["targets"][0]["queryType"] == "time series"]
    assert series, "no quedó ningún panel de serie temporal"
    for panel in series:
        sql = panel["targets"][0]["queryText"]
        assert "strftime('%s', run_started_at)" in sql, (
            f"panel {panel['id']} ({panel['title']}) usa otro eje temporal")


def test_the_series_are_dense_enough_to_read(seeded) -> None:
    """El formato ancho tiene que traer valores, no una grilla de nulos."""
    con = sqlite3.connect(f"file:{seeded.as_posix()}?mode=ro", uri=True)
    try:
        for _, panel in panels():
            if panel["targets"][0]["queryType"] != "time series":
                continue
            rows = con.execute(panel["targets"][0]["queryText"]).fetchall()
            cells = sum(len(r) - 1 for r in rows)
            filled = sum(1 for r in rows for v in r[1:] if v is not None)
            # 45% tolera tls_ms (conexiones reusadas) y server_processing_ms
            # (redirects), que están nulos por diseño y no por error.
            assert filled / cells >= 0.45, (
                f"panel {panel['id']} ({panel['title']}): solo "
                f"{100 * filled / cells:.0f}% de celdas con valor")
    finally:
        con.close()


# =============================================================================
# B. Deriva — los fallos que no se ven mirando Grafana
# =============================================================================

def _load_generator():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen_dash", GEN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_dashboards_match_targets_yaml() -> None:
    """Agregar un target y no regenerar lo deja invisible en los gráficos.

    Y no falla nada: los paneles siguen dibujando las series viejas y se ven
    perfectamente sanos. Es la misma desincronización que dejó el Dockerfile
    copiando tres de seis módulos durante cinco fases — un archivo que debía
    cambiar y no cambió no aparece en ningún diff.
    """
    result = subprocess.run(
        [sys.executable, str(GEN), "--check"],
        cwd=str(ROOT), capture_output=True, text=True)
    assert result.returncode == 0, (
        "los dashboards commiteados no coinciden con targets.yaml:\n"
        f"{result.stdout}{result.stderr}")


def test_check_actually_detects_a_new_target(tmp_path, monkeypatch) -> None:
    """El guard de arriba solo sirve si sabe fallar.

    Un --check que devuelve 0 pase lo que pase da la misma tranquilidad que
    uno que funciona, y es lo que este proyecto ya se comió tres veces.
    """
    gen = _load_generator()
    urls = gen.load_targets()
    antes = json.dumps(gen.build_estado(urls), sort_keys=True)
    despues = json.dumps(
        gen.build_estado(urls + ["https://nuevo.example.com"]), sort_keys=True)
    assert antes != despues, (
        "agregar un target no cambia el dashboard — el generador no lo está "
        "leyendo, y --check no puede detectar nada")
    assert "nuevo.example.com" in despues


def test_the_threshold_lines_match_the_code_that_applies_them() -> None:
    """Un umbral dibujado distinto al aplicado es peor que ninguno.

    El panel se ve como una medición: alguien lee "esto está por debajo de la
    línea" y concluye que no alerta, cuando el código usa otro número. Un
    dashboard equivocado no se ve equivocado.
    """
    import diagnostics
    gen = _load_generator()
    esperado = {
        "dns_ms": diagnostics.DNS_SLOW_MS,
        "ttfb_ms": diagnostics.DEGRADED_TTFB_MS,
        "server_processing_ms": diagnostics.TTFB_BACKEND_SLACK_MS,
    }
    assert gen.THRESHOLDS == esperado, (
        f"los dashboards dibujan {gen.THRESHOLDS} y diagnostics.py aplica "
        f"{esperado}")


def test_the_current_threshold_is_first_among_the_candidates() -> None:
    """La tabla de calibración compara alternativas contra el estado actual.

    Si el primer candidato deja de ser el valor vigente, la columna rotulada
    '(hoy)' miente y la comparación entera pierde su punto de referencia.
    """
    import diagnostics
    gen = _load_generator()
    assert gen.BACKEND_CANDIDATES[0] == diagnostics.TTFB_BACKEND_SLACK_MS, (
        "el primer candidato ya no es el umbral vigente")
    doc = json.loads((DASH_DIR / "calibracion.json").read_text(encoding="utf-8"))
    sql = doc["panels"][0]["targets"][0]["queryText"]
    assert "(hoy)" in sql and str(int(diagnostics.TTFB_BACKEND_SLACK_MS)) in sql


def test_every_panel_points_at_the_provisioned_datasource() -> None:
    """Los dashboards citan un datasource por uid.

    Si el uid del provisioning cambia, los paneles abren sin fuente de datos y
    hay que reasignarla a mano, uno por uno, en cada instalación.
    """
    prov = yaml.safe_load(PROV_DS.read_text(encoding="utf-8"))
    uids = {d["uid"] for d in prov["datasources"]}
    tipos = {d["type"] for d in prov["datasources"]}
    for name, panel in panels():
        ds = panel["datasource"]
        assert ds["uid"] in uids, f"{name} panel {panel['id']}: uid {ds['uid']}"
        assert ds["type"] in tipos, f"{name} panel {panel['id']}: type {ds['type']}"


def test_the_dashboards_open_on_the_collection_window() -> None:
    """Con el rango por defecto de Grafana estos paneles salen vacíos.

    Los datos son un período cerrado del 3 al 17 de agosto de 2026. Abrir en
    "últimas 6 horas" muestra tres dashboards en blanco, y la primera
    conclusión de cualquiera es que la conexión falló.
    """
    for name, doc in dashboards():
        rango = doc["time"]
        assert rango["from"].startswith("2026-08-"), f"{name}: {rango}"
        assert rango["to"].startswith("2026-08-"), f"{name}: {rango}"


def test_the_panels_declare_their_time_column() -> None:
    """Sin timeColumns el plugin entrega el epoch como un número cualquiera y
    el eje X sale numerado en vez de fechado."""
    for name, panel in panels():
        t = panel["targets"][0]
        if t["queryType"] == "time series":
            assert t["timeColumns"] == ["time"], f"{name} panel {panel['id']}"
