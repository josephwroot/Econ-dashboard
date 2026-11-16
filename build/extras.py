"""Builders for the tiles that need their own parsers: the maturity structure of
marketable Treasury debt (MSPD), the average interest rate on marketable debt
(FiscalData), foreign holders of Treasuries by country (TIC), and the IRS
tables of income and income tax by percentile (SOI).

Every builder returns plain dicts for dashboard.json. Parsing is written to
tolerate column renames: columns are found by pattern rather than exact name,
and when a parse fails the error message carries the actual column names or a
dump of the first rows, so the next run can be fixed from the log alone.
"""
import datetime as dt
import io
import os
import re

MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
MONTH_NUM = {m.lower(): i + 1 for i, m in enumerate(MON)}


def _num(v):
    if v in (None, '', 'null', '.', 'n/a', 'N/A'):
        return None
    try:
        return float(str(v).replace(',', '').replace('$', '').strip())
    except ValueError:
        return None


def _pick(keys, *patterns):
    """First key whose lowercase name contains every pattern in one of the tuples."""
    for pat in patterns:
        pats = (pat,) if isinstance(pat, str) else pat
        for k in keys:
            kl = k.lower()
            if all(p in kl for p in pats):
                return k
    return None


# ---------------------------------------------------------------- MSPD: maturity structure
TYPE_ORDER = ['Bills', 'Notes', 'Bonds', 'TIPS', 'Floating rate notes']


def _classify(row):
    text = ' '.join(str(v) for k, v in row.items() if isinstance(v, str) and 'date' not in k).lower()
    if 'inflation' in text or 'tips' in text:
        return 'TIPS'
    if 'floating' in text:
        return 'Floating rate notes'
    if 'bill' in text:
        return 'Bills'
    if 'bond' in text:
        return 'Bonds'
    if 'note' in text:
        return 'Notes'
    return None


def _parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%Y-%m-%dT%H:%M:%S'):
        try:
            return dt.datetime.strptime(s[:19] if 'T' in s else s[:10], fmt).date()
        except ValueError:
            continue
    return None


def mspd_securities(rows, as_of):
    """Turn MSPD table 3 (marketable) rows into (type, maturity date, amount in billions, coupon)."""
    if not rows:
        raise RuntimeError('MSPD table 3: no rows')
    keys = list(rows[0].keys())
    amt_key = _pick(keys, 'outstanding_amt', ('outstanding', 'amt'), ('outstanding',))
    mat_key = _pick(keys, 'maturity_date', ('maturity',))
    rate_key = _pick(keys, 'interest_rate_pct', ('interest_rate',), ('coupon',))
    if not amt_key or not mat_key:
        raise RuntimeError(f'MSPD table 3: could not find amount/maturity columns among {keys}')
    out, skipped_total, skipped = [], 0, {}
    samples = {}
    for r in rows:
        if any(isinstance(v, str) and 'total' in v.lower() for k, v in r.items() if 'date' not in k):
            skipped_total += 1
            continue
        md = _parse_date(r.get(mat_key))
        amt = _num(r.get(amt_key))
        typ = _classify(r)
        reason = 'no maturity date' if md is None else 'no amount' if amt is None else 'zero amount' if amt <= 0 else 'no type' if typ is None else None
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            if reason not in samples:
                samples[reason] = {k: r.get(k) for k in keys if 'desc' in k or 'date' in k or 'amt' in k}
            continue
        out.append((typ, md, amt / 1000.0, _num(r.get(rate_key)) if rate_key else None))
    if not out:
        sample = {k: rows[0].get(k) for k in keys[:14]}
        raise RuntimeError(f'MSPD table 3: parsed 0 securities (skipped {skipped_total} totals, {skipped}); first row {sample}')
    return out, {'amount_col': amt_key, 'maturity_col': mat_key, 'rate_col': rate_key,
                 'n': len(out), 'skipped_total': skipped_total, 'skipped': skipped, 'skipped_samples': samples}


BUCKETS = [('Under 1 year', 0, 1), ('1 to 3 years', 1, 3), ('3 to 5 years', 3, 5),
           ('5 to 10 years', 5, 10), ('10 to 20 years', 10, 20), ('Over 20 years', 20, 999)]


def maturity_summary(secs, as_of):
    total = sum(a for _, _, a, _ in secs)
    years = [((md - as_of).days / 365.25) for _, md, _, _ in secs]
    wam = sum(y * a for y, (_, _, a, _) in zip(years, secs)) / total
    by_type, by_type_y, by_type_c, by_type_ca = {}, {}, {}, {}
    for (t, md, a, c), y in zip(secs, years):
        by_type[t] = by_type.get(t, 0.0) + a
        by_type_y[t] = by_type_y.get(t, 0.0) + y * a
        if c is not None:
            by_type_c[t] = by_type_c.get(t, 0.0) + c * a
            by_type_ca[t] = by_type_ca.get(t, 0.0) + a
    types = [[t, by_type[t], by_type_y[t] / by_type[t],
              (by_type_c[t] / by_type_ca[t]) if by_type_ca.get(t) else None]
             for t in TYPE_ORDER if t in by_type]
    buckets = []
    for label, lo, hi in BUCKETS:
        amt = sum(a for (_, _, a, _), y in zip(secs, years) if lo <= y < hi)
        buckets.append([label, amt, amt / total * 100])
    coupon_num = sum(c * a for _, _, a, c in secs if c is not None)
    coupon_den = sum(a for _, _, a, c in secs if c is not None)
    wall = {}
    for (t, md, a, _) in secs:
        wall.setdefault(md.year, {})
        wall[md.year][t] = wall[md.year].get(t, 0.0) + a
    wall_years = sorted(wall)
    return {
        'total_bn': total, 'wam_years': wam,
        'share_1y': buckets[0][2],
        'bills_share': by_type.get('Bills', 0.0) / total * 100,
        'avg_coupon_pct': (coupon_num / coupon_den) if coupon_den else None,
        'types': types, 'buckets': buckets,
        'wall': {'years': wall_years, 'types': [t for t in TYPE_ORDER if t in by_type],
                 'amounts': [[wall[y].get(t, 0.0) for t in TYPE_ORDER if t in by_type] for y in wall_years]},
    }


