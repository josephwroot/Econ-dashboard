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
