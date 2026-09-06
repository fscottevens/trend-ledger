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
.github/workflows/scan.yml   (hourly schedule)
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

- **GitHub Actions** runs the Python scanner on a schedule and commits the fresh
  JSON back to the repo.
- **GitHub Pages** serves `docs/` as the site root at the custom domain.

---

## Repository layout

| Path | Purpose |
|---|---|
| `sepa_scanner.py` | The scanner. Single file, no local imports. |
| `requirements.txt` | Python dependencies. |
| `.github/workflows/scan.yml` | Scheduled scan + commit. |
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
| `--verbose` | Debug logging |

---

## Operations

### Schedule

The workflow runs at **:17 past each hour, 13:00–21:00 UTC, weekdays**.

The odd minute is deliberate. GitHub's documentation states that scheduled
workflows are delayed or dropped during high load, and that *"high load times
include the start of every hour."* Scheduling at `:00`, `:15`, `:30` or `:45`
puts the job in the most contended slots. If you change the cadence, keep it off
those four minutes.

UTC does not observe daylight saving, so the window drifts by an hour between EDT
and EST. Adjust the hour range in `scan.yml` if you want tighter alignment.

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
The page displays the age of the data and warns when it is more than a few hours
old. Check the Actions tab for failed runs. Yahoo Finance occasionally rate-limits
shared cloud IPs; an isolated failure is expected and self-corrects on the next
run.

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
