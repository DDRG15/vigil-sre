"""Read-only history browsing and spreadsheet export over the retained samples."""
from __future__ import annotations

import csv
import html
import io
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import dashboard
from history import _connect_read_only, _is_missing_schema, _retention_days_from_env

COLUMNS = ('checked_at', 'url', 'status', 'http_status', 'dns_ms', 'rtt_ms',
           'tls_ms', 'ttfb_ms', 'server_processing_ms', 'error', 'finding_codes')
PAGE_SIZE = 100
DAILY_COLUMNS = (
    'day_utc', 'url', 'observations', 'up_samples', 'slow_samples',
    'down_samples', 'other_samples', 'recorded_available_pct', 'ttfb_samples',
    'p50_ttfb_ms', 'p95_ttfb_ms', 'first_checked_at', 'last_checked_at',
    'max_observed_gap_s',
)


def filters(query: dict[str, list[str]]) -> dict:
    """Inclusive UTC calendar dates; defaults follow the retention setting."""
    if any(len(v) != 1 for v in query.values()):
        raise ValueError('Use each filter only once.')
    now = datetime.now(timezone.utc)
    today = now.date()
    def value(key, default):
        return query.get(key, [default])[0]
    try:
        start = date.fromisoformat(value('start', (today-timedelta(days=_retention_days_from_env())).isoformat()))
        end = date.fromisoformat(value('end', today.isoformat()))
        page = int(value('page', '1'))
    except (ValueError, OverflowError):
        raise ValueError('Use valid UTC dates and a positive page number.') from None
    if start > end or end > today or page < 1 or page > 1_000_000:
        raise ValueError('Choose an ordered date range ending no later than today and a positive page.')
    # Retention is a time window, even while the writer is stopped and has not pruned.
    cutoff = now-timedelta(days=_retention_days_from_env())
    view = value('view', 'samples')
    if view not in ('samples', 'daily'):
        raise ValueError('Choose the samples or daily view.')
    since = max(datetime.combine(start,time.min,tzinfo=timezone.utc),cutoff)
    until = datetime.combine(end+timedelta(days=1),time.min,tzinfo=timezone.utc)
    return {'start':start.isoformat(),'end':end.isoformat(),'target':value('target',''),'view':view,
            'page':page,'since':since.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'until':until.strftime('%Y-%m-%dT%H:%M:%SZ')}


def _where(f):
    sql = 'p.checked_at >= ? AND p.checked_at < ?'
    params = [f['since'],f['until']]
    if f['target']:
        sql += ' AND p.url = ?'
        params.append(f['target'])
    return sql, params


def samples(db: Path, f: dict, *, paginate: bool = True):
    """Yield raw samples with bounded buffering; no writes or new database files."""
    if not db.exists():
        return
    where,params = _where(f)
    sql = '''SELECT p.checked_at,p.url,p.status,p.http_status,p.dns_ms,p.rtt_ms,
        p.tls_ms,p.ttfb_ms,p.server_processing_ms,p.error,
        (SELECT group_concat(code, ', ') FROM probe_findings WHERE probe_id=p.id)
        FROM probe_results p WHERE ''' + where + ' ORDER BY p.checked_at DESC,p.id DESC'
    if paginate:
        sql += ' LIMIT ? OFFSET ?'
        params += [PAGE_SIZE,(f['page']-1)*PAGE_SIZE]
    with closing(_connect_read_only(db)) as c:
        try:
            cursor = c.execute(sql,params)
            while True:
                rows = cursor.fetchmany(250)
                if not rows:
                    return
                for row in rows:
                    yield dict(zip(COLUMNS,row))
        except sqlite3.OperationalError as exc:
            if _is_missing_schema(exc):
                return
            raise


def coverage(db: Path,f: dict) -> tuple[int,str | None,str | None]:
    if not db.exists():
        return 0,None,None
    where,params=_where(f)
    with closing(_connect_read_only(db)) as c:
        try:
            return c.execute('SELECT count(*),min(p.checked_at),max(p.checked_at) FROM probe_results p WHERE '+where,params).fetchone()
        except sqlite3.OperationalError as exc:
            if _is_missing_schema(exc):
                return 0,None,None
            raise


