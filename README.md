# Stonks Run

An automated, educational stock screen. It scans every S&P 500 and S&P 400 MidCap
constituent (~900 tickers) hourly during US market hours and ranks them against
several published trading frameworks, then publishes the results as a static site on Cloudflare.

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
            |  1. restore state from the `data` branch
            |  2. sepa_scanner.py  -> docs/data/*.json
            |  3. save state back to the `data` branch (one commit, replaced each run)
            |  4. tools/build_site.py: docs/ + data -> dist/
            |       (clean URLs, pre-rendered lists, one page per stock, sitemap)
            v
     wrangler deploy dist/  ->  Cloudflare Worker "stonks-run" (static assets)
                                      |
                                served at https://stonks.run
```

Nothing here needs a paid server:

- **A Cloudflare Worker** (free plan) starts the scan on time via GitHub's API.
- **GitHub Actions** runs the Python scanner, builds the site and deploys it.
- **The `data` branch** holds the scan state (`history.json` and friends) as a
  single commit that is replaced every run, so `main` no longer grows by
  ~380 KB per scan.
- **Cloudflare Workers static assets** serve `dist/` at the custom domain:
  clean URLs, real response headers and redirects (`dist/_headers`,
  `dist/_redirects`), and no Worker script, so serving pages is free and
  unmetered.

`docs/` is still the hand-edited source and still works as a plain static site
(it is what GitHub Pages served before the move). The repository variable
`SITE_HOST` switches hosting; see `READ-ME-FIRST.txt` for the cutover steps.

---

## Repository layout

| Path | Purpose |
|---|---|
| `sepa_scanner.py` | The scanner. Single file, no local imports. |
| `requirements.txt` | Python dependencies. |
| `.github/workflows/scan.yml` | Scan, save state, build and deploy. Triggered by the Worker; GitHub cron as backup; page edits on `main` redeploy without scanning. |
| `scheduler/scan-trigger.js` | Cloudflare Worker that triggers the scan on schedule. |
| `scheduler/wrangler.toml` | Optional: deploy the Worker from the command line. |
| `docs/index.html` | The screen itself (lists, market pulse, sector leadership, lookup, watchlist, drill-down). |
| `docs/history.html` | Screen history: every session's final list and what happened next. |
| `docs/common.js` | Shared formatting, data loading and hand-built SVG charts for both pages. |
| `docs/styles.css` | Shared stylesheet for every page. |
| `docs/*.html` | Guides, methodology, about, legal pages. |
| `docs/data/latest.json` | Scan output. Written by the workflow, not by hand. |
| `docs/data/history.json` | Final list of every session + forward returns. Updated by every scan. |
| `docs/data/universe.json` | Every scored stock in compact form, for lookup and the watchlist. |
| `docs/data/companies.json` | Company profiles; rewritten only when something changes. |
| `tools/backfill_history.py` | One-off: rebuilds `history.json` from the git history of `latest.json`. |
| `tools/build_site.py` | Builds the deployable site into `dist/`: clean URLs, pre-rendered lists, a page per stock, sitemap, Cloudflare headers/redirects. |
| `wrangler.jsonc` | Cloudflare Worker config for the site (static assets only). |
| `docs/data/stocks.json` | Full detail for every scored stock; build input for the stock pages (kept on the `data` branch, never deployed). |
| `docs/ads.txt` | AdSense seller authorization. |
| `docs/robots.txt`, `docs/sitemap.xml` | Search engine directives. |
| `docs/favicon.png`, `docs/social-card.png` | Icons and social preview image. |

The site is deliberately dependency-free in the browser: no framework, no
bundler, no chart library. Page-specific JavaScript is inline; the shared
helpers live in `docs/common.js`. The only build step is `tools/build_site.py`
(Python standard library), which turns `docs/` into the deployable `dist/`.

---

## Running the scanner locally

```bash
pip install -r requirements.txt

python sepa_scanner.py --selftest        # logic tests, no network required
python sepa_scanner.py                   # full scan, writes docs/data/latest.json
python tools/build_site.py --strict      # build dist/ (fails on broken links)
npx wrangler dev                          # preview dist/ exactly as Cloudflare serves it
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

### History and the other site files

Every published scan also writes three files next to `latest.json`. Each run
rewrites the *current session's* entry in `history.json`, so the last scan of the
day (normally 16:17 ET) becomes that session's permanent record; a later weekend
or holiday rerun never replaces a finished session. The same run refreshes the
+1/+5/+10/+20-session returns of every past entry from the daily price history it
already downloaded. All of this is best-effort: if any of it fails, the scan
logs a warning and `latest.json` still publishes. `--no-extras` turns it off.

If `history.json` is ever lost, `python tools/backfill_history.py` rebuilds the
lists from git history, and the next scan fills the returns back in.

### State, build and deploy

- **Restore:** each run first copies the scan state (`latest`, `history`,
  `universe`, `companies` and `stocks.json`) from the `data` branch into
  `docs/data/`. If the branch cannot be reached the run stops, rather than
  starting from an old copy and overwriting history.
- **Save:** after a good scan the state is pushed back to `data` as a single
  commit (force-pushed, so the branch never grows). The step refuses to save a
  `history.json` with fewer sessions than it started with.
- **Build + deploy:** `tools/build_site.py` writes `dist/`, then
  `wrangler deploy` uploads only the files that changed. The deploy is skipped
  (with a notice) until the `CLOUDFLARE_API_TOKEN` secret exists.
- **Transition:** while `SITE_HOST` is not `cloudflare`, the old commit step
  also copies the four data files to `main` so GitHub Pages keeps working. It
  fetches the latest `main`, resets onto it, drops the generated files in and
  pushes, retrying up to five times, so it never conflicts with your edits.

Do not delete the `data` branch: it is the only copy of the live history once
the site is on Cloudflare.

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
in each file, so if you change navigation, change it everywhere. The generated
stock pages copy their header and footer from `docs/learn.html` at build time.
Keep internal links as `page.html` in `docs/`; the build rewrites them to clean
URLs (`/page`) and adds permanent redirects from the old addresses.

Pushing a change under `docs/` to `main` redeploys the site within a minute or
two (no scan needed).

Styling is centralized in `docs/styles.css`; the design tokens are CSS variables
at the top of that file.

---

## License and attribution

The trading frameworks referenced (Minervini's Trend Template, O'Neil's CANSLIM,
and momentum criteria associated with Dan Zanger and Qullamaggie) are implemented
as simplified approximations from publicly published descriptions. None of these
individuals or their organizations are affiliated with, endorse, or have reviewed
this project.