def build_maturity(fiscaldata_all, get, fd_base, today):
    ep = '/v1/debt/mspd/mspd_table_3_market'
    j = get(fd_base + ep, {'sort': '-record_date', 'page[size]': 1, 'format': 'json'}).json()
    if not j.get('data'):
        raise RuntimeError('MSPD table 3: empty response')
    latest = j['data'][0]['record_date']
    as_of = _parse_date(latest)
    rows = fiscaldata_all(ep, filt=f'record_date:eq:{latest}')
    secs, info = mspd_securities(rows, as_of)
    cur = maturity_summary(secs, as_of)
    # one December per year for the history
    hist = {'x': [], 'labels': [], 'wam': [], 'share_1y': [], 'bills_share': [], 'total_bn': [], 'avg_coupon_pct': [],
            'bucket_labels': [b[0] for b in BUCKETS], 'bucket_share': [], 'type_labels': TYPE_ORDER, 'type_share': []}

    def push(m, x, label):
        hist['x'].append(x)
        hist['labels'].append(label)
        for k in ('wam', 'share_1y', 'bills_share', 'total_bn', 'avg_coupon_pct'):
            hist[k].append(m[k + '_years'] if k == 'wam' else m[k])
        hist['bucket_share'].append([b[2] for b in m['buckets']])
        tmap = {t[0]: t[1] for t in m['types']}
        hist['type_share'].append([tmap.get(t, 0.0) / m['total_bn'] * 100 for t in TYPE_ORDER])

    for y in range(2001, as_of.year + 1):
        d = dt.date(y, 12, 31)
        if d >= as_of:
            break
        try:
            rs = fiscaldata_all(ep, filt=f'record_date:eq:{d.isoformat()}')
            if not rs:
                continue
            s2, _ = mspd_securities(rs, d)
            m = maturity_summary(s2, d)
        except Exception:  # noqa: BLE001
            continue
        push(m, y + 1.0, f'Dec {y}')
    push(cur, as_of.year + (as_of.month - 1) / 12 + (as_of.day - 1) / 365.25, f'{MON[as_of.month - 1]} {as_of.year}')
    cur.update({'as_of': latest, 'hist': hist, 'parse': info,
                'source_url': 'https://fiscaldata.treasury.gov/datasets/monthly-statement-public-debt/',
                'last_updated': latest})
    return cur


# ---------------------------------------------------------------- FiscalData: average interest rate
def avg_interest_rate(fiscaldata_all):
    rows = fiscaldata_all('/v2/accounting/od/avg_interest_rates', sort='record_date')
    if not rows:
        raise RuntimeError('avg_interest_rates: no rows')
    keys = list(rows[0].keys())
    val_key = _pick(keys, 'avg_interest_rate_amt', ('avg', 'rate'), ('rate',))
    desc_keys = [k for k in keys if k.endswith('desc') or 'desc' in k]
    if not val_key or not desc_keys:
        raise RuntimeError(f'avg_interest_rates: columns {keys}')
    obs = []
    for r in rows:
        text = ' | '.join(str(r.get(k, '')).lower() for k in desc_keys)
        if 'total marketable' in text:
            v = _num(r.get(val_key))
            if v is not None:
                obs.append((r['record_date'], v))
    if not obs:
        seen = sorted({' | '.join(str(r.get(k, '')) for k in desc_keys) for r in rows[-200:]})
        raise RuntimeError(f'avg_interest_rates: no "Total Marketable" rows; descriptions seen: {seen[:30]}')
    last = {}
    for d, v in obs:
        last[d[:7]] = (d, v)
    return [last[k] for k in sorted(last)]


# ---------------------------------------------------------------- TIC: foreign holders by country
def build_foreign_holders(session):
    url = 'https://ticdata.treasury.gov/Publish/mfh.txt'
    r = session.get(url, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f'mfh.txt HTTP {r.status_code}')
    lines = r.text.splitlines()
    months, years = None, None
    for i, ln in enumerate(lines):
        toks = ln.split()
        if months is None and len(toks) >= 10 and all(t[:3].lower() in MONTH_NUM for t in toks):
            months = [MONTH_NUM[t[:3].lower()] for t in toks]
            nxt = lines[i + 1].split() if i + 1 < len(lines) else []
            yrs = [t for t in nxt if re.fullmatch(r'\d{4}', t)]
            if len(yrs) == len(months):
                years = [int(t) for t in yrs]
            break
    if not months or not years:
        raise RuntimeError('mfh.txt: could not find the month/year header; first lines: ' + ' / '.join(lines[:12]))
    n = len(months)
    num = r'-?\d+(?:\.\d+)?'
    pat = re.compile(r'^\s*(.+?)\s+((?:' + num + r'\s+){' + str(n - 1) + r'}' + num + r')\s*$')
    rows, grand = [], None
    for ln in lines:
        m = pat.match(ln)
        if not m:
            continue
        name = re.sub(r'\s+\d+/\s*$', '', m.group(1)).strip()
        vals = [float(x) for x in m.group(2).split()]
        if name.lower().startswith('grand total'):
            grand = vals
            break
        if name.lower() in ('country',) or name.lower().startswith('all other'):
            continue
        rows.append((name, vals))
    if grand is None or not rows:
        raise RuntimeError(f'mfh.txt: parsed {len(rows)} countries, grand total {"found" if grand else "missing"}')
    # columns run newest to oldest; flip to ascending
    order = sorted(range(n), key=lambda i: (years[i], months[i]))
    dates = [dt.date(years[i], months[i], 1) for i in order]
    top = sorted(rows, key=lambda x: -x[1][0])[:10]
    top_names = {nm for nm, _ in top}
    cats = [[nm, vals[0]] for nm, vals in top]
    other = grand[0] - sum(v for _, v in cats)
    cats.append(['All other', other])
    hist_cats = []
    for nm, vals in top:
        hist_cats.append([nm, [vals[i] for i in order]])
    hist_cats.append(['All other', [grand[i] - sum(vals[i] for nm2, vals in rows if nm2 in top_names) for i in order]])
    latest = dates[-1]
    prev_i = 0  # a year earlier is the first column after flipping (13 months)
    return {
        'snapshot': {'period': f'{MON[latest.month - 1]} {latest.year}', 'total': grand[0],
                     'prev_total': grand[order[prev_i]] if n >= 13 else None, 'prev_label': 'vs year ago',
                     'cats': cats, 'as_of': latest.isoformat()},
        'hist': {'x': [d.year + (d.month - 1) / 12 for d in dates], 'labels': [f'{MON[d.month - 1]} {d.year}' for d in dates],
                 'cats': hist_cats, 'gdp': None, 'title': f'Past {n} months'},
        'last_updated': latest.isoformat(), 'countries': len(rows), 'source_url': url,
    }


# ---------------------------------------------------------------- IRS SOI: income and tax by percentile
PCT_RE = re.compile(r'(?:(top|bottom)\s+)?([\d.]+)\s*(?:percent\b|%)', re.I)


