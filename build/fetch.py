#!/usr/bin/env python3
"""Fetch every indicator in manifest.yaml and write data/dashboard.json plus CSVs.

Fail-soft: a source that fails keeps its previous data (from the last successful
run) and is marked stale with the error message. The run only fails outright if
nothing at all could be fetched and there is no previous data to fall back on.
"""
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import traceback
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
CSVDIR = os.path.join(DATA, 'csv')
MANIFEST = os.path.join(ROOT, 'manifest.yaml')
FRED_KEY = os.environ.get('FRED_API_KEY', '').strip()
ET = ZoneInfo('America/New_York')
NOW = dt.datetime.now(dt.timezone.utc)
TODAY_ET = NOW.astimezone(ET).date()
MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
FRED = 'https://api.stlouisfed.org/fred'
FD = 'https://api.fiscaldata.treasury.gov/services/api/fiscal_service'

S = requests.Session()
S.headers['User-Agent'] = 'econ-dashboard (https://github.com/josephwroot/econ-dashboard)'


def log(*a):
    print(*a, flush=True)


def get(url, params=None, tries=3, timeout=90):
    last = None
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            last = f'HTTP {r.status_code}: {r.text[:200]}'
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(f'GET {url} failed: {last}')


# ---------------------------------------------------------------- FRED
_meta_cache = {}


def fred_obs(sid):
    if FRED_KEY:
        j = get(f'{FRED}/series/observations',
                {'series_id': sid, 'api_key': FRED_KEY, 'file_type': 'json'}).json()
        out = [(o['date'], float(o['value'])) for o in j['observations'] if o['value'] not in ('.', '')]
    else:
        r = get('https://fred.stlouisfed.org/graph/fredgraph.csv', {'id': sid})
        out = []
        for row in csv.DictReader(io.StringIO(r.text)):
            d = row.get('observation_date') or row.get('DATE')
            v = row.get(sid)
            if d and v not in (None, '.', ''):
                out.append((d, float(v)))
    if not out:
        raise RuntimeError(f'FRED {sid}: no observations')
    return out


def fred_meta(sid):
    if not FRED_KEY:
        return {}
    if sid in _meta_cache:
        return _meta_cache[sid]
    m = {}
    try:
        s = get(f'{FRED}/series', {'series_id': sid, 'api_key': FRED_KEY, 'file_type': 'json'}).json()['seriess'][0]
        m = {'last_updated': s.get('last_updated'), 'units': s.get('units'),
             'frequency': s.get('frequency'), 'title': s.get('title')}
        rel = get(f'{FRED}/series/release', {'series_id': sid, 'api_key': FRED_KEY, 'file_type': 'json'}).json()['releases'][0]
        m['release_id'] = rel['id']
        m['release_name'] = rel['name']
    except Exception as e:  # noqa: BLE001
        m['meta_error'] = repr(e)
    _meta_cache[sid] = m
    return m


def fred_release_dates(start, end):
    if not FRED_KEY:
        return []
    j = get(f'{FRED}/releases/dates',
            {'api_key': FRED_KEY, 'file_type': 'json', 'realtime_start': start.isoformat(),
             'realtime_end': end.isoformat(), 'include_release_dates_with_no_data': 'true',
             'limit': 1000, 'sort_order': 'asc'}).json()
    return j.get('release_dates', [])


# ---------------------------------------------------------------- transforms
def per_year(freq):
    return {'M': 12, 'Q': 4, 'A': 1}[freq]


def apply_transform(obs, src, freq):
    t = src.get('transform')
    if not t:
        return obs
    if t == 'scale':
        f = float(src['factor'])
        return [(d, v * f) for d, v in obs]
    if t == 'yoy_pct':
        n = per_year(freq)
        return [(obs[i][0], (obs[i][1] / obs[i - n][1] - 1) * 100) for i in range(n, len(obs)) if obs[i - n][1]]
    if t == 'diff':
        return [(obs[i][0], obs[i][1] - obs[i - 1][1]) for i in range(1, len(obs))]
    if t == 'month_end':
        f = float(src.get('factor', 1))
        last = {}
        for d, v in obs:
            last[d[:7]] = (d, v * f)
        return [last[k] for k in sorted(last)]
    raise ValueError(f'unknown transform {t}')


