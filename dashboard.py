"""
dashboard.py — the HTML view over vigil-sre's own JSON.

Why no FastAPI, no Jinja2, no HTMX
------------------------------------
The initial design picked that stack while the dashboard was still hypothetical and
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
project has decided twice already that a remembered
guarantee is a broken one. So every value reaches the page through _row(),
which escapes on the way in, and nothing is interpolated by any other path.

Python  : 3.11+
Depends : stdlib only (html, json).
"""

from __future__ import annotations

import base64
import hashlib
import html
from urllib.parse import urlsplit

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

#: A bucket with no probe in it. Deliberately NOT COLOURS["UNKNOWN"]: an
#: unknown STATUS is something the monitor observed and could not classify,
#: while an empty bucket is time the monitor was not watching at all. Rendering
#: the second as the first would let a gap in coverage read as a reading.
STRIP_EMPTY: str = "#6b728040"

#: Bar geometry, in SVG user units. 60 bars at 4+1 fit inside 300px, which is
#: the widest a row can give up without squeezing the URL column.
STRIP_BAR_W : int = 4
STRIP_GAP   : int = 1
STRIP_HEIGHT: int = 18


def render_strip(strip: list[str | None] | None) -> str:
    """
    One target's history as a bar per time bucket, oldest on the left.

    Inline SVG rather than a div per bar: 60 bars across six targets is 360
    elements re-inserted on every 30-second poll, and one <svg> is one node.
    No dependency either way — this project has kept that count at three
    through every release so far.

    Colour is NOT the only channel here, but it is the only one a 4px bar can
    carry. That is acceptable because the strip is supplementary: the status
    glyph and the status word sit in the same row, unchanged, and remain the
    primary signal. Each bar also carries a <title>, so the information is
    reachable by hover and by screen reader rather than by hue alone.
    """
    if not strip:
        return ""
    bars = []
    for index, status in enumerate(strip):
        x      = index * (STRIP_BAR_W + STRIP_GAP)
        colour = COLOURS.get(status, STRIP_EMPTY) if status else STRIP_EMPTY
        label  = status if status else "no data"
        bars.append(
            f'<rect x="{x}" y="0" width="{STRIP_BAR_W}" height="{STRIP_HEIGHT}" '
            f'fill="{colour}"><title>{html.escape(label)}</title></rect>'
        )
    width = len(strip) * (STRIP_BAR_W + STRIP_GAP) - STRIP_GAP
    return (
        f'<svg class="strip" viewBox="0 0 {width} {STRIP_HEIGHT}" '
        f'width="{width}" height="{STRIP_HEIGHT}" role="img" '
        f'aria-label="Status history, oldest to newest">'
        f'{"".join(bars)}</svg>'
    )


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
        return "unknown", "No data yet — the monitor has not completed a run."
    cycles = stale_seconds / RUN_INTERVAL_S
    if cycles < 2:
        return "fresh", f"Updated {stale_seconds:.0f}s ago."
    minutes = stale_seconds / 60
    if cycles < 5:
        return "stale", (
            f"This data is {minutes:.0f} min old — a run was expected "
            f"every {RUN_INTERVAL_S}s."
        )
    return "dead", (
        f"This data is {minutes:.0f} min old. The monitor is probably "
        f"down: what you see below is the past."
    )


def _muted_badge(window: dict | None) -> str:
    """
    Mark a target whose alerts are silenced, with the window that silences it.

    Not optional decoration. A target inside a maintenance window can be DOWN
    and say nothing, so a row that looks identical to a healthy one is worse
    than no row: the reader sees a status and assumes someone would have been
    told. Naming the window also answers the next question — when does this
    stop being true — without a trip to the config.
    """
    if not window:
        return ""
    label = html.escape(f'{window.get("start", "?")}–{window.get("end", "?")} UTC')
    return (
        f'<span class="muted-badge" title="Alerts silenced by maintenance '
        f'window">🔇 {label}</span>'
    )


