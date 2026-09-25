"""Federal tax parameters used for the take-home curves.

One year per decade plus the current year, for a SINGLE filer whose income is
all wages, who takes the standard deduction, and who claims no credits. State
taxes are excluded. Employee-side payroll taxes (Social Security and Medicare)
are included in the take-home calculation.

Sources, transcribed by hand:
  Income tax brackets and rates: Tax Foundation, "Historical U.S. Federal
    Individual Income Tax Rates and Brackets, 1862-2025" (nominal dollars).
  Standard deduction and personal exemption: IRS Statistics of Income,
    historical Table 23 and the year's Form 1040 instructions.
  Payroll tax rates and wage bases: Social Security Administration,
    "Contribution and Benefit Base" and "Social Security and Medicare Tax
    Rates" tables.

Simplifications, deliberately: no earned income credit (small for a single
filer without children), no phase-out of the personal exemption in 1991-2017,
no alternative minimum tax, no Additional Medicare Tax before 2013, no state
tax. Bracket thresholds are the lower bounds of taxable income at which each
rate begins.
"""

# 1971 onward singles have their own schedule (Tax Reform Act of 1969). Before
# that, single filers used the same rates as joint filers on half the brackets.
TAXLAW = {
    1955: {
        'label': '1950s', 'law': 'Internal Revenue Code of 1954',
        'brackets': [(0, 20), (2000, 22), (4000, 26), (6000, 30), (8000, 34), (10000, 38), (12000, 43),
                     (14000, 47), (16000, 50), (18000, 53), (20000, 56), (22000, 59), (26000, 62),
                     (32000, 65), (38000, 69), (44000, 72), (50000, 75), (60000, 78), (70000, 81),
                     (80000, 84), (90000, 87), (100000, 89), (150000, 90), (200000, 91)],
        'std': {'pct': 0.10, 'max': 1000, 'min': 0}, 'exemption': 600, 'credit': 0,
        'payroll': {'oasdi': 2.0, 'oasdi_base': 4200, 'hi': 0.0, 'hi_base': None, 'addl': 0.0, 'addl_over': None},
    },
    1965: {
        'label': '1960s', 'law': 'Revenue Act of 1964',
        'brackets': [(0, 14), (500, 15), (1000, 16), (1500, 17), (2000, 19), (4000, 22), (6000, 25),
                     (8000, 28), (10000, 32), (12000, 36), (14000, 39), (16000, 42), (18000, 45),
                     (20000, 48), (22000, 50), (26000, 53), (32000, 55), (38000, 58), (44000, 60),
                     (50000, 62), (60000, 64), (70000, 66), (80000, 68), (90000, 69), (100000, 70)],
        'std': {'pct': 0.10, 'max': 1000, 'min': 300}, 'exemption': 600, 'credit': 0,
        'payroll': {'oasdi': 3.625, 'oasdi_base': 4800, 'hi': 0.0, 'hi_base': None, 'addl': 0.0, 'addl_over': None},
    },
    1975: {
        'label': '1970s', 'law': 'Tax Reform Act of 1969 schedule, Tax Reduction Act of 1975 credit',
        'brackets': [(0, 14), (500, 15), (1000, 16), (1500, 17), (2000, 19), (4000, 21), (6000, 24),
                     (8000, 25), (10000, 27), (12000, 29), (14000, 31), (16000, 34), (18000, 36),
                     (20000, 38), (22000, 40), (26000, 45), (32000, 50), (38000, 55), (44000, 60),
                     (50000, 62), (60000, 64), (70000, 66), (80000, 68), (90000, 69), (100000, 70)],
        'std': {'pct': 0.16, 'max': 2300, 'min': 1600}, 'exemption': 750, 'credit': 30,
        'payroll': {'oasdi': 4.95, 'oasdi_base': 14100, 'hi': 0.90, 'hi_base': 14100, 'addl': 0.0, 'addl_over': None},
    },
    1985: {
        'label': '1980s', 'law': 'Economic Recovery Tax Act of 1981, indexed',
        # the zero-bracket amount is built into the schedule, so no separate standard deduction
        'brackets': [(0, 0), (2390, 11), (3540, 12), (4580, 14), (6760, 15), (8850, 16), (11240, 18),
                     (13430, 20), (15610, 23), (18940, 26), (24460, 30), (29970, 34), (35490, 38),
                     (43190, 42), (57550, 48), (85130, 50)],
        'std': {'pct': 0, 'max': 0, 'min': 0}, 'exemption': 1040, 'credit': 0,
        'payroll': {'oasdi': 5.7, 'oasdi_base': 39600, 'hi': 1.35, 'hi_base': 39600, 'addl': 0.0, 'addl_over': None},
    },
    1995: {
        'label': '1990s', 'law': 'Omnibus Budget Reconciliation Act of 1993',
        'brackets': [(0, 15), (23350, 28), (56550, 31), (117950, 36), (256500, 39.6)],
        'std': {'fixed': 3900}, 'exemption': 2500, 'credit': 0,
        'payroll': {'oasdi': 6.2, 'oasdi_base': 61200, 'hi': 1.45, 'hi_base': None, 'addl': 0.0, 'addl_over': None},
    },
    2005: {
        'label': '2000s', 'law': 'Jobs and Growth Tax Relief Reconciliation Act of 2003',
        'brackets': [(0, 10), (7300, 15), (29700, 25), (71950, 28), (150150, 33), (326450, 35)],
        'std': {'fixed': 5000}, 'exemption': 3200, 'credit': 0,
        'payroll': {'oasdi': 6.2, 'oasdi_base': 90000, 'hi': 1.45, 'hi_base': None, 'addl': 0.0, 'addl_over': None},
    },
    2015: {
        'label': '2010s', 'law': 'American Taxpayer Relief Act of 2012',
        'brackets': [(0, 10), (9225, 15), (37450, 25), (90750, 28), (189300, 33), (411500, 35), (413200, 39.6)],
        'std': {'fixed': 6300}, 'exemption': 4000, 'credit': 0,
        'payroll': {'oasdi': 6.2, 'oasdi_base': 118500, 'hi': 1.45, 'hi_base': None, 'addl': 0.9, 'addl_over': 200000},
    },
    2026: {
        'label': '2020s', 'law': 'Tax Cuts and Jobs Act of 2017 as made permanent in 2025, indexed for 2026',
        'brackets': [(0, 10), (12400, 12), (50400, 22), (105700, 24), (201775, 32), (256225, 35), (640600, 37)],
        'std': {'fixed': 16100}, 'exemption': 0, 'credit': 0,
        'payroll': {'oasdi': 6.2, 'oasdi_base': 184500, 'hi': 1.45, 'hi_base': None, 'addl': 0.9, 'addl_over': 200000},
    },
}