# ---------------------------------------------------------------- FiscalData
def fiscaldata_all(endpoint, fields=None, filt=None, sort=None):
    rows, page = [], 1
    while True:
        params = {'page[size]': 10000, 'page[number]': page, 'format': 'json'}
        if fields:
            params['fields'] = fields
        if filt:
            params['filter'] = filt
        if sort:
            params['sort'] = sort
        j = get(FD + endpoint, params).json()
        rows += j.get('data', [])
        if page >= int(j.get('meta', {}).get('total-pages', 1) or 1):
            break
        page += 1
    return rows


def debt_to_penny():
    rows = fiscaldata_all('/v2/accounting/od/debt_to_penny', fields='record_date,tot_pub_debt_out_amt', sort='record_date')
    obs = [(r['record_date'], float(r['tot_pub_debt_out_amt'])) for r in rows
           if r.get('tot_pub_debt_out_amt') not in (None, 'null', '')]
    if not obs:
        raise RuntimeError('Debt to the Penny: no rows')
    return obs


def norm(s):
    return re.sub(r'\s+', ' ', (s or '').strip().rstrip(':').strip().lower())


_mts_cache = None


def mts_table9():
    global _mts_cache
    if _mts_cache is None:
        _mts_cache = fiscaldata_all('/v1/accounting/mts/mts_table_9', sort='record_date')
        if not _mts_cache:
            raise RuntimeError('MTS table 9: no rows')
    return _mts_cache


def build_comp(cfg, gdp_fy):
    rows = mts_table9()
    groups = {}
    for r in rows:
        groups.setdefault(r.get('record_type_cd'), []).append(r)
    marker = norm(cfg['marker'])
    code = None
    for c, rs in groups.items():
        if any(norm(r.get('classification_desc')) == marker for r in rs):
            code = c
            break
    if code is None:
        raise RuntimeError(f'MTS table 9: no record_type group contains "{cfg["marker"]}"; groups: {list(groups)}')
    rs = [r for r in groups[code] if r.get('data_type_cd') != 'S']
    by_date = {}
    for r in rs:
        by_date.setdefault(r['record_date'], []).append(r)
    dates = sorted(by_date)

    def amounts(date, col):
        m = {}
        for r in by_date[date]:
            v = r.get(col)
            if v in (None, 'null', ''):
                continue
            m[norm(r.get('classification_desc'))] = float(v)
        return m

    def categorize(m):
        total = None
        for k, v in m.items():
            if k.startswith('total'):
                total = v
                break
        if total is None:
            total = sum(v for k, v in m.items())
        matched, cats = 0.0, []
        for c in cfg['cats']:
            if c.get('residual'):
                continue
            val = sum(m.get(norm(x), 0.0) for x in c['match'])
            cats.append([c['name'], val / 1e9])
            matched += val
        for c in cfg['cats']:
            if c.get('residual'):
                cats.append([c['name'], (total - matched) / 1e9])
        return cats, total / 1e9

    latest = dates[-1]
    r0 = by_date[latest][0]
    fy = int(r0['record_fiscal_year'])
    mon = int(r0['record_calendar_month'])
    cur_cats, cur_total = categorize(amounts(latest, 'current_fytd_rcpt_outly_amt'))
    _, prev_total = categorize(amounts(latest, 'prior_fytd_rcpt_outly_amt'))
    period = f'Fiscal {fy}' if mon == 9 else f'Fiscal {fy} to date, Oct\u2013{MON[mon - 1]}'
    years, hist = [], {c['name']: [] for c in cfg['cats']}
    for d in dates:
        rr = by_date[d][0]
        if int(rr['record_calendar_month']) != 9:
            continue
        cats, _ = categorize(amounts(d, 'current_fytd_rcpt_outly_amt'))
        years.append(int(rr['record_fiscal_year']))
        for name, val in cats:
            hist[name].append(val)
    return {
        'snapshot': {'period': period, 'fy': fy, 'through_month': mon, 'total': cur_total,
                     'prev_total': prev_total, 'cats': cur_cats, 'as_of': latest},
        'hist': {'years': years, 'cats': [[n, hist[n]] for n in hist],
                 'gdp': {str(y): gdp_fy.get(y) for y in years}},
        'last_updated': latest,
    }