def _grid_from_bytes(content, name):
    """Return a list of rows (lists of cell values) from an xls or xlsx file."""
    if name.lower().endswith('.xlsx'):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        return [[c for c in row] for row in ws.iter_rows(values_only=True)]
    import xlrd
    wb = xlrd.open_workbook(file_contents=content)
    ws = wb.sheet_by_index(0)
    return [[ws.cell_value(r, c) for c in range(ws.ncols)] for r in range(ws.nrows)]


def _cell_text(v):
    return re.sub(r'\s+', ' ', str(v)).strip() if isinstance(v, str) else ''


def _header_columns(grid):
    """Find the row(s) whose cells name percentile groups. Returns {col: ('all'|'top'|'bottom', pct)}."""
    best, best_row = {}, None
    for ri, row in enumerate(grid[:80]):
        cols = {}
        for ci, v in enumerate(row):
            t = _cell_text(v)
            m = PCT_RE.search(t)
            if m:
                cols[ci] = ((m.group(1) or 'top').lower(), float(m.group(2)))
            elif re.search(r'all\s+returns', t, re.I):
                cols[ci] = ('all', 0.0)
        if len(cols) > len(best):
            best, best_row = cols, ri
    if len(best) < 3:
        raise RuntimeError('percentile header row not found')
    # header labels can be split over two rows ("Top 1" / "percent"); merge the row above and below
    for ri in (best_row - 1, best_row + 1):
        if 0 <= ri < len(grid):
            for ci, v in enumerate(grid[ri]):
                if ci in best:
                    continue
                t = _cell_text(v)
                m = PCT_RE.search(t)
                if m:
                    best[ci] = ((m.group(1) or 'top').lower(), float(m.group(2)))
    return best, best_row


def _row_label(row):
    for ci in range(min(3, len(row))):
        t = _cell_text(row[ci])
        if t:
            return t
    return ''


def _find_row(grid, start, patterns, stop=None):
    for ri in range(start, len(grid)):
        lab = _row_label(grid[ri]).lower()
        if stop and stop(lab):
            return None
        if any(re.search(p, lab) for p in patterns):
            return ri
    return None


def parse_percentile_table_transposed(grid):
    """Parse the IRS table 4.3 layout: percentile groups in rows ("0.001 percent", "1 percent", ...,
    "All returns, total"), items in columns under a two-level header with merged cells."""
    # percentile rows
    prow = {}
    for ri, row in enumerate(grid):
        t = _cell_text(row[0]) if row else ''
        m = re.match(r'^(?:(top|bottom)\s+)?([\d.]+)\s*percent', t, re.I)
        if m:
            prow[((m.group(1) or 'top').lower(), float(m.group(2)))] = ri
        elif re.match(r'^all returns', t, re.I):
            prow[('all', 0.0)] = ri
    if len(prow) < 4:
        raise RuntimeError('transposed layout: fewer than 4 percentile rows')
    first = min(prow.values())
    # header rows are the rows above the first percentile row with at least two text cells
    # (that leaves out the title and the units line, which sit alone in column A)
    hdr_rows = [r for r in range(max(0, first - 12), first) if sum(1 for c in grid[r] if _cell_text(c)) >= 2]
    ncol = max(len(grid[r]) for r in hdr_rows + list(prow.values()))
    labels = []
    for ci in range(ncol):
        parts = []
        for r in hdr_rows:
            row = grid[r]
            # a blank cell under a merged header inherits the nearest label to the left in that row
            t = _cell_text(row[ci]) if ci < len(row) else ''
            if not t and r == hdr_rows[0]:
                for cj in range(ci - 1, -1, -1):
                    tj = _cell_text(row[cj]) if cj < len(row) else ''
                    if tj:
                        t = tj
                        break
            if t and not re.fullmatch(r'\(?\d+\)?', t):
                parts.append(t.lower())
        labels.append(' | '.join(parts))

    def find_col(*pats):
        for ci, lab in enumerate(labels):
            if all(re.search(p, lab) for p in pats):
                return ci
        return None
    col = {
        'returns': find_col(r'^number of returns'),
        'agi_floor': find_col(r'floor'),
        'agi': find_col(r'adjusted gross income less deficit') if find_col(r'adjusted gross income less deficit') is not None else find_col(r'^adjusted gross income'),
        'avg_rate': find_col(r'average tax rate'),
    }
    # total income tax: the "amount" sub-column if there is one, else the column itself
    tcols = [ci for ci, lab in enumerate(labels) if re.search(r'^total income tax', lab) and not re.search(r'average', lab)]
    tax_col = None
    for ci in tcols:
        if 'amount' in labels[ci]:
            tax_col = ci
            break
    if tax_col is None and tcols:
        tax_col = tcols[-1]
    col['tax'] = tax_col
    if col['tax'] is None or col['agi'] is None:
        raise RuntimeError(f'transposed layout: tax/agi columns not found; all {len(labels)} column labels: {labels}')
    points = {}
    for key_pct, ri in prow.items():
        row = grid[ri]
        pt = {'kind': key_pct[0], 'pct': key_pct[1]}
        for k, ci in col.items():
            pt[k] = _num(row[ci]) if ci is not None and ci < len(row) else None
        points[key_pct] = pt
    allp = points.get(('all', 0.0))
    if not allp or not allp.get('tax'):
        raise RuntimeError('transposed layout: "All returns" row has no total income tax')
    for pt in points.values():
        if pt.get('tax') is not None:
            pt['tax_share'] = pt['tax'] / allp['tax'] * 100
        if pt.get('agi') is not None and allp.get('agi'):
            pt['agi_share'] = pt['agi'] / allp['agi'] * 100
        if pt.get('avg_rate') is None and pt.get('tax') and pt.get('agi'):
            pt['avg_rate'] = pt['tax'] / pt['agi'] * 100
    return points, allp, col, first


def parse_percentile_table(grid):
    """Parse a one-year IRS table: try the transposed 4.3 layout first, then items-in-rows."""
    try:
        return parse_percentile_table_transposed(grid)
    except RuntimeError as e_t:
        err_t = str(e_t)
    try:
        return parse_percentile_table_rows(grid)
    except RuntimeError as e_r:
        raise RuntimeError(f'transposed: {err_t}; rows: {e_r}')