# Approximate distribution of tax returns by adjusted gross income, tax year
# 2022 (IRS Statistics of Income, Table 1.1), used only to translate a change
# in the rate schedule into a change in revenue. Counts in millions of returns,
# income in billions of dollars.
INCOME_CLASSES = [
    # (lower bound, upper bound or None, returns, total AGI)
    (1, 10000, 20.3, 105),
    (10000, 25000, 27.5, 480),
    (25000, 50000, 36.5, 1330),
    (50000, 75000, 22.5, 1380),
    (75000, 100000, 14.6, 1265),
    (100000, 200000, 24.6, 3400),
    (200000, 500000, 9.6, 2740),
    (500000, 1000000, 1.6, 1070),
    (1000000, None, 0.9, 2390),
]
INCOME_CLASSES_YEAR = 2022


def taxable_income(y, law):
    std = law['std']
    if 'fixed' in std:
        ded = std['fixed']
    else:
        ded = max(min(std['pct'] * y, std['max']), std['min'])
    return max(0.0, y - ded - law['exemption'])


def income_tax(y, law, brackets=None):
    """Federal income tax before credits on wage income y, single filer."""
    br = brackets or law['brackets']
    ti = taxable_income(y, law)
    tax = 0.0
    for k, (lo, rate) in enumerate(br):
        hi = br[k + 1][0] if k + 1 < len(br) else float('inf')
        if ti > lo:
            tax += (min(ti, hi) - lo) * rate / 100
    return max(0.0, tax - law.get('credit', 0))


def marginal_rate(y, law, brackets=None):
    br = brackets or law['brackets']
    ti = taxable_income(y, law)
    rate = 0.0
    for lo, r in br:
        if ti > lo:
            rate = r
    return rate


def payroll_tax(y, law):
    p = law['payroll']
    t = p['oasdi'] / 100 * min(y, p['oasdi_base'])
    t += p['hi'] / 100 * (min(y, p['hi_base']) if p['hi_base'] else y)
    if p.get('addl') and p.get('addl_over') and y > p['addl_over']:
        t += p['addl'] / 100 * (y - p['addl_over'])
    return t