# ---------------------------------------------------------------- Census
def census_nim():
    base = 'https://www2.census.gov/programs-surveys/popest/datasets'
    y = TODAY_ET.year
    cands20 = []
    for v in range(y, 2020, -1):
        for folder in ('state', 'national'):
            cands20 += [f'{base}/2020-{v}/{folder}/totals/NST-EST{v}-ALLDATA.csv',
                        f'{base}/2020-{v}/{folder}/totals/nst-est{v}-alldata.csv']
    cands10 = []
    for folder in ('state', 'national'):
        cands10 += [f'{base}/2010-2020/{folder}/totals/nst-est2020int-alldata.csv',
                    f'{base}/2010-2020/{folder}/totals/NST-EST2020INT-ALLDATA.csv',
                    f'{base}/2010-2019/{folder}/totals/nst-est2019-alldata.csv']
    tried = []

    modified = {}

    def load(cands):
        for u in cands:
            try:
                r = S.get(u, timeout=90, headers={'Accept': 'text/csv,*/*'})
                tried.append(f'{r.status_code} {u}')
                if r.status_code == 200 and 'INTERNATIONALMIG' in r.text[:50000]:
                    modified[u] = r.headers.get('Last-Modified')
                    return u, r.text
            except Exception as e:  # noqa: BLE001
                tried.append(f'{type(e).__name__} {u}')
        return None, None

    def us_row(text):
        for row in csv.DictReader(io.StringIO(text)):
            if (row.get('SUMLEV') or '').strip() in ('010', '10') or (row.get('NAME') or '').strip() == 'United States':
                return row
        raise RuntimeError('US row not found')

    def nim_cols(row):
        out = {}
        for k, v in row.items():
            m = re.match(r'INTERNATIONALMIG(\d{4})$', (k or '').strip())
            if m and v not in (None, ''):
                out[int(m.group(1))] = float(v)
        return out

    u20, t20 = load(cands20)
    if not t20:
        raise RuntimeError('Census NST-EST totals file not found for the 2020s; tried: ' + '; '.join(tried))
    u10, t10 = load(cands10)
    vals = {}
    part_a = None
    if t10:
        v10 = nim_cols(us_row(t10))
        v10.pop(2010, None)          # April to June 2010 only
        part_a = v10.pop(2020, None)  # July 2019 to March 2020
        vals.update(v10)
    v20 = nim_cols(us_row(t20))
    part_b = v20.pop(2020, None)      # April to June 2020
    vals.update(v20)
    if part_a is not None and part_b is not None:
        vals[2020] = part_a + part_b
    if not vals:
        raise RuntimeError('Census file parsed but no INTERNATIONALMIG columns found')
    obs = [(f'{yr}-01-01', vals[yr]) for yr in sorted(vals)]
    lm = modified.get(u20)
    last_updated = None
    if lm:
        try:
            last_updated = dt.datetime.strptime(lm, '%a, %d %b %Y %H:%M:%S %Z').date().isoformat()
        except ValueError:
            last_updated = None
    vintage = re.search(r'EST(\d{4})', u20, re.I)
    return obs, {'source_url': u20, 'source_url_2010s': u10, 'last_updated': last_updated,
                 'release_name': f'Vintage {vintage.group(1)} population estimates' if vintage else 'Population estimates',
                 'next_text': f'Vintage {TODAY_ET.year if TODAY_ET.month < 12 else TODAY_ET.year + 1} estimates, expected around the turn of the year'}


# ---------------------------------------------------------------- sources
def fetch_source(src, freq):
    t = src['type']
    if t == 'fred':
        obs = apply_transform(fred_obs(src['series']), src, freq)
        return obs, fred_meta(src['series'])
    if t == 'ratio':
        num = fred_obs(src['num'])
        dmap = {}
        for s in src['den']:
            for d, v in fred_obs(s):
                dmap[d] = dmap.get(d, 0.0) + v
        f = float(src.get('factor', 1))
        obs = [(d, v / dmap[d] * f) for d, v in num if dmap.get(d)]
        return obs, fred_meta(src['num'])
    if t == 'splice':
        parts = [fetch_source(p, freq) for p in src['parts']]
        obs, meta = list(parts[0][0]), parts[-1][1]
        for pobs, _ in parts[1:]:
            if pobs:
                cutoff = pobs[0][0]
                obs = [o for o in obs if o[0] < cutoff] + list(pobs)
        return obs, meta
    if t == 'fiscaldata_dtp':
        obs = apply_transform(debt_to_penny(), src, freq)
        return obs, {'last_updated': obs[-1][0] + ' 00:00:00-05', 'release_name': 'Debt to the Penny', 'daily': True}
    if t == 'census_nim':
        return census_nim()
    raise ValueError(f'unknown source type {t}')


# ---------------------------------------------------------------- helpers
def fmt_date(s):
    if not s:
        return None
    try:
        d = dt.date.fromisoformat(s[:10])
        return f'{MON[d.month - 1]} {d.day}, {d.year}'
    except ValueError:
        return s