def daily_summaries(db: Path, f: dict, *, paginate: bool = True):
    """Aggregate selected observations by UTC day and exact target, read-only.

    Gaps use the preceding selected observation of the same target, including
    across midnight. They say nothing about time outside the observed span.
    SQLite sorts/aggregates the raw rows; Python buffers only one result batch.
    """
    if not db.exists():
        return
    where, params = _where(f)
    sql = '''WITH selected AS (
        SELECT p.id,p.checked_at,p.url,p.status,p.ttfb_ms
        FROM probe_results p WHERE ''' + where + '''
    ), timed AS (
        SELECT *,substr(checked_at,1,10) AS day_utc,
            (julianday(checked_at)-julianday(lag(checked_at) OVER (
                PARTITION BY url ORDER BY checked_at,id)))*86400 AS gap_s
        FROM selected
    ), latency AS (
        SELECT day_utc,url,ttfb_ms,cume_dist() OVER (
            PARTITION BY day_utc,url ORDER BY ttfb_ms) AS cd
        FROM timed WHERE typeof(ttfb_ms) IN ('integer','real')
            AND ttfb_ms >= 0 AND ttfb_ms <= 1.7976931348623157e308
    ), percentiles AS (
        SELECT day_utc,url,count(*) AS ttfb_samples,
            min(CASE WHEN cd >= 0.50 THEN ttfb_ms END) AS p50,
            min(CASE WHEN cd >= 0.95 THEN ttfb_ms END) AS p95
        FROM latency GROUP BY day_utc,url
    )
    SELECT t.day_utc,t.url,count(*),sum(status='UP'),sum(status='DEGRADED'),
        sum(status='DOWN'),sum(status NOT IN ('UP','DEGRADED','DOWN')),
        round(100.0*sum(status IN ('UP','DEGRADED')) /
            nullif(sum(status IN ('UP','DEGRADED','DOWN')),0),2),
        coalesce(l.ttfb_samples,0),l.p50,l.p95,
        min(checked_at),max(checked_at),round(max(gap_s),3)
    FROM timed t LEFT JOIN percentiles l ON t.day_utc=l.day_utc AND t.url=l.url
    GROUP BY t.day_utc,t.url ORDER BY t.day_utc DESC,t.url'''
    if paginate:
        sql += ' LIMIT ? OFFSET ?'
        params += [PAGE_SIZE, (f['page']-1)*PAGE_SIZE]
    with closing(_connect_read_only(db)) as con:
        try:
            cursor = con.execute(sql, params)
            while rows := cursor.fetchmany(250):
                for row in rows:
                    yield dict(zip(DAILY_COLUMNS, row))
        except sqlite3.OperationalError as exc:
            if _is_missing_schema(exc):
                return
            raise


def daily_summary_count(db: Path, f: dict) -> int:
    if not db.exists():
        return 0
    where, params = _where(f)
    with closing(_connect_read_only(db)) as con:
        try:
            return con.execute('SELECT count(*) FROM (SELECT 1 FROM probe_results p WHERE '
                + where + ' GROUP BY substr(p.checked_at,1,10),p.url)', params).fetchone()[0]
        except sqlite3.OperationalError as exc:
            if _is_missing_schema(exc):
                return 0
            raise


def _csv_chunks(rows, columns):
    """UTF-8 BOM for Excel; text cells cannot become spreadsheet formulas."""
    yield '\ufeff'.encode('utf-8')
    buffer=io.StringIO(newline='')
    writer=csv.writer(buffer)
    writer.writerow(columns)
    yield buffer.getvalue().encode('utf-8')
    for sample in rows:
        buffer.seek(0); buffer.truncate(0)
        row=[]
        for key in columns:
            value=sample[key]
            if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@','\t','\r','\n')):
                value="'"+value
            row.append(value)
        writer.writerow(row)
        yield buffer.getvalue().encode('utf-8')


def csv_chunks(db: Path, f: dict):
    yield from _csv_chunks(samples(db, f, paginate=False), COLUMNS)