def parse_percentile_table_rows(grid):
    """Items in rows, percentile groups in columns."""
    cols, hrow = _header_columns(grid)
    rows = {}
    specs = [
        ('returns', [r'^number of returns']),
        ('agi_floor', [r'adjusted gross income floor.*current', r'agi floor.*current', r'adjusted gross income floor']),
        ('agi', [r'^adjusted gross income\s*\(', r'^adjusted gross income\s*$', r'^adjusted gross income \[']),
        ('tax', [r'^total income tax\s*\(', r'^total income tax\s*$', r'^total income tax \[']),
        ('agi_share', [r'adjusted gross income share', r'^share of (total )?adjusted gross income', r'agi share']),
        ('tax_share', [r'total income tax share', r'^share of (total )?income tax', r'income tax share']),
        ('avg_rate', [r'average tax rate']),
    ]
    for key, pats in specs:
        ri = _find_row(grid, hrow + 1, pats)
        if ri is not None:
            rows[key] = ri
    if 'tax' not in rows and 'tax_share' not in rows:
        labels = [_row_label(r) for r in grid[hrow + 1:hrow + 60]]
        raise RuntimeError(f'income tax row not found; row labels after header: {labels}')
    points = {}
    for ci, (kind, pct) in cols.items():
        pt = {'kind': kind, 'pct': pct}
        for key, ri in rows.items():
            v = _num(grid[ri][ci]) if ci < len(grid[ri]) else None
            pt[key] = v
        points[(kind, pct)] = pt
    allp = points.get(('all', 0.0))
    for key in ('agi_share', 'tax_share'):
        base = 'agi' if key == 'agi_share' else 'tax'
        for pt in points.values():
            if pt.get(key) is None and pt.get(base) is not None and allp and allp.get(base):
                pt[key] = pt[base] / allp[base] * 100
    for pt in points.values():
        if pt.get('avg_rate') is None and pt.get('tax') and pt.get('agi'):
            pt['avg_rate'] = pt['tax'] / pt['agi'] * 100
    return points, allp, rows, hrow


def parse_panel_table(grid):
    """Parse the multi-year IRS table (4.1 style): sections of items, each with one row per year."""
    cols, hrow = _header_columns(grid)
    sections = [
        ('tax_share', [r'total income tax share', r'income tax share']),
        ('agi_share', [r'adjusted gross income share', r'agi share']),
        ('avg_rate', [r'average tax rate']),
        ('agi_floor', [r'adjusted gross income floor.*current', r'floor.*current']),
        ('returns', [r'^number of returns']),
        ('agi', [r'^adjusted gross income\s*\(']),
        ('tax', [r'^total income tax\s*\(']),
    ]
    out, cur = {}, None
    for ri in range(hrow + 1, len(grid)):
        row = grid[ri]
        lab = _row_label(row)
        ll = lab.lower()
        matched = None
        for key, pats in sections:
            if any(re.search(p, ll) for p in pats):
                matched = key
                break
        if matched and not re.fullmatch(r'\d{4}', lab):
            cur = matched
            continue
        if lab and not re.match(r'\d{4}', lab) and not (isinstance(row[0], (int, float)) and 1990 <= float(row[0]) <= 2100):
            cur = None   # a section we do not use (constant-dollar floors, for instance) until the next known heading
            continue
        yr = None
        if re.fullmatch(r'\d{4}(\s*\[\d+\])?', lab):
            yr = int(lab[:4])
        elif isinstance(row[0], (int, float)) and 1990 <= float(row[0]) <= 2100:
            yr = int(row[0])
        if yr and cur:
            for ci, (kind, pct) in cols.items():
                v = _num(row[ci]) if ci < len(row) else None
                if v is not None:
                    out.setdefault(cur, {}).setdefault((kind, pct), {})[yr] = v
    if 'tax_share' not in out and 'tax' not in out:
        raise RuntimeError('no year rows found under a tax section; sections seen: ' + str(list(out)))
    return out, cols


def _label(kind, pct):
    if kind == 'all':
        return 'All returns'
    p = ('%g' % pct)
    return f'{kind.capitalize()} {p}%'


def build_income_tax_shares(session, today):
    base = 'https://www.irs.gov/pub/irs-soi/'
    tried = []

    def fetch_first(cands):
        for name in cands:
            try:
                r = session.get(base + name, timeout=120)
                tried.append(f'{r.status_code} {name}')
                if r.status_code == 200 and len(r.content) > 5000:
                    return name, r.content
            except Exception as e:  # noqa: BLE001
                tried.append(f'{type(e).__name__} {name}')
        return None, None

    result = {}
    # multi-year panel (table 4.1): the history, and a coarse set of points as a fallback
    cands = []
    for y in range(today.year - 1, today.year - 6, -1):
        yy = f'{y % 100:02d}'
        cands += [f'{yy}in41ts.xlsx', f'{yy}in41ts.xls']
    name2, content2 = fetch_first(cands)
    panel_points, panel_year = None, None
    if content2:
        try:
            grid2 = _grid_from_bytes(content2, name2)
            panel, cols = parse_panel_table(grid2)
            years = sorted({yr for sec in panel.values() for g in sec.values() for yr in g})
            hist = {'years': years, 'groups': []}
            for (kind, pct) in sorted(set(cols.values()), key=lambda kp: (kp[0] != 'top', kp[1])):
                if kind == 'all':
                    continue
                g = {'label': _label(kind, pct), 'kind': kind, 'pct': pct}
                for key in ('tax_share', 'agi_share', 'avg_rate', 'agi_floor', 'returns'):
                    sec = panel.get(key, {}).get((kind, pct), {})
                    g[key] = [sec.get(yr) for yr in years]
                hist['groups'].append(g)
            result['history'] = hist
            result['history_url'] = base + name2
            # latest year with tax shares, as fallback points
            for yr in reversed(years):
                pts = []
                for g in hist['groups']:
                    i = years.index(yr)
                    if g['tax_share'][i] is None:
                        continue
                    pts.append({'label': g['label'], 'kind': g['kind'], 'pct': g['pct'], 'returns': g['returns'][i],
                                'agi_floor': g['agi_floor'][i], 'agi_share': g['agi_share'][i],
                                'tax_share': g['tax_share'][i], 'avg_rate': g['avg_rate'][i]})
                if len(pts) >= 3:
                    panel_points, panel_year = pts, yr
                    break
        except Exception as e:  # noqa: BLE001
            dump = [[_cell_text(c) or (c if isinstance(c, (int, float)) else '') for c in r[:9]] for r in grid2[:45]]
            result['history_error'] = f'{name2}: {e}; grid head: {dump}'
    else:
        result['history_error'] = 'IRS table 4.1 not found; tried ' + ', '.join(tried)

    if panel_points and len(panel_points) >= 8:
        result.update({'year': panel_year, 'points': panel_points, 'source_url': base + name2, 'detail': 'table 4.1'})
        return result

    # expanded one-year table (4.3), newest available
    cands = []
    for y in range(today.year - 1, today.year - 6, -1):
        yy = f'{y % 100:02d}'
        cands += [f'{yy}in43ts.xls', f'{yy}in43ts.xlsx']
    name, content = fetch_first(cands)
    detail_error = None
    if content:
        year = 2000 + int(name[:2])
        grid = _grid_from_bytes(content, name)
        try:
            points, allp, rows, hrow = parse_percentile_table(grid)
            pts = []
            for (kind, pct), pt in sorted(points.items(), key=lambda kv: (kv[0][0] != 'top', kv[0][1])):
                if kind == 'all':
                    continue
                pts.append({'label': _label(kind, pct), 'kind': kind, 'pct': pct,
                            'returns': pt.get('returns'), 'agi_floor': pt.get('agi_floor'),
                            'agi_share': pt.get('agi_share'), 'tax_share': pt.get('tax_share'), 'avg_rate': pt.get('avg_rate')})
            result.update({'year': year, 'points': pts, 'all': allp, 'source_url': base + name, 'parsed_rows': list(rows), 'detail': 'table 4.3'})
        except Exception as e:  # noqa: BLE001
            detail_error = f'{name}: {e}'
    else:
        detail_error = 'IRS table 4.3 not found; tried ' + ', '.join(tried)
    if 'points' not in result:
        if panel_points:
            result.update({'year': panel_year, 'points': panel_points, 'source_url': base + name2, 'detail': 'table 4.1',
                           'detail_error': detail_error})
        else:
            raise RuntimeError(f'detail: {detail_error}; history: {result.get("history_error")}')
    elif detail_error:
        result['detail_error'] = detail_error
    return result


