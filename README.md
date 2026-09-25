# Econ dashboard

A one-page dashboard of US economic indicators that rebuilds itself every morning.

- **Live page:** the GitHub Pages site for this repository.
- **Data:** `data/dashboard.json` is what the page reads; `data/csv/` holds one clean CSV per series (`date,value`) and one per fiscal composition. Read them straight from the raw GitHub URLs.
- **Sources:** FRED (BEA, BLS, Census, Federal Reserve series), Treasury FiscalData (Monthly Treasury Statement, Debt to the Penny), and the Census Bureau population estimates.

## How it runs

`.github/workflows/update.yml` runs `build/fetch.py` twice a day (after the 8:30 am Eastern releases and again in the evening), commits any changed data, and redeploys the page. Nothing needs to be done by hand. If a source fails, the tile keeps its previous data and says so; the run only fails outright if nothing could be fetched at all.

## Adding an indicator

Add an entry to `manifest.yaml` (the comments at the top explain the fields), commit, and the next run picks it up. FRED series are the easy case: `source: {type: fred, series: SERIESID}` plus an optional transform (`yoy_pct`, `diff`, `scale`).

## Secrets

`FRED_API_KEY` is stored as a repository secret. Without it the fetcher falls back to FRED's keyless CSV export and loses the last-updated dates and the release calendar.

## Take-home curves and the tax policy model

`build/taxlaw.py` holds the statutory federal income tax schedule for one year per decade (1955, 1965, 1975, 1985, 1995, 2005, 2015) and the current year, transcribed from the Tax Foundation's historical bracket table, plus standard deductions, personal exemptions, and employee-side payroll tax parameters. The fetcher applies each year's schedule to a grid of wage incomes stated in current dollars (deflated with the CPI it already downloads) for a single filer taking the standard deduction with no credits, and writes the resulting share of income kept to `data/dashboard.json` under `tax_curves`. State taxes, the employer half of payroll taxes, the earned income credit, exemption phase-outs, and the alternative minimum tax are all left out. Updating for a new tax year means adding a block to `TAXLAW`.

The debt page turns changes to that schedule into revenue with a deliberately simple model: nine income classes from IRS Statistics of Income Table 1.1 (tax year 2022, counts and mean incomes hard-coded in `taxlaw.py`, incomes scaled with nominal GDP), each taxed as a single filer with the standard deduction. Because that overstates actual receipts, only the proportional change in modeled revenue is used, applied to actual individual income tax receipts as a share of GDP from the Monthly Treasury Statement. An optional behavioral response scales each class's taxable income by the change in its net-of-tax marginal rate with an elasticity of 0.25.