def _row(url: str, state: dict, history: dict, strip: list | None = None,
         muted: dict | None = None) -> str:
    """
    Render one target row. **Every value is escaped here**, which is the only
    place any of them enters the HTML — see the module docstring for why that
    is structural rather than a convention to remember.
    """
    # isinstance BEFORE the membership test: `in COLOURS` raises TypeError on an
    # unhashable value rather than falling through to UNKNOWN, and `and` short-
    # circuits before it gets the chance. TargetState types these `str`, but
    # nothing enforces that at the JSON boundary — a hand edit during debugging
    # is enough, and the crash costs the whole page, not the one bad row.
    status  = state.get("status", "UNKNOWN")
    status  = status if isinstance(status, str) and status in COLOURS else "UNKNOWN"
    detail  = state.get("last_error") or ""
    detail  = detail if isinstance(detail, str) else str(detail)
    checked = state.get("last_checked", "—")

    e_url     = html.escape(url)
    e_status  = html.escape(status)
    labels = {"UP": "Available", "DEGRADED": "Slow", "DOWN": "Unavailable", "UNKNOWN": "Unknown"}
    label = labels[status]
    try:
        host = html.escape(urlsplit(url).hostname or url)
    except ValueError:
        host = e_url
    e_detail  = html.escape(detail)
    e_checked = html.escape(str(checked))
    glyph     = GLYPHS[status]
    # `status` was reduced to a COLOURS key above, so this is one of four fixed
    # words and not caller text.
    state_cls = f"st-{status.lower()}"

    uptime = _fmt_pct(history.get("uptime_pct"))
    p50    = _fmt_ms(history.get("p50_ttfb_ms"))
    p95    = _fmt_ms(history.get("p95_ttfb_ms"))

    detail_block = (
        f'<p class="detail">{e_detail}</p>' if e_detail
        else '<p class="detail muted">No errors recorded.</p>'
    )

    return f"""<details class="row" data-url="{e_url}">
  <summary>
    <span class="state {state_cls}" title="{e_status}">{glyph}<b>{label}</b></span>
    <span class="url" title="{e_url}">{host}</span>
    <span class="last-check">{e_checked}</span>
    <span class="hist">{render_strip(strip)}</span>{_muted_badge(muted)}
  </summary>
  <div class="body">
    <p>{e_url}</p>
    {detail_block}
    <p class="muted">Last check (UTC): {e_checked}</p>
    <p class="muted">Probe availability: {uptime} · TTFB p50: {p50} · p95: {p95}.
    Slow means the check succeeded with a performance finding.</p>
  </div>
</details>"""