# ---------------------------------------------------------------- BLS OEWS: the most common occupations
# The BLS web site refuses downloads from cloud hosts, so the occupation figures come from the
# BLS public API instead, which carries the Occupational Employment and Wage Statistics as time
# series (one value a year). The API cannot list occupations, so the detailed occupations that
# have been among the largest in recent years are named here; the forty largest among them are
# shown. Codes are the 2018 Standard Occupational Classification as used by the OEWS.
OEWS_GROUPS = {
    '11-0000': 'Management', '13-0000': 'Business and Financial Operations', '15-0000': 'Computer and Mathematical',
    '17-0000': 'Architecture and Engineering', '19-0000': 'Life, Physical, and Social Science',
    '21-0000': 'Community and Social Service', '23-0000': 'Legal', '25-0000': 'Educational Instruction and Library',
    '27-0000': 'Arts, Design, Entertainment, Sports, and Media', '29-0000': 'Healthcare Practitioners and Technical',
    '31-0000': 'Healthcare Support', '33-0000': 'Protective Service', '35-0000': 'Food Preparation and Serving Related',
    '37-0000': 'Building and Grounds Cleaning and Maintenance', '39-0000': 'Personal Care and Service',
    '41-0000': 'Sales and Related', '43-0000': 'Office and Administrative Support', '45-0000': 'Farming, Fishing, and Forestry',
    '47-0000': 'Construction and Extraction', '49-0000': 'Installation, Maintenance, and Repair', '51-0000': 'Production',
    '53-0000': 'Transportation and Material Moving',
}
OEWS_CANDIDATES = {
    '11-1021': 'General and Operations Managers', '11-2022': 'Sales Managers', '11-3021': 'Computer and Information Systems Managers',
    '11-3031': 'Financial Managers', '11-9111': 'Medical and Health Services Managers', '11-9199': 'Managers, All Other',
    '13-1071': 'Human Resources Specialists', '13-1111': 'Management Analysts',
    '13-1161': 'Market Research Analysts and Marketing Specialists', '13-1198': 'Project Management Specialists',
    '13-1199': 'Business Operations Specialists, All Other', '13-2011': 'Accountants and Auditors',
    '15-1232': 'Computer User Support Specialists', '15-1252': 'Software Developers',
    '23-1011': 'Lawyers',
    '25-2021': 'Elementary School Teachers, Except Special Education',
    '25-2022': 'Middle School Teachers, Except Special and Career/Technical Education',
    '25-2031': 'Secondary School Teachers, Except Special and Career/Technical Education',
    '25-3031': 'Substitute Teachers, Short-Term', '25-9045': 'Teaching Assistants, Except Postsecondary',
    '29-1141': 'Registered Nurses', '29-2061': 'Licensed Practical and Licensed Vocational Nurses',
    '31-1120': 'Home Health and Personal Care Aides', '31-1131': 'Nursing Assistants', '31-9092': 'Medical Assistants',
    '33-3051': "Police and Sheriff's Patrol Officers", '33-9032': 'Security Guards',
    '35-1012': 'First-Line Supervisors of Food Preparation and Serving Workers', '35-2011': 'Cooks, Fast Food',
    '35-2014': 'Cooks, Restaurant', '35-2021': 'Food Preparation Workers', '35-3011': 'Bartenders',
    '35-3023': 'Fast Food and Counter Workers', '35-3031': 'Waiters and Waitresses', '35-9021': 'Dishwashers',
    '37-2011': 'Janitors and Cleaners, Except Maids and Housekeeping Cleaners', '37-2012': 'Maids and Housekeeping Cleaners',
    '37-3011': 'Landscaping and Groundskeeping Workers', '39-9011': 'Childcare Workers',
    '41-1011': 'First-Line Supervisors of Retail Sales Workers', '41-2011': 'Cashiers', '41-2031': 'Retail Salespersons',
    '41-3091': 'Sales Representatives of Services, Except Advertising, Insurance, Financial Services, and Travel',
    '41-4012': 'Sales Representatives, Wholesale and Manufacturing, Except Technical and Scientific Products',
    '43-1011': 'First-Line Supervisors of Office and Administrative Support Workers',
    '43-3031': 'Bookkeeping, Accounting, and Auditing Clerks', '43-4051': 'Customer Service Representatives',
    '43-4171': 'Receptionists and Information Clerks', '43-5071': 'Shipping, Receiving, and Inventory Clerks',
    '43-6013': 'Medical Secretaries and Administrative Assistants',
    '43-6014': 'Secretaries and Administrative Assistants, Except Legal, Medical, and Executive', '43-9061': 'Office Clerks, General',
    '47-1011': 'First-Line Supervisors of Construction Trades and Extraction Workers', '47-2031': 'Carpenters',
    '47-2061': 'Construction Laborers', '47-2111': 'Electricians',
    '49-3023': 'Automotive Service Technicians and Mechanics', '49-9071': 'Maintenance and Repair Workers, General',
    '51-1011': 'First-Line Supervisors of Production and Operating Workers', '51-2098': 'Miscellaneous Assemblers and Fabricators',
    '51-9061': 'Inspectors, Testers, Sorters, Samplers, and Weighers',
    '53-1047': 'First-Line Supervisors of Transportation and Material Moving Workers, Except Aircraft Cargo Handling Supervisors',
    '53-3032': 'Heavy and Tractor-Trailer Truck Drivers', '53-3033': 'Light Truck Drivers',
    '53-7051': 'Industrial Truck and Tractor Operators', '53-7062': 'Laborers and Freight, Stock, and Material Movers, Hand',
    '53-7064': 'Packers and Packagers, Hand', '53-7065': 'Stockers and Order Fillers',
}
OEWS_TOTAL = '00-0000'
WAGE_CAP = 239200  # the OEWS does not publish annual wages above this; the API returns the cap


