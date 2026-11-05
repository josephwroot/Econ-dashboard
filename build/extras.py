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
            if not t:
                for cj in range(ci - 1, -1, -1):
                    tj = _cell_text(row[cj]) if cj < len(row) else ''
                    if tj:
                        if re.search(r'number of returns|amount|percent|floor|income|tax|wages|credit', tj, re.I):
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
    tcols = [ci for ci, lab in enumerate(labels) if re.search(r'total income tax', lab) and not re.search(r'average|credit', lab)]
    tax_col = None
    for ci in tcols:
        if 'amount' in labels[ci]:
            tax_col = ci
            break
    if tax_col is None and tcols:
        tax_col = tcols[-1]
    col['tax'] = tax_col
    if col['tax'] is None or col['agi'] is None:
        raise RuntimeError(f'transposed layout: tax/agi columns not found; column labels: {labels[:40]}')
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

    # expanded one-year table (4.3), newest available
    cands = []
    for y in range(today.year - 1, today.year - 6, -1):
        yy = f'{y % 100:02d}'
        cands += [f'{yy}in43ts.xls', f'{yy}in43ts.xlsx']
    name, content = fetch_first(cands)
    if not content:
        raise RuntimeError('IRS table 4.3 not found; tried ' + ', '.join(tried))
    year = 2000 + int(name[:2])
    grid = _grid_from_bytes(content, name)
    try:
        points, allp, rows, hrow = parse_percentile_table(grid)
    except Exception as e:  # noqa: BLE001
        dump = [[_cell_text(c) or (c if isinstance(c, (int, float)) else '') for c in r[:10]] for r in grid[:40]]
        raise RuntimeError(f'{name}: {e}; grid head: {dump}')
    pts = []
    for (kind, pct), pt in sorted(points.items(), key=lambda kv: (kv[0][0] != 'top', kv[0][1])):
        if kind == 'all':
            continue
        pts.append({'label': _label(kind, pct), 'kind': kind, 'pct': pct,
                    'returns': pt.get('returns'), 'agi_floor': pt.get('agi_floor'),
                    'agi_share': pt.get('agi_share'), 'tax_share': pt.get('tax_share'), 'avg_rate': pt.get('avg_rate')})
    result = {'year': year, 'points': pts, 'all': allp, 'source_url': base + name, 'parsed_rows': list(rows)}

    # multi-year panel (4.1) for the history
    cands = []
    for y in range(today.year - 1, today.year - 6, -1):
        yy = f'{y % 100:02d}'
        cands += [f'{yy}in41ts.xlsx', f'{yy}in41ts.xls']
    name2, content2 = fetch_first(cands)
    if content2:
        try:
            grid2 = _grid_from_bytes(content2, name2)
            panel, cols = parse_panel_table(grid2)
            years = sorted({yr for sec in panel.values() for g in sec.values() for yr in g})
            hist = {'years': years, 'groups': []}
            for (kind, pct) in sorted(cols.values(), key=lambda kp: (kp[0] != 'top', kp[1])):
                if kind == 'all':
                    continue
                g = {'label': _label(kind, pct), 'kind': kind, 'pct': pct}
                for key in ('tax_share', 'agi_share', 'avg_rate', 'agi_floor'):
                    sec = panel.get(key, {}).get((kind, pct), {})
                    g[key] = [sec.get(yr) for yr in years]
                hist['groups'].append(g)
            result['history'] = hist
            result['history_url'] = base + name2
        except Exception as e:  # noqa: BLE001
            dump = [[_cell_text(c) or (c if isinstance(c, (int, float)) else '') for c in r[:9]] for r in grid2[:45]]
            result['history_error'] = f'{name2}: {e}; grid head: {dump}'
    else:
        result['history_error'] = 'IRS table 4.1 not found; tried ' + ', '.join(tried)
    return result
