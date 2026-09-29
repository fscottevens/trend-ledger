# Stonks Run

An automated, educational stock screen. It scans every S&P 500 and S&P 400 MidCap
constituent (~900 tickers) hourly during US market hours and ranks them against
several published trading frameworks, then publishes the results as a static site.

**Live site:** https://stonks.run

> **Educational tool, not investment advice.** See `docs/terms.html` for the full
> disclaimer and `docs/methodology.html` for documented limitations.

---

## How it fits together

```
scheduler/scan-trigger.js  (Cloudflare Worker, cron: on time, every market hour)
            |  calls GitHub API: workflow_dispatch
            v
.github/workflows/scan.yml   (also has a GitHub cron as a backup)
            |
            v
     sepa_scanner.py  ---writes--->  docs/data/latest.json
                                              |
                                              v
                                     docs/index.html
                                     (fetches + renders it)
                                              |
                                     served by GitHub Pages
```

Nothing here needs a paid server:

- **A Cloudflare Worker** (free plan) starts the scan on time via GitHub's API.
- **GitHub Actions** runs the Python scanner and commits the fresh JSON back to
  the repo.
- **GitHub Pages** serves `docs/` as the site root at the custom domain.

---

## Repository layout

| Path | Purpose |
|---|---|
| `sepa_scanner.py` | The scanner. Single file, no local imports. |
| `requirements.txt` | Python dependencies. |
| `.github/workflows/scan.yml` | Scan + commit. Triggered by the Worker; GitHub cron as backup. |
| `scheduler/scan-trigger.js` | Cloudflare Worker that triggers the scan on schedule. |
| `scheduler/wrangler.toml` | Optional: deploy the Worker from the command line. |
| `docs/index.html` | The screen itself. |
| `docs/styles.css` | Shared stylesheet for every page. |
| `docs/*.html` | Guides, methodology, about, legal pages. |
| `docs/data/latest.json` | Scan output. Written by the workflow, not by hand. |
| `docs/ads.txt` | AdSense seller authorization. |
| `docs/robots.txt`, `docs/sitemap.xml` | Search engine directives. |
| `docs/favicon.png`, `docs/social-card.png` | Icons and social preview image. |

The site is deliberately dependency-free in the browser: no framework, no build
step, no bundler. `index.html` contains its own JavaScript inline.

---

## Running the scanner locally

```bash
pip install -r requirements.txt

python sepa_scanner.py --selftest        # logic tests, no network required
python sepa_scanner.py                   # full scan, writes docs/data/latest.json
python -m http.server 8000 --directory docs   # preview at localhost:8000
```

Useful flags:

| Flag | Effect |
|---|---|
| `--sp500-only` | Skip the S&P 400, scan ~500 tickers instead of ~900 |
| `--strict-trend` | Require all 8 Minervini criteria instead of 6 |
| `--min-trend-criteria N` | Set the Trend Template threshold explicitly |
| `--extra-tickers A,B` | Add tickers outside the index universe |
| `--json-out PATH` | Where to write the dashboard feed |
| `--min-coverage 0.6` | Refuse to publish if less than this share of stocks was scored |
| `--verbose` | Debug logging |

---

## Operations

### Schedule

Scans start at **9:47, 10:47 ... 15:47 and 16:17 US Eastern, Monday-Friday**
(8 per day). The times live in `SLOTS_ET` in `scheduler/scan-trigger.js`.

**Why a Cloudflare Worker instead of GitHub's cron:** GitHub treats `schedule:`
as best-effort. The Actions history for this repo showed a cron asking for 9
runs per weekday actually running 2-3 times, usually 3-4 hours late, and some
days not at all. The Worker's cron trigger fires on time and calls the GitHub
API to start the workflow (`workflow_dispatch`). It converts to Eastern time
itself, so daylight saving needs no edits.

The GitHub cron in `scan.yml` is kept as a **backup**. Its first step checks the
age of the published data and exits in seconds if the Worker refreshed it in the
last 50 minutes, so the two don't double up.

The Worker needs one secret, `GITHUB_TOKEN`: a fine-grained personal access
token limited to this repository with **Actions: Read and write**. When the
token expires the Worker's status page (its workers.dev URL) says so, and the
site falls back to the backup cron until you replace it.

### Data quality gate

If Yahoo throttles the runner and fewer than 60% of stocks can be scored (or
SPY is missing), the scanner exits with code 2 **without** touching
`latest.json`. The run shows as failed (GitHub emails you) and the site keeps
the last good scan, labelled with its real age. Batches that come back mostly
empty are retried twice with a pause first.

### Fundamentals cache

`fundamentals_cache.json` (company name, sector, P/E, growth, ...) is carried
between runs with `actions/cache` and refreshed once a day (`--fundamentals-ttl-hours 20`),
instead of re-downloading ~900 company profiles every run.

### The commit step

The scan output is *generated* data, so the workflow treats it accordingly: it
fetches the latest `main`, resets onto it, drops the newly generated file in
place, and pushes — retrying up to five times if another commit lands in between.
There is nothing to merge, because the freshly scanned file is always the correct
version. This is why pushes no longer fail when you edit the site while a scan is
running.

### Troubleshooting

**Push rejected / "fetch first" in the Actions log.**
Should not happen with the current workflow. If it does, confirm only one
workflow file exists in `.github/workflows/` — duplicate files with the same
`name:` look identical in the Actions sidebar and can race each other.

**Site shows stale data.**
Outside market hours the page shows a neutral "Market closed" note - that is
normal. A red banner means scans really have stopped. Check, in order:
1. The Actions tab: are there `workflow_dispatch` runs at the slot times? If
   not, open the Worker's URL - it reports whether its token still works.
2. Failed runs: exit code 2 means Yahoo throttled that runner; it retries on the
   next slot. Repeated failures across a whole day point at a yfinance or Yahoo
   change - `pip install -U yfinance` and run `python sepa_scanner.py` locally.

**A ticker fails to download.**
Logged as a single summary line, not a crash. Index membership is hardcoded in
`sepa_scanner.py` and updated manually — see the docstring at the top of that file
for how to refresh it.

**Local push rejected after a scan ran.**
Run `git pull` first. The bot commits to the same branch you do.

---

## Editing the site

Pages are plain HTML in `docs/`. Shared chrome (header, nav, footer) is repeated
in each file, so if you change navigation, change it everywhere — or regenerate
with the build scripts if you keep them.

Styling is centralized in `docs/styles.css`; the design tokens are CSS variables
at the top of that file.

---

## License and attribution

The trading frameworks referenced (Minervini's Trend Template, O'Neil's CANSLIM,
and momentum criteria associated with Dan Zanger and Qullamaggie) are implemented
as simplified approximations from publicly published descriptions. None of these
individuals or their organizations are affiliated with, endorse, or have reviewed
this project.