def oews_sid(code, dtype):
    """OEWS series id: OE, U (not seasonally adjusted), N (national), area 0000000, industry 000000
    (all industries), the six-digit occupation code, and the data type (01 employment,
    04 annual mean wage, 13 annual median wage)."""
    return 'OEUN' + '0' * 13 + code.replace('-', '') + dtype


def build_occupations(bls_fn, today, prev=None):
    """Top detailed occupations and the SOC major groups from the OEWS, via the BLS API.
    Annual data: once the year's figures are in hand nothing is fetched until the next
    survey year is due (May estimates come out the following spring)."""
    exp_year = today.year - 1 if today.month >= 4 else today.year - 2
    if prev and prev.get('top') and prev.get('year') and prev.get('source') == 'api' and prev['year'] >= exp_year:
        out = dict(prev)
        out['fetched'] = False
        return out
    codes = [OEWS_TOTAL] + list(OEWS_GROUPS) + list(OEWS_CANDIDATES)
    sids = [oews_sid(c, '01') for c in codes] + [oews_sid(c, '13') for c in codes] + [oews_sid(OEWS_TOTAL, '04')]
    data = {}
    for k in range(0, len(sids), 25):
        data.update(bls_fn(sids[k:k + 25], exp_year - 1, exp_year, annual=True))

    def val(code, dtype, year):
        for y, v in data.get(oews_sid(code, dtype)) or []:
            if y == year:
                return v
        return None
    year = exp_year if val(OEWS_TOTAL, '01', exp_year) else exp_year - 1
    T = val(OEWS_TOTAL, '01', year)
    if not T:
        raise RuntimeError(f'OEWS: no national total for {exp_year} or {exp_year - 1}; {len(data)} series returned')

    def pack(code, title):
        emp = val(code, '01', year)
        if emp is None:
            return None
        med = val(code, '13', year)
        return {'code': code, 'title': title, 'emp': emp, 'share': emp / T * 100,
                'median': med, 'median_capped': bool(med is not None and med >= WAGE_CAP)}
    detailed = [r for r in (pack(c, t) for c, t in OEWS_CANDIDATES.items()) if r]
    groups = [r for r in (pack(c, t) for c, t in OEWS_GROUPS.items()) if r]
    if len(detailed) < 40 or len(groups) < 20:
        raise RuntimeError(f'OEWS {year}: only {len(detailed)} detailed and {len(groups)} group series returned')
    detailed.sort(key=lambda r: -r['emp'])
    groups.sort(key=lambda r: -r['emp'])
    top = detailed[:40]
    cum = 0.0
    for r in top:
        cum += r['share']
        r['cum_share'] = cum
    return {'year': year, 'source': 'api', 'source_url': 'https://www.bls.gov/oes/tables.htm',
            'total_emp': T, 'median_all': val(OEWS_TOTAL, '13', year), 'mean_all': val(OEWS_TOTAL, '04', year),
            'n_detailed': None, 'n_candidates': len(detailed), 'top': top, 'groups': groups,
            'last_updated': None, 'fetched': True}


# ---------------------------------------------------------------- GSS: religious affiliation and attendance
# The General Social Survey (NORC, since 1972) is the standard source for long-run trends in
# religious identity. The cumulative Stata file is large (hundreds of megabytes unpacked), so it
# is downloaded at most every few months and the shares are cached in the data file between runs.
GSS_URL = 'https://gss.norc.org/content/dam/gss/get-the-data/documents/stata/GSS_stata.zip'
# RELIG codes: 1 Protestant, 2 Catholic, 3 Jewish, 4 none, 5 other, and from 1998 on 6 Buddhism, 7 Hinduism,
# 8 other eastern, 9 Islam, 10 Orthodox Christian, 11 Christian, 12 Native American, 13 inter-/nondenominational.
# DENOM codes (Protestants only): 10-19 Baptist, 20-29 Methodist, 30-39 Lutheran, 40-49 Presbyterian,
# 50 Episcopal, 60 other, 70 no denomination. Latter-day Saints are found through the OTHER variable's labels.
GSS_CATS = [  # stacking order, bottom to top
    'Protestant and other Christian', 'Catholic', 'Jewish', 'Muslim', 'Hindu', 'Buddhist', 'Other religion', 'No religion',
]
GSS_FAMILIES = [  # the non-Catholic Christians by denominational family, for the detail view
    'Baptist', 'Methodist', 'Lutheran', 'Presbyterian', 'Episcopal', 'Other Protestant', 'Nondenominational',
    'Latter-day Saints and Orthodox',
]
GSS_SMALL = [  # the religions the GSS has coded separately since 1998, as shares of all adults
    ('Muslim', {9}), ('Buddhist', {6}), ('Hindu', {7}), ('Orthodox Christian', {10}),
    ('Other eastern religions', {8}), ('Native American', {12}), ('Other', {5}),
]
GSS_SMALL_FROM = 1998
GSS_ATTEND = [  # ATTEND codes: 0 never ... 7 every week, 8 several times a week
    ('Weekly or more', {7, 8}), ('Monthly to nearly weekly', {4, 5, 6}),
    ('A few times a year or less', {1, 2, 3}), ('Never', {0}),
]
GSS_REFRESH_DAYS = 120
GSS_METHOD = 'wtssps-first/8cats/labels3'  # bump to force a re-read after a change in how the shares are computed