def daily_csv_chunks(db: Path, f: dict):
    yield from _csv_chunks(daily_summaries(db, f, paginate=False), DAILY_COLUMNS)


def render(db: Path,f: dict) -> str:
    count,first,last=coverage(db,f)
    escape=lambda v: html.escape(str(v if v is not None else ''),quote=True)
    daily = f.get('view') == 'daily'
    view = 'daily' if daily else 'samples'
    query = {k:f[k] for k in ('start','end','target')}
    query['view'] = view
    download=('/history-daily.csv?' if daily else '/history.csv?')+urlencode(query)
    other_view = dict(query, view='samples' if daily else 'daily')
    switch = '/history?' + urlencode(other_view)
    columns = DAILY_COLUMNS if daily else COLUMNS
    rows = daily_summaries(db, f) if daily else samples(db, f)
    table=[]
    for row in rows:
        table.append('<tr>'+''.join('<td>'+escape(row[k])+'</td>' for k in columns)+'</tr>')
    page=f['page']
    row_count = daily_summary_count(db, f) if daily else count
    links=[]
    for label,n in [('Previous',page-1),('Next',page+1)]:
        if (label=='Previous' and page>1) or (label=='Next' and page*PAGE_SIZE<row_count):
            links.append('<a href="/history?'+escape(urlencode(dict(query,page=n)))+'">'+label+'</a>')
    summary=f'{count} observation'+('' if count==1 else 's')
    if count:
        summary+=' · First: '+escape(first)+' · Last: '+escape(last)
    else:
        summary+=' · No observations in this selection.'
    headers=('Checked at (UTC)','Target','Recorded status','HTTP','DNS ms','RTT ms',
             'TLS ms','TTFB ms','Backend ms','Detail','Findings')
    if daily:
        headers = ('Day (UTC)', 'Target', 'Observations', 'Recorded UP', 'Recorded Slow',
            'Recorded DOWN', 'Other', 'Recorded non-DOWN %', 'TTFB samples',
            'TTFB p50 ms', 'TTFB p95 ms', 'First check (UTC)', 'Last check (UTC)',
            'Largest observed gap s')
    heading = 'Daily summary' if daily else 'Raw observations'
    limitations = ('''<p class="muted">Daily rows summarize observations, not incidents or notification deliveries.
Recorded non-DOWN % counts UP and Slow among known recorded states; it is not time-based uptime.
A failed probe awaiting DOWN confirmation can still be recorded UP or Slow.
TTFB percentiles use nearest rank over finite, nonnegative measurements; sample counts show their support.
Largest gap compares consecutive selected checks of the same target, assigned to the later UTC day.
Blank means no measurement. Missing periods and time before the first or after the last check are not success.
No thresholds are changed automatically.</p>''' if daily else '')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>History · vigil-sre</title>
<style>{dashboard._STYLE_RENDERED}</style></head><body>
<header><h1>History · vigil-sre</h1><a href="/">Dashboard</a></header>
<p>{heading} · UTC · retention: {_retention_days_from_env()} days. Missing periods are not successful checks.</p>
<p><a href="{escape(switch)}">{'Raw observations' if daily else 'Daily summary'}</a></p>
<form method="get" action="/history" class="history-filters">
<input type="hidden" name="view" value="{view}">
<label>From (UTC) <input type="date" name="start" value="{escape(f['start'])}" required></label>
<label>Through (UTC) <input type="date" name="end" value="{escape(f['end'])}" required></label>
<label>Target URL <input name="target" type="text" value="{escape(f['target'])}" placeholder="All targets"></label>
<button type="submit">View history</button><a href="{escape(download)}">Download CSV</a></form>
<p role="status">{summary}</p>
{limitations}
<p class="muted">Showing up to {PAGE_SIZE} {'daily rows' if daily else 'observations'} per page. CSV includes the entire selection.
Downloads are separate copies and remain on your device after live data expires.</p>
<div class="history-scroll"><table class="history-table"><thead><tr>{''.join('<th scope="col">'+h+'</th>' for h in headers)}</tr></thead>
<tbody>{''.join(table)}</tbody></table></div><nav class="history-filters">{' '.join(links)}</nav>
</body></html>'''
