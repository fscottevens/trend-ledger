# Trend Ledger

A live dashboard that scans the S&P 500 + S&P 400 against Mark Minervini's
Trend Template, William O'Neil's CANSLIM, and momentum criteria from Dan
Zanger and Qullamaggie, on an automatic schedule — free to run and host.

**Educational tool, not financial advice.** See `sepa_scanner.py`'s own
docstring for the full disclaimer and methodology notes.

## How it's wired together

```
sepa_scanner.py  ──(scheduled by)──>  .github/workflows/scan.yml
       │                                       │
       └──writes──> docs/data/latest.json <────┘ (committed back to the repo)
                            │
                     docs/index.html  (fetches it, renders the dashboard)
                            │
                    served by GitHub Pages
```

Nothing here needs a server you pay for or maintain:
- **GitHub Actions** runs the Python scanner on a schedule and commits the
  fresh JSON output back to the repo.
- **GitHub Pages** serves `docs/index.html`, which reads that JSON and
  renders the two ranked lists. It polls for updates every 5 minutes while
  a visitor has the tab open.

## Setup (10 minutes)

1. **Create a new GitHub repository** and push everything in this folder to
   it (`git init`, `git add .`, `git commit -m "Initial commit"`,
   `git remote add origin <your-repo-url>`, `git push -u origin main`).

2. **Enable GitHub Pages**: repo → Settings → Pages → under "Build and
   deployment," set Source to "Deploy from a branch," branch `main`, folder
   `/docs`. Save. Your site will be live at
   `https://<your-username>.github.io/<repo-name>/` within a minute or two.

3. **Enable Actions** (usually on by default for a new repo): repo →
   Actions tab → if prompted, click to enable workflows.

4. **Trigger the first scan manually** so the site has real data right
   away instead of waiting for the next scheduled hour: Actions tab →
   "Scheduled scan" workflow → "Run workflow." It takes a few minutes for
   ~900 tickers; when it finishes, refresh your site.

After that, it runs on its own — see the cron schedule in
`.github/workflows/scan.yml` (hourly, market hours, weekdays).

## Known limitations worth knowing up front

- **Yahoo Finance may rate-limit shared cloud IPs.** GitHub Actions runners
  share IP ranges across many users, and Yahoo's free data endpoint
  (`yfinance` uses it) occasionally throttles or blocks high-volume
  automated access from those ranges. The workflow is set to
  `continue-on-error` for the scan step so a failed run doesn't break your
  site — it just keeps showing the last successful scan until the next
  run succeeds. If failures become frequent, consider reducing the
  schedule's frequency, or look into a paid data source with an API key.
- **The cron schedule is in UTC**, and GitHub Actions cron doesn't shift
  for US daylight saving. The current schedule drifts by about an hour
  relative to market open/close depending on the time of year — see the
  comment in `scan.yml` if you want to tighten it.
- **A full scan takes a few minutes** (~900 tickers, daily bars + intraday
  + fundamentals). This is normal.

## Adding ad space

The dashboard already has ad slots marked and sized (`docs/index.html`,
search for `ad-slot`): a 728×90 leaderboard under the header, two 300×250
rectangles in the side rail, and a 320×100 inline slot between sections.
They're empty placeholders (dashed boxes labeled "Advertisement") until
you add a network's code.

**Google AdSense** is the standard starting point for a new site:
1. Apply at [adsense.google.com](https://adsense.google.com) with your
   site's URL. Approval isn't automatic — it requires original content,
   policy compliance, and (in practice) some real visitor traffic. A
   brand-new site can take days to weeks to get approved, sometimes on a
   second application after adding more content.
2. Once approved, AdSense gives you a snippet per ad unit. Replace each
   `<div class="ad-slot ...">…</div>` block with that snippet (keep the
   surrounding sizing if you want to avoid layout shift).
3. AdSense requires an `ads.txt` file at your site's root
   (`docs/ads.txt` here, since Pages serves from `/docs`) containing the
   line AdSense gives you during setup — add that file once you have it.

Other options if AdSense doesn't fit: Media.net, Ezoic (better for lower-traffic
sites, since it doesn't have AdSense's stricter minimums), or selling a
banner directly to a sponsor yourself. Whichever you use, most ad
networks' policies expect a visible, genuine disclaimer on financial
content — the one already in the page header meets that bar; don't remove
it.

## Customizing

- **Colors/fonts**: all defined as CSS variables at the top of
  `docs/index.html`'s `<style>` block (`--paper`, `--ink`, `--accent`,
  etc.) — change them there, nothing else needs touching.
- **Scan parameters** (universe, thresholds, weighting): see
  `sepa_scanner.py`'s `Config` class and its module docstring.
- **Schedule frequency**: edit the `cron` line in
  `.github/workflows/scan.yml`.

## Running locally

```bash
pip install -r requirements.txt
python sepa_scanner.py --json-out docs/data/latest.json
python -m http.server 8000 --directory docs   # then open localhost:8000
python sepa_scanner.py --selftest              # logic tests, no network needed
```