def build_gss_religion(session, today, prev=None):
    """Weighted shares of adults by religious preference and by attendance, every GSS year."""
    import tempfile
    import zipfile
    prev = prev or {}
    cached_ok = (prev.get('snapshot') and prev.get('hist') and prev.get('fetched_on') and not prev.get('stale') and prev.get('method') == GSS_METHOD
                 and all(v is not None for _, v in prev['snapshot']['cats'])
                 and all(v is not None for _, vals in prev['hist']['cats'] for v in vals))
    if cached_ok:
        try:
            age = (today - dt.date.fromisoformat(prev['fetched_on'])).days
        except ValueError:
            age = 10 ** 6
        if age < GSS_REFRESH_DAYS:
            out = {k: prev[k] for k in ('snapshot', 'hist', 'extras', 'fetched_on', 'release', 'last_updated', 'updated_text', 'next', 'source_url', 'n_latest', 'weights', 'unweighted_years', 'method') if k in prev}
            out['fetched'] = False
            return out
    import pandas as pd  # imported here so the rest of the build does not need it

    tmp = tempfile.mkdtemp(prefix='gss')
    zpath = os.path.join(tmp, 'gss.zip')
    with session.get(GSS_URL, stream=True, timeout=900, headers={'Accept': '*/*'}) as r:
        if r.status_code != 200:
            raise RuntimeError(f'GSS download HTTP {r.status_code}')
        with open(zpath, 'wb') as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    size = os.path.getsize(zpath)
    with open(zpath, 'rb') as f:
        if f.read(2) != b'PK':
            raise RuntimeError(f'GSS download is not a zip ({size} bytes)')
    with zipfile.ZipFile(zpath) as z:
        dtas = sorted((m for m in z.infolist() if m.filename.lower().endswith('.dta')), key=lambda m: -m.file_size)
        if not dtas:
            raise RuntimeError('GSS zip holds no .dta file: ' + ', '.join(m.filename for m in z.infolist()[:10]))
        member = dtas[0]
        dpath = z.extract(member, tmp)
    os.remove(zpath)
    release = os.path.basename(member.filename)
    mrel = re.match(r'gss(\d\d)(\d\d)_?r?(\w*)', release.lower())
    if mrel:
        release = f'19{mrel.group(1)}\u201320{mrel.group(2)}' + (f', release {mrel.group(3)}' if mrel.group(3) else '')
    with pd.io.stata.StataReader(dpath, convert_categoricals=False) as rdr:
        names = {v.lower(): v for v in rdr.variable_labels()}
        need = {'year': names.get('year'), 'relig': names.get('relig'), 'attend': names.get('attend')}
        opt = {'denom': names.get('denom'), 'other': names.get('other')}
        # value labels, so that denominations are matched by name rather than by code numbers
        lds_codes, denom_fam, lds_note = set(), {}, ''
        diag = {}
        try:
            vls = rdr.value_labels()
            lbl = getattr(rdr, '_lbllist', None) or getattr(rdr, 'lbllist', None)
            vl = getattr(rdr, '_varlist', None) or getattr(rdr, 'varlist', None)

            def labels_for(var):
                # the label set attached to the variable first (several variables share label
                # names in the GSS file), then one that merely carries the variable's name
                out = []
                if lbl and vl and var in list(vl):
                    out.append(vls.get(lbl[list(vl).index(var)]) or {})
                for key in (var, str(var).lower(), str(var).upper()):
                    if key in vls and vls[key] not in out:
                        out.append(vls[key])
                return out
            labs = labels_for(opt['other']) if opt['other'] else []
            for lab in labs:
                lds_codes |= {int(k) for k, v in lab.items() if re.search(r'latter.?day|mormon|\blds\b', str(v), re.I)}
            diag['lds_labels'] = {int(k): str(v) for lab in labs for k, v in lab.items() if int(k) in lds_codes}

            def fam_of(text):
                t = str(text).lower()
                return ('Baptist' if 'bapt' in t else 'Methodist' if 'meth' in t else 'Lutheran' if 'luth' in t
                        else 'Presbyterian' if 'presb' in t else 'Episcopal' if 'episcopal' in t
                        else 'Nondenominational' if 'no denom' in t or 'nondenom' in t or 'non-denom' in t
                        else 'Other Protestant' if t.strip() == 'other' or t.startswith('other') else None)
            denom_candidates = []
            for lab in (labels_for(opt['denom']) if opt['denom'] else []):
                m = {int(k): fam_of(v) for k, v in lab.items() if fam_of(v)}
                if m:
                    denom_candidates.append((f'labels ({len(m)} codes)', m))
        except Exception as e:  # noqa: BLE001
            lds_note = f'{type(e).__name__}: {e}'
            denom_candidates = []
        # documented codings as fallbacks: the seven-category DENOM, and the detailed DENOM16 ranges
        denom_candidates.append(('seven codes', {1: 'Baptist', 2: 'Methodist', 3: 'Lutheran', 4: 'Presbyterian', 5: 'Episcopal',
                                                 6: 'Other Protestant', 7: 'Nondenominational'}))
        denom_candidates.append(('code ranges', {**{c: 'Baptist' for c in range(10, 20)}, **{c: 'Methodist' for c in range(20, 30)},
                                                 **{c: 'Lutheran' for c in range(30, 40)}, **{c: 'Presbyterian' for c in range(40, 50)},
                                                 50: 'Episcopal', 60: 'Other Protestant', 70: 'Nondenominational'}))
        # weights, coalesced row by row: the post-stratification weight WTSSPS, which NORC computes
        # the same way for every year including the web-era waves, then the nonresponse-adjusted
        # WTSSNRPS (2004 on), then the original WTSSALL
        wcols = [names[w] for w in ('wtssps', 'wtsscomp', 'wtssnrps', 'wtssall', 'wtss') if w in names]
        missing = [k for k, v in need.items() if not v]
        if missing:
            raise RuntimeError(f'GSS file lacks {missing}; has {len(names)} variables')
        optcols = [v for v in (opt['denom'], opt['other']) if v]
        cols = [need['year'], need['relig'], need['attend']] + optcols + wcols
        df = rdr.read(columns=cols)
    df.columns = ['year', 'relig', 'attend'] + [k for k in ('denom', 'other') if opt[k]] + [f'w{i}' for i in range(len(wcols))]
    for k in ('denom', 'other'):
        if k not in df:
            df[k] = -1.0
    try:
        os.remove(dpath)
    except OSError:
        pass
    df = df.apply(pd.to_numeric, errors='coerce')
    # NORC stores missing-value codes as negatives in some releases and as very large integers in
    # others; either way nothing meaningful is above 100000
    for c in ('relig', 'attend', 'denom', 'other'):
        df[c] = df[c].where((df[c] >= 0) & (df[c] < 100000))
    df = df[df['year'] > 1900]
    # pick the denomination coding that covers the codes actually present among Protestants
    prot_codes = df.loc[df['relig'] == 1, 'denom'].dropna()
    denom_fam, diag['denom_source'] = {}, 'none'
    for label, cand in denom_candidates:
        cover = float(prot_codes.isin(list(cand)).mean()) if len(prot_codes) else 0.0
        diag.setdefault('denom_coverage', {})[label] = round(cover, 3)
        if cover >= 0.8:
            denom_fam, diag['denom_source'] = cand, label
            break
    w = pd.Series(float('nan'), index=df.index)
    for i in range(len(wcols)):
        c = df[f'w{i}']
        w = w.where(w > 0, c.where(c > 0))
    df['w'] = w.fillna(0.0)
    years = sorted(int(y) for y in df['year'].dropna().unique())
    # a wave with no usable weight at all is tabulated unweighted rather than dropped
    unweighted = [y for y in years if df.loc[df['year'] == y, 'w'].sum() <= 0]
    for y in unweighted:
        df.loc[df['year'] == y, 'w'] = 1.0

    # one category per respondent
    import numpy as np
    r, dn, ot = df['relig'], df['denom'], df['other']
    lds = ot.isin(lds_codes) if lds_codes else pd.Series(False, index=df.index)
    fam = dn.map(denom_fam)
    conds = [
        lds & r.isin([1, 5, 11, 13]),   # Latter-day Saints answer Protestant, other, or Christian; the write-in settles it
        r == 4, r == 2, r == 3, r == 9, r == 7, r == 6, r.isin([5, 8, 12]), r == 10,
        r.isin([11, 13]),
        (r == 1) & fam.notna(), r == 1,
    ]
    choices = [
        'Latter-day Saints and Orthodox',
        'No religion', 'Catholic', 'Jewish', 'Muslim', 'Hindu', 'Buddhist', 'Other religion', 'Latter-day Saints and Orthodox',
        'Nondenominational',
        fam.fillna(''), 'Other Protestant',
    ]
    df['fam'] = np.select(conds, choices, default='')
    df['cat'] = df['fam'].where(~df['fam'].isin(GSS_FAMILIES), 'Protestant and other Christian')
    # diagnostics for the log: how the latest wave's Protestants are spread over denomination codes
    lastyr = df['year'].max()
    pr = df[(df['year'] == lastyr) & (df['relig'] == 1)]
    diag['denom_latest'] = {int(k): round(float(v), 1) for k, v in pr['denom'].value_counts().head(12).items()}
    diag['cats_latest'] = {k: int(v) for k, v in df.loc[df['year'] == lastyr, 'cat'].value_counts().items()}

    def shares(col, groups, valid_mask=None):
        valid = df[valid_mask] if valid_mask is not None else df[df[col].notna()]
        out = {name: [] for name, _ in groups}
        ns = []
        for y in years:
            d = valid[valid['year'] == y]
            tot = d['w'].sum()
            ns.append(int(len(d)))
            for name, codes in groups:
                out[name].append(round(float(d.loc[d[col].isin(codes), 'w'].sum()) / float(tot) * 100, 2) if tot else None)
        return out, ns
    GSS_RELIG = [(name, {name}) for name in GSS_CATS]
    rel, n_rel = shares('cat', GSS_RELIG, df['cat'] != '')
    fams, _ = shares('fam', [(name, {name}) for name in GSS_FAMILIES], df['cat'] != '')
    att, n_att = shares('attend', GSS_ATTEND)
    small, n_small = shares('relig', GSS_SMALL)
    small_idx = [i for i, y in enumerate(years) if y >= GSS_SMALL_FROM]
    # the attendance item was not asked in every year; drop years with too few answers
    att_years = [y for y, n in zip(years, n_att) if n >= 300]
    att_idx = [years.index(y) for y in att_years]
    latest = years[-1]
    li = len(years) - 1
    # comparison about a decade earlier (the survey is biennial)
    pi = max(i for i, y in enumerate(years) if y <= latest - 10)
    none_now = rel['No religion'][li]
    none_then = rel['No religion'][pi]
    snapshot = {'period': str(latest), 'total': 100.0, 'prev_total': None, 'prev_label': f'vs {years[pi]}',
                'cats': [[name, rel[name][li]] for name, _ in GSS_RELIG], 'as_of': f'{latest}-01-01', 'memo': None,
                'headline': {'name': 'No religion', 'value': none_now, 'prev': none_then, 'delta': 'pt'}}
    hist = {'x': [float(y) for y in years], 'labels': [str(y) for y in years],
            'cats': [[name, rel[name]] for name, _ in GSS_RELIG], 'gdp': None, 'title': f'Since {years[0]}'}
    extras = [{'title': 'Protestants and other Christians by denominational family', 'cats': [{'name': name} for name in GSS_FAMILIES],
               'hist': {'x': [float(y) for y in years], 'labels': [str(y) for y in years],
                        'cats': [[name, fams[name]] for name in GSS_FAMILIES], 'gdp': None},
               'modes': ['level'], 'vfmt': 'pct', 'mode_labels': {'level': 'Share of all adults'},
               'note': 'The five families the survey asks about by name, the remaining denominations together (Pentecostals, Churches of Christ, Adventists, Jehovah\u2019s Witnesses and others), Protestants naming no denomination or answering simply "Christian", and Latter-day Saints (from the write-in) with, since 1998, Orthodox Christians.'},
              {'title': f'Religions coded separately since {GSS_SMALL_FROM}', 'cats': [{'name': name} for name, _ in GSS_SMALL],
               'hist': {'x': [float(years[i]) for i in small_idx], 'labels': [str(years[i]) for i in small_idx],
                        'cats': [[name, [small[name][i] for i in small_idx]] for name, _ in GSS_SMALL], 'gdp': None},
               'modes': ['level'], 'vfmt': 'pct', 'mode_labels': {'level': 'Share of all adults'},
               'note': f'Before {GSS_SMALL_FROM} the survey recorded all of these as "other"; since then each is coded on its own. Each is a small share of adults, so the sampling error on any one year is a few tenths of a point. "Other" is the residual write-ins.'},
              {'title': 'How often people attend religious services', 'cats': [{'name': name} for name, _ in GSS_ATTEND],
               'hist': {'x': [float(y) for y in att_years], 'labels': [str(y) for y in att_years],
                        'cats': [[name, [att[name][i] for i in att_idx]] for name, _ in GSS_ATTEND], 'gdp': None},
               'modes': ['share'], 'vfmt': 'pct', 'mode_labels': {'share': 'Share of adults'},
               'note': 'Answers to "How often do you attend religious services?", grouped. Self-reported attendance runs well above counts taken at the door.'}]
    return {'snapshot': snapshot, 'hist': hist, 'extras': extras, 'n_latest': n_rel[li],
            'weights': wcols, 'unweighted_years': unweighted, 'method': GSS_METHOD, 'lds_codes': sorted(lds_codes), 'lds_note': lds_note, 'diag': diag,
            'release': release, 'source_url': GSS_URL, 'fetched_on': today.isoformat(),
            'last_updated': today.isoformat() + ' 00:00:00-05',
            'updated_text': f'{today.strftime("%b %-d, %Y")} (GSS {release}, latest wave {latest})',
            'next': f'The {latest + 2} survey, expected in {latest + 3}', 'fetched': True}
