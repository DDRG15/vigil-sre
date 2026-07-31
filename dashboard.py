"""
dashboard.py — the HTML view over vigil-sre's own JSON (the design note, an earlier release).

Why no FastAPI, no Jinja2, no HTMX
------------------------------------
The roadmap picked that stack while the dashboard was still hypothetical and
api.py did not exist. It exists now, serves JSON on stdlib, and this view is
two routes and two templates. FastAPI would buy routing, async and validation
this does not need — while dragging in starlette, pydantic and an ASGI server,
four-plus packages against the three this project has after nineteen releases.
HTMX would buy interactivity that here reduces to a ten-line fetch, and
`<details>` handles expansion with no JavaScript at all.

The exit is named: the day this grows real interactions — filtering, silencing
a target, acknowledging an incident — HTMX earns its place and Jinja2 with it.
Not today.

Escaping is structural, not remembered
----------------------------------------
Target URLs and last_error land inside HTML, so a target named
`https://x.com/<script>` in targets.yaml is an XSS vector. html.escape is
enough, but calling it at every interpolation point is discipline, and this
project has decided twice already (an earlier release D4, an earlier release D3) that a remembered
guarantee is a broken one. So every value reaches the page through _row(),
which escapes on the way in, and nothing is interpolated by any other path.

Python  : 3.11+
Depends : stdlib only (html, json).
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

#: Same palette Discord and Slack already use for the same three states, so
#: the reflex an operator trains in one channel carries to the others.
COLOURS: dict[str, str] = {
    "UP"      : "#00C853",
    "DEGRADED": "#FFB300",
    "DOWN"    : "#FF0000",
    "UNKNOWN" : "#6b7280",
}

#: Colour is never the only channel — roughly 8% of men have a colour vision
#: deficiency, and red/green is precisely the pair that fails. Every state
#: also carries a distinct glyph and its own word.
GLYPHS: dict[str, str] = {
    "UP": "●", "DEGRADED": "▲", "DOWN": "■", "UNKNOWN": "○"}

#: Run cadence in seconds. Staleness is judged in multiples of this.
RUN_INTERVAL_S: int = 60

#: The page re-fetches at half the run cadence: the data only changes once per
#: run, but the freshness readout should never be more than half a cycle stale
#: about its own staleness.
POLL_INTERVAL_S: int = 30


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}ms"


def freshness(stale_seconds: float | None) -> tuple[str, str]:
    """
    Turn the age of the oldest reading into a severity and a sentence.

    This is the one thing on the page that is not copied from anyone. Almost
    no monitoring dashboard tells you its own data is old: it shows green and
    the reader assumes green means now. When the probe process dies, this
    endpoint keeps answering 200 with data that is perfectly well-formed and
    simply stale — so the page has to say so louder than it says anything
    else, and at the worst level it dims everything below it. Green that
    cannot be trusted should not look reassuring.
    """
    if stale_seconds is None:
        return "unknown", "Sin datos todavía — el monitor no ha completado una corrida."
    cycles = stale_seconds / RUN_INTERVAL_S
    if cycles < 2:
        return "fresh", f"Actualizado hace {stale_seconds:.0f}s."
    minutes = stale_seconds / 60
    if cycles < 5:
        return "stale", (
            f"Estos datos tienen {minutes:.0f} min — se esperaba una corrida "
            f"cada {RUN_INTERVAL_S}s."
        )
    return "dead", (
        f"Estos datos tienen {minutes:.0f} min. El monitor probablemente está "
        f"caído: lo que ves abajo es el pasado."
    )


def _row(url: str, state: dict, history: dict) -> str:
    """
    Render one target row. **Every value is escaped here**, which is the only
    place any of them enters the HTML — see the module docstring for why that
    is structural rather than a convention to remember.
    """
    status  = state.get("status", "UNKNOWN")
    status  = status if status in COLOURS else "UNKNOWN"
    detail  = state.get("last_error") or ""
    checked = state.get("last_checked", "—")

    e_url     = html.escape(url)
    e_status  = html.escape(status)
    e_detail  = html.escape(detail)
    e_checked = html.escape(str(checked))
    glyph     = GLYPHS[status]
    colour    = COLOURS[status]

    uptime = _fmt_pct(history.get("uptime_pct"))
    p50    = _fmt_ms(history.get("p50_ttfb_ms"))
    p95    = _fmt_ms(history.get("p95_ttfb_ms"))

    detail_block = (
        f'<p class="detail">{e_detail}</p>' if e_detail
        else '<p class="detail muted">Sin errores registrados.</p>'
    )

    return f"""<details class="row" data-url="{e_url}">
  <summary>
    <span class="state" style="color:{colour}" title="{e_status}">{glyph}<b>{e_status}</b></span>
    <span class="url">{e_url}</span>
    <span class="num" title="uptime">{uptime}</span>
    <span class="num" title="p50 TTFB">{p50}</span>
    <span class="num" title="p95 TTFB">{p95}</span>
  </summary>
  <div class="body">
    {detail_block}
    <p class="muted">Último chequeo: {e_checked}</p>
  </div>