def recessions():
    obs = fred_obs('USREC')
    out, start = [], None
    for d, v in obs:
        if v == 1 and start is None:
            start = d
        if v == 0 and start is not None:
            out.append([start, d])
            start = None
    if start:
        out.append([start, obs[-1][0]])
    return out


def gdp_by_fiscal_year(gdp_obs_trillions):
    q = {d: v * 1000 for d, v in gdp_obs_trillions}  # billions
    out = {}
    years = sorted({int(d[:4]) for d in q})
    for y in years:
        keys = [f'{y - 1}-10-01', f'{y}-01-01', f'{y}-04-01', f'{y}-07-01']
        vals = [q[k] for k in keys if k in q]
        if len(vals) == 4:
            out[y] = sum(vals) / 4
    return out


def load_previous():
    p = os.path.join(DATA, 'dashboard.json')
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return None
    return None


def main():
    with open(MANIFEST) as f:
        man = yaml.safe_load(f)
    os.makedirs(CSVDIR, exist_ok=True)
    prev = load_previous() or {}
    prev_series = {s['id']: s for s in prev.get('series', [])}
    prev_comp = {c['id']: c for c in prev.get('comp', [])}
    status = {'generated_at': NOW.isoformat(timespec='seconds'), 'ok': [], 'stale': {}, 'failed': {}, 'notes': []}
    if not FRED_KEY:
        status['notes'].append('FRED_API_KEY not set: using keyless CSV export; no metadata or release calendar')

    out_series = []
    for cfg in man['series']:
        sid = cfg['id']
        entry = {k: v for k, v in cfg.items() if k != 'source'}
        entry['sid'] = cfg.get('sid_override') or (cfg['source'].get('series') if cfg['source']['type'] == 'fred' else '')
        if not entry.get('url'):
            entry['url'] = f'https://fred.stlouisfed.org/series/{entry["sid"]}' if entry['sid'] else ''
        try:
            obs, meta = fetch_source(cfg['source'], cfg['freq'])
            obs = sorted(obs)
            entry['obs'] = [[d, round(v, 6)] for d, v in obs]
            entry['meta'] = meta
            entry['updated'] = fmt_date(meta.get('last_updated'))
            entry['updated_raw'] = meta.get('last_updated')
            entry['release_id'] = meta.get('release_id')
            entry['release_name'] = meta.get('release_name')
            entry['daily'] = bool(meta.get('daily'))
            entry['stale'] = False
            entry['error'] = None
            status['ok'].append(sid)
            log(f'ok    {sid:14s} {len(obs):5d} obs, last {obs[-1][0]} = {obs[-1][1]:.4g}')
            with open(os.path.join(CSVDIR, f'{sid}.csv'), 'w', newline='') as f:
                w = csv.writer(f)
                w.writerow(['date', 'value'])
                w.writerows(obs)
        except Exception as e:  # noqa: BLE001
            err = f'{type(e).__name__}: {e}'
            log(f'FAIL  {sid:14s} {err}')
            traceback.print_exc()
            old = prev_series.get(sid)
            if old and old.get('obs'):
                entry.update({k: old.get(k) for k in ('obs', 'meta', 'updated', 'updated_raw', 'release_id', 'release_name', 'daily')})
                entry['stale'] = True
                entry['error'] = err
                entry['stale_since'] = old.get('stale_since') or fmt_date(TODAY_ET.isoformat())
                status['stale'][sid] = err
            else:
                entry['obs'] = []
                entry['stale'] = True
                entry['error'] = err
                status['failed'][sid] = err
        out_series.append(entry)

    # GDP by fiscal year for the composition charts
    gdp_fy = {}
    gdp_entry = next((s for s in out_series if s['id'] == 'gdp_nominal'), None)
    if gdp_entry and gdp_entry.get('obs'):
        gdp_fy = gdp_by_fiscal_year(gdp_entry['obs'])

    out_comp = []
    for cfg in man.get('comp', []):
        cid = cfg['id']
        entry = {k: v for k, v in cfg.items()}
        entry['kind'] = 'comp'
        try:
            entry.update(build_comp(cfg, gdp_fy))
            entry['updated'] = fmt_date(entry['last_updated'])
            entry['stale'] = False
            entry['error'] = None
            status['ok'].append(cid)
            log(f'ok    {cid:14s} snapshot {entry["snapshot"]["period"]}, total {entry["snapshot"]["total"]:.1f}B, hist {entry["hist"]["years"]}')
            with open(os.path.join(CSVDIR, f'{cid}_by_fiscal_year.csv'), 'w', newline='') as f:
                w = csv.writer(f)
                w.writerow(['fiscal_year'] + [n for n, _ in entry['hist']['cats']] + ['gdp_billions'])
                for i, y in enumerate(entry['hist']['years']):
                    w.writerow([y] + [round(vals[i], 3) for _, vals in entry['hist']['cats']] + [entry['hist']['gdp'].get(str(y))])
        except Exception as e:  # noqa: BLE001
            err = f'{type(e).__name__}: {e}'
            log(f'FAIL  {cid:14s} {err}')
            traceback.print_exc()
            old = prev_comp.get(cid)
            if old and old.get('snapshot'):
                entry.update({k: old.get(k) for k in ('snapshot', 'hist', 'last_updated', 'updated')})
                entry['stale'] = True
                entry['error'] = err
                entry['stale_since'] = old.get('stale_since') or fmt_date(TODAY_ET.isoformat())
                status['stale'][cid] = err
            else:
                entry['stale'] = True
                entry['error'] = err
                status['failed'][cid] = err
        out_comp.append(entry)

    # Recessions
    try:
        rec = recessions()
    except Exception as e:  # noqa: BLE001
        rec = prev.get('recessions', [])
        status['notes'].append(f'recessions: {e}')

    # Release calendar: this week and next (from Monday, ET) and the next release per series
    releases_week, next_by_release = [], {}
    try:
        monday = TODAY_ET - dt.timedelta(days=TODAY_ET.weekday())
        rd = fred_release_dates(monday, TODAY_ET + dt.timedelta(days=75))
        our_ids = {s.get('release_id') for s in out_series if s.get('release_id')}
        extra = {norm(x) for x in man.get('extra_releases', [])}
        for r in rd:
            d = dt.date.fromisoformat(r['date'])
            rid, name = r.get('release_id'), r.get('release_name', '')
            if d >= TODAY_ET and rid not in next_by_release:
                next_by_release[rid] = r['date']
            if 'h.15' in name.lower():
                continue
            if monday <= d <= monday + dt.timedelta(days=13) and (rid in our_ids or norm(name) in extra):
                releases_week.append({'date': r['date'], 'name': name, 'release_id': rid})
        seen, dedup = set(), []
        for r in releases_week:
            key = (r['date'], r['name'])
            if key not in seen:
                seen.add(key)
                dedup.append(r)
        releases_week = sorted(dedup, key=lambda r: (r['date'], r['name']))
    except Exception as e:  # noqa: BLE001
        status['notes'].append(f'release calendar: {e}')
        releases_week = prev.get('releases', [])
    for s in out_series:
        if s.get('daily'):
            s['next'] = 'Every business day'
        elif (s.get('meta') or {}).get('next_text'):
            s['next'] = s['meta']['next_text']
        elif 'h.15' in (s.get('release_name') or '').lower():
            s['next'] = 'Posted every business day; the monthly average settles after month end'
        elif s.get('release_id') in next_by_release:
            s['next'] = fmt_date(next_by_release[s['release_id']])
        else:
            s['next'] = s.get('next') or ''
    for c in out_comp:
        c['next'] = 'Around the 8th business day of each month'

    now_et = NOW.astimezone(ET)
    dash = {
        'title': man.get('title', 'Economic dashboard'),
        'generated_at': NOW.isoformat(timespec='seconds'),
        'generated_label': now_et.strftime('%a, %b %-d, %Y, %-I:%M %p ET').replace('AM', 'am').replace('PM', 'pm'),
        'groups': man['groups'],
        'series': out_series,
        'comp': out_comp,
        'recessions': rec,
        'releases': releases_week,
        'week_start': (TODAY_ET - dt.timedelta(days=TODAY_ET.weekday())).isoformat(),
        'today': TODAY_ET.isoformat(),
        'status': {'ok': len(status['ok']), 'stale': list(status['stale']), 'failed': list(status['failed'])},
    }
    with open(os.path.join(DATA, 'dashboard.json'), 'w') as f:
        json.dump(dash, f, separators=(',', ':'))
    with open(os.path.join(DATA, 'status.json'), 'w') as f:
        json.dump(status, f, indent=1)
    log(f'\nwrote dashboard.json: {len(status["ok"])} ok, {len(status["stale"])} stale, {len(status["failed"])} failed')
    if status['failed']:
        log('failed:', json.dumps(status['failed'], indent=1))
    if not status['ok'] and not prev:
        sys.exit(1)


if __name__ == '__main__':
    main()