def render_rows(targets: dict, history: dict, stale_seconds: float | None,
                strips: dict | None = None, muted: dict | None = None) -> str:
    """The fragment the page re-fetches: freshness banner plus every row."""
    level, message = freshness(stale_seconds)
    banner = (
        f'<div class="freshness {level}" role="status">{html.escape(message)}</div>'
    )
    if not targets:
        return banner + '<p class="empty">No targets configured yet.</p>'
    def _state_of(url: str) -> dict:
        value = targets.get(url)
        # A hand-edited state.json can put a non-dict here; render it as
        # UNKNOWN rather than crashing the whole page over one bad row.
        return value if isinstance(value, dict) else {}

    def _history_of(url: str) -> dict:
        value = history.get(url) if isinstance(history, dict) else None
        return value if isinstance(value, dict) else {}

    def _strip_of(url: str) -> list | None:
        value = strips.get(url) if isinstance(strips, dict) else None
        return value if isinstance(value, list) else None

    def _muted_of(url: str) -> dict | None:
        value = muted.get(url) if isinstance(muted, dict) else None
        return value if isinstance(value, dict) else None

    rows = "\n".join(
        _row(url, _state_of(url), _history_of(url), _strip_of(url), _muted_of(url))
        for url in sorted(targets)
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
.row summary{display:grid;grid-template-columns:8rem minmax(8rem,1fr) 12rem minmax(8rem,19rem);
  gap:.75rem;align-items:center;padding:.5rem .25rem;cursor:pointer}
.row summary::-webkit-details-marker{display:none}
.head summary{cursor:default}
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
/* Supplementary by design: the strip may shrink or vanish before the status
   word does, because the word is the primary channel and a 4px bar is not. */
.hist{display:flex;align-items:center;overflow:hidden}
.strip{max-width:100%;display:block}
@media(max-width:900px){.hist{display:none}}
/* Empty until a poll fails, so it costs no space in the normal case. When it
   does speak it is the only red in the header, because it outranks everything
   below it: if polling is broken, nothing below it is current. */
.conn{color:#FF0000;font-weight:700;font-size:.85rem}
/* A silenced target that looks like a healthy one is a trap: the operator
   reads green and assumes green, when what it means is nobody is watching. */
.muted-badge{font-size:.65rem;border:1px solid var(--muted);color:var(--muted);
  border-radius:3px;padding:0 .3rem;margin-left:.4rem;white-space:nowrap}
.empty{color:var(--muted);padding:2rem 0;text-align:center}
@media(max-width:640px){
  body{padding:.75rem}
  .row summary{grid-template-columns:1fr;gap:.15rem}
  .num{text-align:left}
  .num::before{content:attr(title) ": ";text-transform:uppercase;font-size:.7rem}
  .body{padding-left:.25rem}}
"""

# A dozen lines of fetch, in place of a JavaScript dependency. It preserves
# which rows are open across a swap — replacing innerHTML would otherwise close
# every expanded row every 30 seconds, which is the kind of small wrongness that
# makes a tool feel broken.
#
# A failed poll used to keep the last good render on screen and say nothing.
# That is the freshness banner's own failure mode turned against it: the banner
# only updates when the poll succeeds, so the moment polling breaks it freezes
# mid-sentence and keeps claiming the data is thirty seconds old. #conn lives
# outside #list precisely so a failed poll can still reach it.
_SCRIPT = """
let misses=0;
async function refresh(){
  const open=[...document.querySelectorAll('details[open]')].map(d=>d.dataset.url);
  const conn=document.getElementById('conn');
  try{
    const r=await fetch('/partial/targets',{cache:'no-store'});
    if(!r.ok){
      misses++;
      if(conn)conn.textContent=`⚠ The server answered ${r.status} — ${misses} poll(s) without an update.`;
      return;
    }
    document.getElementById('list').innerHTML=await r.text();
    open.forEach(u=>{
      const d=document.querySelector(`details[data-url="${CSS.escape(u)}"]`);
      if(d)d.open=true;});
    misses=0;
    if(conn)conn.textContent='';
  }catch(e){
    misses++;
    if(conn)conn.textContent=`⚠ No connection to the server — ${misses} failed attempt(s). What is below is the last good data.`;
  }
}
setInterval(refresh, %d000);
"""



#: The target editor. Rendered empty and filled by JS from /api/targets,
#: because every cell is an editable input that has to round-trip back to the
#: API — server-rendering them would mean maintaining two sources of the same
#: values, and the one on screen would win silently.
#:
#: Every value is written with `element.value = x`, never through innerHTML.
#: That makes escaping structural rather than remembered: a target URL
#: containing markup cannot become markup, because it never passes through an
#: HTML parser. Same rule _row() follows on the server side.
_EDITOR = """
<details class="editor" id="editor">
  <summary>Manage targets</summary>
  <div class="ebody">
    <p class="muted" id="esrc"></p>
    <label class="tok">Write token
      <input type="password" id="etok" placeholder="API_WRITE_TOKEN" autocomplete="off">
    </label>
    <div id="erows"></div>
    <div class="eactions">
      <button type="button" id="eadd" disabled>+ Add target</button>
      <button type="button" id="esave" disabled>Save</button>
      <span id="emsg" role="status" aria-live="polite"></span>
    </div>
  </div>
</details>
"""

_EDITOR_STYLE = """
.editor{margin-top:1.5rem;border-top:1px solid var(--line);padding-top:.75rem}
.editor summary{cursor:pointer;color:var(--muted);font-size:.8rem;
  text-transform:uppercase;letter-spacing:.05em}
.ebody{padding:.75rem 0}
.tok{display:block;margin:.5rem 0 1rem;font-size:.8rem;color:var(--muted)}
.tok input{display:block;margin-top:.25rem;width:22rem;max-width:100%}
.erow{display:grid;grid-template-columns:minmax(12rem,1fr) minmax(12rem,1fr) 2rem;
  gap:.4rem;margin-bottom:.4rem;align-items:center}
.erow input[type=text],.erow input[type=number]{width:100%}
.editor input,.editor button{background:var(--card);color:var(--fg);
  border:1px solid var(--line);border-radius:3px;padding:.3rem .4rem;
  font:inherit;font-size:.8rem}
.editor button{cursor:pointer}
.editor input:disabled,.editor button:disabled{opacity:.5;cursor:not-allowed}
.ehead{font-size:.7rem;color:var(--muted);text-transform:uppercase}
.eactions{margin-top:.75rem;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
.erm{color:#FF0000}
#emsg.ok{color:#00C853}
#emsg.bad{color:#FF0000;font-weight:700}
@media(max-width:700px){.erow{grid-template-columns:1fr}}
"""

_EDITOR_SCRIPT = """
const EF=[['expected_status','Expected HTTP status'],['timeout_s','Timeout (s)'],
          ['degraded_ttfb_ms','Slow TTFB (ms)'],['degraded_rtt_ms','High RTT (ms)'],
          ['degraded_backend_ms','Slow backend (ms)']];
// Fields the store accepts that this editor does not render. They are carried
// through a save untouched instead of being dropped.
//
// A save used to send exactly the inputs drawn here, and the API replaces the
// whole list -- so every unrendered field was DELETED by clicking save, with a
// success message and no warning. One click reverted the per-target backend
// threshold calibrated over 920 probes, the trend opt-out measured over 230,
// every maintenance window, and every content assertion. The person who did it
// was editing a timeout.
//
// expect_substring and maintenance stay unrendered on purpose. maintenance is a
// list of objects and needs a repeating sub-form; expect_substring can hold a
// ${VAR} secret reference. Neither is worth the surface here, and neither has
// to be editable for the editor to stop destroying it.
const EKEEP=['expect_substring','maintenance'];
function erow(t){
  const d=document.createElement('div'); d.className='erow';
  EKEEP.forEach(k=>{if(t[k]!=null)d.dataset['keep_'+k]=JSON.stringify(t[k]);});
  const u=document.createElement('input'); u.type='text'; u.dataset.f='url';
  u.placeholder='https://...'; u.value=t.url||''; u.title='Target URL'; d.appendChild(u);
  const advanced=document.createElement('details'); advanced.className='advanced';
  const summary=document.createElement('summary'); summary.textContent='Advanced settings';
  advanced.appendChild(summary); d.appendChild(advanced);
  EF.forEach(([f,ph])=>{const i=document.createElement('input');
    i.type='number'; i.step='any'; i.dataset.f=f; i.placeholder=ph;
    if(t[f]!=null)i.value=t[f];
    const label=document.createElement('label'); label.textContent=ph;
    label.appendChild(i); advanced.appendChild(label);});
  const r=document.createElement('label');
  const c=document.createElement('input'); c.type='checkbox'; c.dataset.f='remind';
  c.checked=!!t.remind; r.appendChild(c);
  r.appendChild(document.createTextNode(' Outage reminders')); advanced.appendChild(r);
  // Defaults to true, so the checkbox starts checked unless the stored value is
  // explicitly false. Reading it as !!t.trend_alerts would turn "not set" into
  // "off" for every target on the first save.
  const rt=document.createElement('label');
  const ct=document.createElement('input'); ct.type='checkbox';
  ct.dataset.f='trend_alerts'; ct.checked=t.trend_alerts!==false;
  rt.appendChild(ct);
  rt.appendChild(document.createTextNode(' Trend alerts')); advanced.appendChild(rt);
  const x=document.createElement('button'); x.type='button'; x.className='erm';
  x.textContent='x'; x.title='Remove'; x.onclick=()=>d.remove(); d.appendChild(x);
  return d;
}
function ecollect(){
  return [...document.querySelectorAll('#erows .erow')].map(d=>{
    const o={};
    // Carried through first, so a rendered field always wins if the two ever
    // name the same key.
    EKEEP.forEach(k=>{const v=d.dataset['keep_'+k];
      if(v!==undefined){try{o[k]=JSON.parse(v);}catch(e){}}});
    d.querySelectorAll('[data-f]').forEach(i=>{
      const f=i.dataset.f;
      if(i.type==='checkbox'){o[f]=i.checked;}
      else if(i.value!==''){o[f]=i.type==='number'?Number(i.value):i.value;}
    });
    return o;
  }).filter(o=>o.url);
}
function emsg(text,ok){
  const m=document.getElementById('emsg');
  m.textContent=text; m.className=ok===true?'ok':ok===false?'bad':'';
}
// Why the server could not show targets.yaml, or null. While it is set the table
// is empty because the file could not be read, not because nothing is monitored,
// and a save would replace the real list with whatever is typed here.
let eseed=null;
// Save sends the WHOLE list and the API replaces what is stored with it, so a
// table that is not the server's list must not be savable. A first load that
// failed used to be abandoned with Save still live on an empty table: add one
// row, press Save, and that row became the entire monitored list.
//
// idle -> loading -> loaded | failed, and only 'loaded' can edit or save. A
// failure stays failed until a later load succeeds, whatever is on screen.
let estate='idle';
// Id of the newest load. An answer carrying an older id is dropped whole, success
// or failure: it describes a request the page has already moved past, and
// painting it would put an old list over a new one.
let eseq=0;
// ecollect() as the last load or save left it. The table differs from this only
// when the person has typed, which is what a reload must never overwrite.
let esnap='';
function eset(next){
  estate=next;
  const ok=next==='loaded';
  document.getElementById('esave').disabled=!ok;
  document.getElementById('eadd').disabled=!ok;
  document.querySelectorAll('#erows input,#erows button').forEach(i=>{i.disabled=!ok;});
}
// The API's own words for a refusal: `error`, plus `store_error` when the stored
// list itself is the problem (present but unusable). Without them the editor
// would open empty and say nothing about why. Only strings are taken, and they
// reach the page through textContent, so markup in them stays text.
function ereason(b){
  if(!b||typeof b!=='object')return '';
  let out=typeof b.error==='string'?b.error:'';
  if(typeof b.store_error==='string'&&b.store_error)
    out+=(out?' ':'')+'('+b.store_error+')';
  return out;
}
function efail(why){
  eset('failed');
  emsg('Could not load the list: '+why+'. Saving is disabled until it loads; close and reopen this panel to try again.',false);
}
async function eload(){
  // Unsaved edits are the only copy of that work. With them on screen there is
  // nothing to fetch over them, and no way to tell which side is newer.
  if(estate==='loaded'&&JSON.stringify(ecollect())!==esnap)return;
  const mine=++eseq;
  eset('loading');
  emsg('Loading the list...',null);
  let d;
  try{
    const r=await fetch('/api/targets',{cache:'no-store'});
    if(mine!==eseq)return;
    if(!r.ok){
      let reason='';
      try{reason=ereason(await r.json());}catch(e){}
      if(mine!==eseq)return;
      efail('the server answered '+r.status+(reason?': '+reason:''));
      return;
    }
    d=await r.json();
  }catch(e){
    if(mine!==eseq)return;
    efail('no connection to the server, or the answer was not JSON');
    return;
  }
  if(mine!==eseq)return;
  if(!d||!Array.isArray(d.targets)||
     !d.targets.every(t=>t&&typeof t==='object'&&typeof t.url==='string')){
    efail('the answer was not a list of targets');
    return;
  }
  try{
    document.getElementById('erows').replaceChildren(...d.targets.map(erow));
  }catch(e){efail('the answer could not be displayed');return;}
  esnap=JSON.stringify(ecollect());
  eseed=d.managed?null:(d.seed_error||null);
  eset('loaded');
  emsg('',null);
  document.getElementById('esrc').textContent = d.managed
    ? 'This list is managed from here. targets.yaml remains as the seed and is no longer read.'
    : 'targets.yaml is still in use and this is its list. On the first save, this list takes over.';
  if(eseed)emsg('Could not display targets.yaml: '+eseed,false);
  if(!d.writable)emsg('Writing is disabled: API_WRITE_TOKEN is missing from .env.',false);
}
async function esave(){
  if(estate!=='loaded')return;
  const tok=document.getElementById('etok').value;
  if(!tok){emsg('The token is missing.',false);return;}
  const rows=ecollect();
  const body={targets:rows};
  // An empty list is refused by the API unless it carries the mark, and the
  // mark is only ever set here, after the person said yes.
  if(rows.length===0){
    if(!confirm('The list is empty. Saving it leaves the monitor with no targets to check. Save the empty list?'))return;
    body.allow_empty=true;
  }else if(eseed&&!confirm('Could not read targets.yaml, so this table is not the list the system is monitoring. Saving replaces it entirely. Continue?')){
    return;
  }
  sessionStorage.setItem('vigilTok',tok);
  try{
    const r=await fetch('/api/targets',{method:'PUT',
      headers:{'Content-Type':'application/json','X-Vigil-Token':tok},
      body:JSON.stringify(body)});
    const d=await r.json();
    if(r.ok){eseed=null;esnap=JSON.stringify(rows);emsg(`Saved: ${d.targets.length} target(s). Applies on the next run.`,true);}
    else{emsg(ereason(d)||`Error ${r.status}`,false);}
  }catch(e){emsg('Could not save: no connection to the server.',false);}
}
eset('idle');
document.getElementById('eadd').onclick=()=>{
  if(estate!=='loaded')return;
  document.getElementById('erows').appendChild(erow({}));};
document.getElementById('esave').onclick=esave;
document.getElementById('editor').addEventListener('toggle',function(){
  if(this.open)eload();});
document.getElementById('etok').value=sessionStorage.getItem('vigilTok')||'';
"""

#: The exact bytes that go inside <script>. Rendered once and reused, because
#: the CSP hash below is computed over this string: formatting it twice would
#: let the two copies drift by a byte, and the browser would then refuse the
#: script and silently stop refreshing the page.
_SCRIPT_RENDERED = (_SCRIPT % POLL_INTERVAL_S) + _EDITOR_SCRIPT


def _csp_hash(content: str) -> str:
    """A `'sha256-...'` source expression for exactly this inline block."""
    digest = hashlib.sha256(content.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


#: The status colours, one class per status, generated from COLOURS so there is
#: still a single place that says what UP looks like.
#:
#: They were an inline `style="color:..."` on every row, and that is the one
#: thing a style-src hash does not admit: without `'unsafe-hashes'` a CSP3
#: browser refuses a style attribute outright and applies none of it. DOWN,
#: DEGRADED and UP rendered in the default text colour, with the glyph and the
#: word as the only difference, while every test over the markup passed.
#: `'unsafe-hashes'` would need one hash per distinct attribute and a change to
#: the header; a class in the stylesheet that is already hashed needs neither.
_STATUS_STYLE = "".join(
    f".st-{status.lower()}{{color:{colour}}}\n" for status, colour in COLOURS.items()
)

#: `unsafe-inline` would let ANY inline script run, including an injected one —
#: which is the single thing CSP exists to stop, so a policy carrying it buys
#: nothing against XSS while reading as if it did. Both blocks here are fixed
#: strings for the life of the process, so a hash computed once is enough and
#: no per-request nonce is needed.
_HISTORY_STYLE = """
a{color:var(--fg);text-underline-offset:.2rem}
.last-check{font-size:.75rem;color:var(--muted)}
.history-filters{display:flex;gap:1rem;flex-wrap:wrap;align-items:end;margin:1rem 0}
.history-filters label,.advanced label{display:block;font-size:.8rem;margin:.4rem 0}
.history-filters input,.history-filters button{display:block;background:var(--card);
 color:var(--fg);border:1px solid var(--line);padding:.4rem;font:inherit}
.history-scroll{overflow:auto}
.history-table{border-collapse:collapse;min-width:100%;font-size:.8rem}
.history-table th,.history-table td{border-bottom:1px solid var(--line);padding:.5rem;
 text-align:left;vertical-align:top;max-width:28rem;overflow-wrap:anywhere}
.advanced{padding:.25rem .5rem;border:1px solid var(--line)}
@media(max-width:900px) and (min-width:641px){
 .row summary{grid-template-columns:8rem minmax(8rem,1fr) 12rem}}
"""
_STYLE_RENDERED = _STYLE + _STATUS_STYLE + _EDITOR_STYLE + _HISTORY_STYLE
STYLE_HASH : str = _csp_hash(_STYLE_RENDERED)
SCRIPT_HASH: str = _csp_hash(_SCRIPT_RENDERED)


def render_page(targets: dict, history: dict, stale_seconds: float | None,
                strips: dict | None = None, muted: dict | None = None) -> str:
    """The full page. Rows come from the same fragment the poll re-fetches."""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>vigil-sre</title>
<style>{_STYLE_RENDERED}</style>
</head><body>
<header>
  <h1>vigil-sre</h1>
  <a href="/history">History / Download CSV</a>
  <span class="muted">{len(targets)} target(s) · refreshes every {POLL_INTERVAL_S}s</span>
  <span id="conn" class="conn" role="status" aria-live="polite"></span>
</header>
<div class="row head"><div class="row"><summary>
  <span>Status</span><span>Service</span><span>Last check (UTC)</span><span>History</span>
</summary></div></div>
<div id="list">{render_rows(targets, history, stale_seconds, strips, muted)}</div>
{_EDITOR}
<script>{_SCRIPT_RENDERED}</script>
</body></html>"""