</details>"""


def render_rows(targets: dict, history: dict, stale_seconds: float | None) -> str:
    """The fragment the page re-fetches: freshness banner plus every row."""
    level, message = freshness(stale_seconds)
    banner = (
        f'<div class="freshness {level}" role="status">{html.escape(message)}</div>'
    )
    if not targets:
        return banner + '<p class="empty">No hay targets configurados todavía.</p>'
    def _state_of(url: str) -> dict:
        value = targets.get(url)
        # A hand-edited state.json can put a non-dict here; render it as
        # UNKNOWN rather than crashing the whole page over one bad row.
        return value if isinstance(value, dict) else {}

    def _history_of(url: str) -> dict:
        value = history.get(url) if isinstance(history, dict) else None
        return value if isinstance(value, dict) else {}

    rows = "\n".join(
        _row(url, _state_of(url), _history_of(url)) for url in sorted(targets)
    )
    return f'{banner}<div class="rows {level}">{rows}</div>'


_STYLE = """
:root{--bg:#fff;--fg:#111827;--muted:#6b7280;--line:#e5e7eb;--card:#f9fafb}
@media(prefers-color-scheme:dark){
  :root{--bg:#0b0f16;--fg:#e5e7eb;--muted:#9ca3af;--line:#1f2937;--card:#111827}}
*{box-sizing:border-box}
body{margin:0;padding:1.5rem;background:var(--bg);color:var(--fg);
  font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
h1{font-size:1rem;margin:0;letter-spacing:.02em}
header{display:flex;justify-content:space-between;align-items:baseline;
  margin-bottom:1rem;gap:1rem;flex-wrap:wrap}
.freshness{padding:.4rem .6rem;border-radius:4px;margin-bottom:.75rem;
  border-left:3px solid var(--muted);color:var(--muted);font-size:.85rem}
.freshness.stale{border-color:#FFB300;color:#FFB300;background:#FFB3001a}
.freshness.dead{border-color:#FF0000;color:#FF0000;background:#FF00001a;
  font-weight:700}
/* The whole list dims when the data cannot be trusted: green that might be
   hours old has no business looking reassuring. */
.rows.dead{opacity:.45}
.row{border-bottom:1px solid var(--line)}
.row summary{display:grid;grid-template-columns:9rem 1fr 5rem 5rem 5rem;
  gap:.75rem;align-items:center;padding:.5rem .25rem;cursor:pointer}
.row summary::-webkit-details-marker{display:none}
.row:hover{background:var(--card)}
.state{white-space:nowrap}
.state b{font-weight:600;margin-left:.4rem;font-size:.8rem}
.url{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.num{text-align:right;font-variant-numeric:tabular-nums;color:var(--muted)}
.head{color:var(--muted);font-size:.75rem;text-transform:uppercase;
  letter-spacing:.05em;border-bottom:1px solid var(--line)}
.body{padding:.25rem .25rem .75rem 9.75rem}
.detail{margin:.25rem 0;word-break:break-word}
.muted{color:var(--muted)}
.empty{color:var(--muted);padding:2rem 0;text-align:center}
@media(max-width:640px){
  body{padding:.75rem}
  .row summary{grid-template-columns:1fr;gap:.15rem}
  .num{text-align:left}
  .num::before{content:attr(title) ": ";text-transform:uppercase;font-size:.7rem}
  .body{padding-left:.25rem}}
"""

# Ten lines of fetch, in place of a JavaScript dependency. It preserves which
# rows are open across a swap — replacing innerHTML would otherwise close every
# expanded row every 30 seconds, which is the kind of small wrongness that
# makes a tool feel broken.
_SCRIPT = """
async function refresh(){
  const open=[...document.querySelectorAll('details[open]')].map(d=>d.dataset.url);
  try{
    const r=await fetch('/partial/targets',{cache:'no-store'});
    if(!r.ok)return;
    document.getElementById('list').innerHTML=await r.text();
    open.forEach(u=>{
      const d=document.querySelector(`details[data-url="${CSS.escape(u)}"]`);
      if(d)d.open=true;});
  }catch(e){/* a failed poll keeps the last good render on screen */}
}
setInterval(refresh, %d000);
"""


def render_page(targets: dict, history: dict, stale_seconds: float | None) -> str:
    """The full page. Rows come from the same fragment the poll re-fetches."""
    return f"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>vigil-sre</title>
<style>{_STYLE}</style>
</head><body>
<header>
  <h1>vigil-sre</h1>
  <span class="muted">{len(targets)} target(s) · se actualiza cada {POLL_INTERVAL_S}s</span>
</header>
<div class="row head"><div class="row"><summary style="cursor:default">
  <span>Estado</span><span>Target</span><span class="num">Uptime</span>
  <span class="num">p50</span><span class="num">p95</span>
</summary></div></div>
<div id="list">{render_rows(targets, history, stale_seconds)}</div>
<script>{_SCRIPT % POLL_INTERVAL_S}</script>
</body></html>"""
