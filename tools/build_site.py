#!/usr/bin/env python3
"""
tools/build_site.py
===================
Builds the deployable website (dist/) from docs/ plus the scan's data files.
The scheduled workflow runs it after every scan and deploys dist/ to
Cloudflare (see wrangler.jsonc). docs/ stays the hand-edited source.

WHAT IT DOES
  1. Copies docs/ to dist/, minus GitHub-Pages-only and build-only files.
  2. Rewrites internal links to clean URLs (/history, not history.html),
     makes asset paths root-absolute, and cache-busts styles.css/common.js.
  3. Pre-renders the screen's two lists into index.html, so search engines
     (and visitors, before JavaScript runs) see real rankings instead of an
     empty "Loading" table.
  4. Generates one static page per scored stock (/stock/nvda) and a stock
     directory (/stock/), each with its own title, description and links.
  5. Writes sitemap.xml, robots.txt, and Cloudflare's _headers/_redirects.
  6. Checks that every internal link in dist/ points at a real file.

USAGE
    python tools/build_site.py                       # docs/ -> dist/
    python tools/build_site.py --strict              # fail on broken links
    python tools/build_site.py --src docs --out dist --site-url https://stonks.run

OPTIONAL ENVIRONMENT VARIABLES (set as GitHub repository variables)
    CF_WEB_ANALYTICS_TOKEN    Cloudflare Web Analytics site token
    GOOGLE_SITE_VERIFICATION  Search Console HTML-tag verification code

Standard library only, so the workflow needs nothing extra installed.
Every data file is optional: with no data at all it still builds the static
pages, and with no stocks.json the stock pages show the universe-level data.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Minervini criteria in the scanner's fixed order (bit i of universe "tb").
CRITERIA = [
    "Price above rising 150-day & 200-day average",
    "150-day average above 200-day average",
    "200-day average trending up vs. 1 month ago",
    "50-day average above both 150-day & 200-day",
    "Price above 50-day average",
    "At least 30% above 52-week low",
    "Within 25% of 52-week high",
    "RS Rating >= 70",
]
RATING_LABEL = {"strong_buy": "Strong Buy", "buy": "Buy", "hold": "Hold",
                "underperform": "Underperform", "sell": "Sell"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Never deployed: CNAME is GitHub Pages' domain file; stocks.json is a build
# input (~3 MB) that visitors' browsers never need.
SKIP_FILES = {"CNAME", ".gitkeep", ".DS_Store"}
SKIP_DATA_FILES = {"stocks.json"}

WARNINGS: List[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    # "::warning::" makes GitHub Actions show it on the run summary.
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") else "WARNING: "
    print(prefix + msg)


# --------------------------------------------------------------------------
# Formatting (mirrors common.js so static and live pages read the same)
# --------------------------------------------------------------------------

def esc(v) -> str:
    return "" if v is None else html.escape(str(v), quote=True)


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def fmt_pct(v, d: int = 2) -> str:
    if not is_num(v):
        return "—"
    r = round(float(v), d)
    if r == 0:
        r = 0.0
    return ("+" if r >= 0 else "") + f"{r:.{d}f}%"


def fmt_num(v, d: int = 2) -> str:
    if not is_num(v):
        return "—"
    s = f"{float(v):,.{d}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def fmt_money(v) -> str:
    return "—" if not is_num(v) else f"${float(v):,.2f}"


def fmt_vol(v) -> str:
    if not is_num(v):
        return "—"
    v = float(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if v >= div:
            return f"{v / div:.2f}{suf}"
    if v >= 1e3:
        return f"{v / 1e3:.1f}K"
    return str(round(v))


def fmt_cap(v) -> str:
    return "—" if not is_num(v) else "$" + fmt_vol(v)


def fmt_day(iso: Optional[str], with_year: bool = False) -> str:
    if not iso:
        return "—"
    y, m, d = (int(x) for x in iso[:10].split("-"))
    return f"{MONTHS[m - 1]} {d}" + (f", {y}" if with_year else "")


def fmt_day_long(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso[:10])
    return f"{WDAYS[dt.weekday()]}, {MONTHS[dt.month - 1]} {dt.day}, {dt.year}"


def fmt_ts_et(ts: Optional[str]) -> str:
    """'Nov 3, 7:52 PM ET' - the same shape the live page shows."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts).astimezone(ET)
    except ValueError:
        return esc(ts)
    hour = dt.hour % 12 or 12
    return f"{MONTHS[dt.month - 1]} {dt.day}, {hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'} ET"


def chg_class(v) -> str:
    return "" if not is_num(v) else ("chg-pos" if v >= 0 else "chg-neg")


def slug(ticker: str) -> str:
    """URL slug for a ticker. Must match SR.stockUrl() in docs/common.js."""
    return re.sub(r"[^a-z0-9-]", "-", str(ticker).lower())


def short_name(name: str) -> str:
    """'NVIDIA Corporation' -> 'NVIDIA', for titles that must stay short."""
    s = (name or "").strip()
    s = re.sub(r",?\s+(Inc\.?|Incorporated|Corporation|Corp\.?|Company|Co\.?|Ltd\.?|Limited|plc|PLC|N\.V\.|S\.A\.|"
               r"L\.P\.|LP|Holdings?|Group|Technologies|Trust)$", "", s)
    s = re.sub(r",?\s+(Inc\.?|Corporation|Corp\.?|Holdings?|Group|plc)$", "", s)
    return s.rstrip(",").strip() or (name or "")


def title_name(name: str, limit: int = 26) -> str:
    """Company name short enough for a page title (search results cut titles
    at ~60 characters): 'Expeditors International of Washington, Inc.'
    -> 'Expeditors International'."""
    s = short_name(name)
    if len(s) <= limit:
        return s
    words, out = s.split(), ""
    for w in words:
        if len(out) + len(w) + 1 > limit:
            break
        out = (out + " " + w).strip()
    out = re.sub(r"\s+(of|and|the|&|for|de)$", "", out, flags=re.I)
    return out or s[:limit]


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        warn(f"Could not read {path.name} ({e}); building without it.")
        return default


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10] if path.exists() else "0"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

class SiteData:
    """Everything the generated pages need, merged from the data files.
    Priority for a stock's detail: today's lists (latest.json) > stocks.json
    > the compact universe row."""

    def __init__(self, data_dir: Path):
        self.latest = load_json(data_dir / "latest.json", {}) or {}
        self.history = load_json(data_dir / "history.json", {}) or {}
        self.companies = (load_json(data_dir / "companies.json", {}) or {}).get("c", {}) or {}
        details = load_json(data_dir / "stocks.json", {}) or {}
        self.details = details.get("stocks", {}) or {}
        self.detail_dates = details.get("spark_dates") or []
        uni = load_json(data_dir / "universe.json", {}) or {}
        self.universe = self._parse_universe(uni)
        self.session_date = (self.latest.get("session_date") or uni.get("session_date")
                             or self.history.get("as_of"))
        self.generated_at = self.latest.get("generated_at") or uni.get("generated_at")
        self.total_scored = self.latest.get("total_scored") or len(self.universe)
        self.records = ticker_records(self.history)
        self.on_list: Dict[str, dict] = {}
        for sec, key in ((1, "section1"), (2, "section2")):
            for i, r in enumerate(self.latest.get(key) or []):
                self.on_list[r["ticker"]] = {"sec": sec, "rank": i + 1, "row": r}
        self.sector_stats = {s["name"]: s for s in (self.latest.get("sectors") or []) if s.get("name")}
        self.sector_rank = {s["name"]: i + 1 for i, s in enumerate(self.latest.get("sectors") or [])}
        self.top_industries = {s["name"]: i + 1 for i, s in enumerate(self.latest.get("industries") or [])}
        # Tickers that left the index keep their history, but get no page:
        # there is no current data to show for them.
        self.tickers = [u["t"] for u in self.universe]

    @staticmethod
    def _parse_universe(u: dict) -> List[dict]:
        fields = u.get("fields") or []
        sectors = u.get("sectors") or []
        inds = u.get("industries") or []
        out = []
        for row in u.get("rows") or []:
            o = dict(zip(fields, row))
            o["sector"] = sectors[o["s"]] if is_num(o.get("s")) and o["s"] < len(sectors) else ""
            o["industry"] = inds[o["i"]] if is_num(o.get("i")) and o["i"] < len(inds) else ""
            out.append(o)
        return out

    def stock(self, u: dict) -> dict:
        """View model for one stock page."""
        t = u["t"]
        det = self.details.get(t, {})
        lst = self.on_list.get(t)
        row = lst["row"] if lst else {}
        co = self.companies.get(t, {})

        m = {"sma50": u.get("s50"), "sma150": u.get("s150"), "sma200": u.get("s200"),
             "wk52_high": u.get("h52"), "wk52_low": u.get("l52"), "ret_3m_pct": u.get("r3"),
             "ret_12m_pct": u.get("r12"), "adr20_pct": u.get("adr"), "pct_off_52w_high": u.get("hi")}
        m.update({k: v for k, v in (det.get("m") or {}).items() if v is not None})
        m.update({k: v for k, v in (row.get("metrics") or {}).items() if v is not None})
        m = {k: v for k, v in m.items() if v is not None}

        prof = row.get("profile") or det.get("pf") or {}
        if not prof and co:
            prof = {"summary": co.get("d"), "website": co.get("w"), "hq": co.get("hq"), "employees": co.get("e")}
        if not prof.get("market_cap") and is_num(u.get("mc")):
            prof = dict(prof, market_cap=u["mc"] * 1e9)

        earnings = row.get("earnings") or det.get("e")
        if not earnings and is_num(u.get("ed")):
            earnings = {"days": u["ed"]}

        spark, dates = None, None
        if row.get("spark") and self.latest.get("spark_dates"):
            spark, dates = row["spark"], self.latest["spark_dates"]
        elif det.get("sp") and self.detail_dates:
            spark, dates = det["sp"], self.detail_dates

        signals = row.get("signals") or det.get("sig")
        fw = None
        if row:
            fw = {"canslim": row.get("canslim"), "zanger": row.get("zanger"),
                  "qullamaggie": row.get("qullamaggie"), "value": row.get("value")}
        elif det.get("fw"):
            fw = det["fw"]

        return {
            "t": t, "slug": slug(t),
            "name": u.get("n") or co.get("n") or t,
            "sector": u.get("sector") or co.get("s") or "",
            "industry": u.get("industry") or co.get("i") or "",
            "price": row.get("price", u.get("p")), "chg": row.get("pct_chg", u.get("c")),
            "rv": row.get("rel_vol", det.get("rv", u.get("rv"))),
            "rs": u.get("rs"), "tn": int(u.get("tn") or 0), "tb": int(u.get("tb") or 0),
            "q": bool(u.get("q")), "ms": u.get("ms"), "qs": u.get("qs"),
            "mr": u.get("mr"), "qr": u.get("qr"),
            "m": m, "profile": prof, "earnings": earnings, "spark": spark, "spark_dates": dates,
            "signals": signals, "fw": fw, "list": lst, "record": self.records.get(t),
        }


def ticker_records(history: dict) -> Dict[str, dict]:
    """Per-ticker record across every session (port of SR.tickerRecords)."""
    days = history.get("days") or []
    recs: Dict[str, dict] = {}
    for di, d in enumerate(days):
        for p in d.get("picks") or []:
            r = recs.get(p["t"])
            if r is None:
                r = recs[p["t"]] = {"t": p["t"], "apps": [], "first": d["date"], "best": 99}
            r["apps"].append({"date": d["date"], "di": di, "sec": p.get("sec"), "r": p.get("r"),
                              "p": p.get("p"), "tn": p.get("tn"), "f": p.get("f") or {},
                              "spy": d.get("spy") or {}})
            r["last"], r["last_di"] = d["date"], di
            r["best"] = min(r["best"], p.get("r") or 99)
    last = len(days) - 1
    for r in recs.values():
        r["sessions"] = len(r["apps"])
        r["on_list"] = r["last_di"] == last
        s = 0
        for a in reversed(r["apps"]):
            if a["di"] == last - s:
                s += 1
            else:
                break
        r["streak"] = s if r["on_list"] else 0
        r["since_first"] = r["apps"][0]["f"].get("td")
        r["spy_since_first"] = r["apps"][0]["spy"].get("td")
    return recs


# --------------------------------------------------------------------------
# Shared page fragments
# --------------------------------------------------------------------------

def trend_bar(n: int) -> str:
    segs = "".join(f'<span class="seg{" on" if i < n else ""}"></span>' for i in range(8))
    return f'<span class="trend-bar" aria-label="{n} of 8 Trend Template criteria met">{segs}</span>'


def status_class(status: Optional[str]) -> str:
    s = status or ""
    if "CONFIRMED UPTREND" in s:
        return "status-up"
    if "UNDER PRESSURE" in s:
        return "status-down"
    if "MIXED" in s:
        return "status-mixed"
    return "status-unknown"


STATUS_LABEL = {"status-up": "Market: Confirmed Uptrend", "status-down": "Market: Under Pressure",
                "status-mixed": "Market: Mixed", "status-unknown": "Market: Unavailable"}


def badges_html(r: dict, section: int, rank: int) -> str:
    """Port of badgesHtml() in index.html (NEW / BACK / D-streak, ER, rank move)."""
    h = r.get("history")
    out = ""
    if h:
        if h.get("streak", 0) <= 1 and h.get("sessions", 0) <= 1:
            out += '<span class="bdg bdg-new" title="First appearance on either list">New</span>'
        elif h.get("streak", 0) <= 1:
            out += '<span class="bdg bdg-back" title="Back on the lists after dropping off">Back</span>'
        else:
            out += f'<span class="bdg" title="{h["streak"]} consecutive sessions on the lists">D{h["streak"]}</span>'
    e = r.get("earnings") or {}
    if e.get("next") and is_num(e.get("days")) and e["days"] <= 7:
        when = "today" if e["days"] == 0 else f'in {e["days"]} day{"" if e["days"] == 1 else "s"}'
        out += f'<span class="bdg bdg-earn" title="Earnings {when}">ER {"today" if e["days"] == 0 else str(e["days"]) + "d"}</span>'
    move = ""
    if h and h.get("prev_rank") is not None and h.get("prev_section") == section and h["prev_rank"] != rank:
        d = h["prev_rank"]
        up = d > rank
        move = (f'<span class="rank-move {"up" if up else "down"}" title="Rank last session: {d}">'
                f'{"&#9650;" if up else "&#9660;"}{abs(d - rank)}</span>')
    return (f'<span class="badges">{out}</span>' if out else "") + move


def ledger_table(rows: List[dict], section: int) -> str:
    """Same markup renderTable() in index.html produces, with ticker links."""
    if not rows:
        return ('<div class="state-msg"><strong>No Data</strong>The first scheduled scan will populate this '
                'automatically.</div>')
    out = ['<table class="ledger"><thead><tr><th></th><th>Ticker</th><th class="num">Price</th>'
           '<th class="num">%Chg</th><th>Trend</th><th class="num">Score</th></tr></thead><tbody>']
    for i, r in enumerate(rows):
        qual = '<span class="qual-flag" title="Fully qualifies">&#9679;</span>' if r.get("qualifies") else ""
        co = f'<span class="co-name">{esc(r.get("company"))}</span>' if r.get("company") else ""
        out.append(
            f'<tr class="data-row" data-idx="{i}" tabindex="0"><td class="rank">{i + 1}</td>'
            f'<td class="ticker"><a class="tk-link" href="/stock/{slug(r["ticker"])}">{esc(r["ticker"])}</a>'
            f'{badges_html(r, section, i + 1)}{co}</td>'
            f'<td class="num">{fmt_money(r.get("price"))}</td>'
            f'<td class="num {"chg-pos" if (r.get("pct_chg") or 0) >= 0 else "chg-neg"}">{fmt_pct(r.get("pct_chg"))}</td>'
            f'<td>{trend_bar(int(r.get("trend_n") or 0))} {esc(r.get("trend"))}{qual}</td>'
            f'<td class="num">{esc(r.get("score"))}</td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


def range_bar(price, low, high) -> str:
    if not (is_num(price) and is_num(low) and is_num(high)) or high <= low:
        return ""
    pct = max(0.0, min(100.0, (price - low) / (high - low) * 100))
    return ('<div class="range-wrap"><div class="range-track">'
            f'<div class="range-fill" style="width:{pct:.1f}%"></div>'
            f'<div class="range-marker" style="left:{pct:.1f}%"></div></div>'
            f'<div class="range-labels"><span>52W Low <strong>{fmt_money(low)}</strong></span>'
            f'<span>{pct:.0f}% of range</span>'
            f'<span>52W High <strong>{fmt_money(high)}</strong></span></div></div>')


def ma_stack(price, m: dict) -> str:
    rows = [r for r in (("Price", price, True), ("50-day", m.get("sma50"), False),
                        ("150-day", m.get("sma150"), False), ("200-day", m.get("sma200"), False))
            if is_num(r[1])]
    if len(rows) < 2:
        return ""
    ideal = ["Price", "50-day", "150-day", "200-day"]
    srt = sorted(rows, key=lambda r: -r[1])
    order = [r[0] for r in srt]
    in_order = order == [l for l in ideal if l in order]
    hi, lo = srt[0][1], srt[-1][1]
    span = (hi - lo) or 1
    bars = "".join(
        f'<div class="ma-row"><span class="ma-name{" is-price" if p else ""}">{label}</span>'
        f'<span class="ma-bar-wrap"><span class="ma-bar{" is-price" if p else ""}" '
        f'style="width:{22 + (v - lo) / span * 78:.1f}%"></span></span>'
        f'<span class="ma-val">{fmt_money(v)}</span></div>'
        for label, v, p in srt)
    verdict = ('<span class="ma-ok">Stacked in trend order</span> &mdash; price above the 50-day, above the '
               '150-day, above the 200-day.' if in_order else
               '<span class="ma-no">Not in trend order</span> &mdash; the framework looks for price above the '
               '50-day, above the 150-day, above the 200-day.')
    return f'<div class="ma-stack">{bars}<div class="ma-verdict">{verdict}</div></div>'


def metric_cell(label: str, value: str, sub: Optional[tuple] = None, tip: str = "") -> str:
    tip_attr = f' data-tip="{esc(tip)}" tabindex="0"' if tip else ""
    sub_html = f'<div class="m-sub {sub[1]}">{sub[0]}</div>' if sub else ""
    return (f'<div class="metric-cell"><div class="m-label"{tip_attr}>{label}</div>'
            f'<div class="m-value">{value}</div>{sub_html}</div>')


def ma_distance(price, ma) -> Optional[tuple]:
    if not (is_num(price) and is_num(ma)) or ma == 0:
        return None
    d = (price / ma - 1) * 100
    return (f'{"+" if d >= 0 else ""}{d:.1f}% vs price', "m-above" if d >= 0 else "m-below")


TIPS = {
    "sma50": "Average closing price over the trailing 50 trading days (about 2.5 months).",
    "sma150": "Average closing price over the trailing 150 trading days (about 7 months).",
    "sma200": "Average closing price over the trailing 200 trading days (about 10 months).",
    "adr": "Average daily high-to-low range over the past 20 sessions, as a % of price.",
    "rs": "Ranks this stock's price performance against every other stock in the scan, 1 (worst) to 99 (best).",
    "trend": "How many of Minervini's 8 Trend Template criteria this stock currently meets.",
}


# --------------------------------------------------------------------------
# Stock page
# --------------------------------------------------------------------------

def readout_lines(vm: dict, data: SiteData) -> List[str]:
    """Plain-language description of the numbers (port of buildReadout() in
    index.html, plus group context). Descriptive, never prescriptive."""
    m, price, tn = vm["m"], vm["price"], vm["tn"]
    lines = []
    if tn >= 7:
        lines.append(f"<strong>Trend structure:</strong> meets {tn} of 8 Trend Template criteria &mdash; the moving "
                     "averages are in the stacked arrangement the framework associates with a confirmed uptrend.")
    elif tn >= 5:
        lines.append(f"<strong>Trend structure:</strong> meets {tn} of 8. Partially aligned &mdash; the checklist "
                     "below shows which specific conditions are unmet, which matters more than the count.")
    else:
        lines.append(f"<strong>Trend structure:</strong> meets only {tn} of 8. The framework would not consider "
                     "this a confirmed uptrend.")

    if is_num(m.get("sma50")) and is_num(price) and m["sma50"]:
        ext = (price / m["sma50"] - 1) * 100
        if ext > 25:
            lines.append(f"<strong>Extension:</strong> trading {ext:.0f}% above its 50-day average. Minervini's "
                         "writing treats stocks this far extended as carrying elevated risk of a sharp pullback "
                         "toward the average.")
        elif ext < -3:
            lines.append(f"<strong>Extension:</strong> currently {abs(ext):.1f}% <em>below</em> its 50-day "
                         "average &mdash; in a short-term pullback rather than pushing higher.")
        else:
            side = f"{ext:.1f}% above" if ext >= 0 else f"{abs(ext):.1f}% below"
            lines.append(f"<strong>Extension:</strong> {side} its 50-day average &mdash; not unusually extended "
                         "in either direction.")

    hi, lo = m.get("wk52_high"), m.get("wk52_low")
    if is_num(hi) and is_num(lo) and is_num(price) and hi > lo:
        pos = (price - lo) / (hi - lo) * 100
        if pos >= 90:
            lines.append("<strong>Range position:</strong> near the top of its 52-week range. Little to no "
                         "overhead supply &mdash; few holders are underwater.")
        elif pos <= 40:
            lines.append("<strong>Range position:</strong> in the lower portion of its 52-week range, meaning "
                         "meaningful overhead supply sits above the current price.")
        else:
            lines.append("<strong>Range position:</strong> mid-range within its 52-week span.")

    r3, r12 = m.get("ret_3m_pct"), m.get("ret_12m_pct")
    if is_num(r3) and is_num(r12):
        if r12 > 0 and r3 > r12 * 0.7:
            lines.append("<strong>Momentum shape:</strong> most of the trailing 12-month gain came in the last 3 "
                         "months &mdash; a recent acceleration rather than a steady climb.")
        elif r3 < 0 < r12:
            lines.append("<strong>Momentum shape:</strong> positive over 12 months but negative over the last 3 "
                         "&mdash; decelerating.")

    if is_num(vm["rs"]):
        lines.append(f"<strong>Relative strength:</strong> RS Rating {vm['rs']} of 99 &mdash; its weighted "
                     f"3-to-12-month price performance ranks ahead of roughly {max(0, int(vm['rs']) - 1)}% of the "
                     f"{data.total_scored} stocks scanned.")

    adr = m.get("adr20_pct")
    if is_num(adr):
        if adr >= 5:
            lines.append(f"<strong>Volatility:</strong> average daily range of {adr:.1f}% is high &mdash; daily "
                         "swings are much larger than a typical large-cap's.")
        elif adr <= 1.5:
            lines.append(f"<strong>Volatility:</strong> average daily range of {adr:.1f}% is low relative to "
                         "typical momentum candidates.")

    sec = data.sector_stats.get(vm["sector"])
    if sec and is_num(sec.get("pct_qualified")):
        rank = data.sector_rank.get(vm["sector"])
        lines.append(f"<strong>Group context:</strong> {sec['pct_qualified']:.0f}% of the {sec.get('n', '')} "
                     f"{esc(vm['sector'])} stocks scanned clear the Trend Template gate"
                     + (f" (sector {rank} of {len(data.sector_rank)} by that measure)" if rank else "") + "."
                     + (f" Its industry, {esc(vm['industry'])}, is among the strongest groups by average RS today."
                        if vm["industry"] in data.top_industries else ""))

    e = vm["earnings"] or {}
    if is_num(e.get("days")) and e["days"] <= 14:
        when = "today" if e["days"] == 0 else f'in {e["days"]} day{"" if e["days"] == 1 else "s"}'
        lines.append(f"<strong>Event risk:</strong> earnings are due {when}. Momentum frameworks generally treat "
                     "an imminent report as a separate risk from the chart: the stock can gap well beyond its "
                     "normal daily range.")

    if not vm["q"]:
        need = data.latest.get("min_trend_needed") or 6
        lines.append(f"<strong>Note:</strong> does not fully clear the Trend Template gate ({need} of 8 "
                     "required), so the screen would not treat it as a confirmed setup.")
    return lines


def checklist_html(vm: dict) -> str:
    """All 8 criteria, met or not, with the actual numbers behind each."""
    m, p, rs = vm["m"], vm["price"], vm["rs"]
    s50, s150, s200 = m.get("sma50"), m.get("sma150"), m.get("sma200")
    hi, lo = m.get("wk52_high"), m.get("wk52_low")
    vals = [None] * 8
    if is_num(p) and is_num(s150) and is_num(s200):
        vals[0] = f"price {fmt_money(p)} vs {fmt_money(s150)} and {fmt_money(s200)}"
    if is_num(s150) and is_num(s200):
        vals[1] = f"{fmt_money(s150)} vs {fmt_money(s200)}"
    if is_num(s50) and is_num(s150) and is_num(s200):
        vals[3] = f"{fmt_money(s50)} vs {fmt_money(s150)} / {fmt_money(s200)}"
    if is_num(p) and is_num(s50) and s50:
        vals[4] = f"{fmt_pct((p / s50 - 1) * 100, 1)} vs the 50-day"
    if is_num(p) and is_num(lo) and lo:
        vals[5] = f"{fmt_pct((p / lo - 1) * 100, 1)} vs the 52-week low of {fmt_money(lo)}"
    if is_num(p) and is_num(hi) and hi:
        vals[6] = f"{fmt_pct((p / hi - 1) * 100, 1)} vs the 52-week high of {fmt_money(hi)}"
    if is_num(rs):
        vals[7] = f"currently {rs}"
    items = []
    for i, label in enumerate(CRITERIA):
        met = bool(vm["tb"] & (1 << i))
        detail = f' <span class="ck-val">({esc(vals[i])})</span>' if vals[i] else ""
        items.append(f'<li class="{"" if met else "unmet"}"><span class="{"check-yes" if met else "check-no"}">'
                     f'{"[X]" if met else "[ ]"}</span> <span>{esc(label)}{detail}</span></li>')
    return '<ul class="checklist">' + "".join(items) + "</ul>"


def static_chart_svg(closes: List, sma: List) -> str:
    """No-JavaScript fallback: two plain lines, no text (text in a scaled SVG
    becomes unreadable on phones). common.js replaces it with the
    interactive chart on load."""
    vals = [v for v in closes + sma if is_num(v)]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    n = max(len(closes), 2)

    def path(series):
        d, pen = [], False
        for i, v in enumerate(series):
            if not is_num(v):
                pen = False
                continue
            x = i / (n - 1) * 600
            y = 190 - (v - lo) / span * 180
            d.append(f'{"L" if pen else "M"}{x:.1f},{y:.1f}')
            pen = True
        return "".join(d)

    return ('<svg class="viz-svg" viewBox="0 0 600 200" width="100%" height="200" preserveAspectRatio="none" '
            'aria-hidden="true">'
            f'<path d="{path(sma)}" fill="none" style="stroke:var(--series-2)" stroke-width="2" '
            'vector-effect="non-scaling-stroke"/>'
            f'<path d="{path(closes)}" fill="none" style="stroke:var(--series-1)" stroke-width="2" '
            'vector-effect="non-scaling-stroke"/></svg>')


def record_html(vm: dict, data: SiteData) -> str:
    rec = vm["record"]
    days = data.history.get("days") or []
    lines = []
    if is_num(vm["mr"]):
        lines.append(f"Ranks <strong>#{vm['mr']}</strong> of {data.total_scored} on momentum"
                     + (f" and <strong>#{vm['qr']}</strong> on trend quality &amp; value" if is_num(vm["qr"]) else "")
                     + " in the latest scan.")
    if rec:
        s = f"On the lists in <strong>{rec['sessions']}</strong> of {len(days)} tracked sessions"
        if rec["streak"] > 1:
            s += f", including the last <strong>{rec['streak']}</strong> in a row"
        s += f". First flagged <strong>{fmt_day(rec['first'], True)}</strong>"
        if is_num(rec["since_first"]):
            s += (f' &mdash; <span class="{chg_class(rec["since_first"])}">{fmt_pct(rec["since_first"], 1)}</span>'
                  " since that close")
            if is_num(rec["spy_since_first"]):
                s += f" (S&amp;P 500: {fmt_pct(rec['spy_since_first'], 1)})"
        s += f". Best rank <strong>#{rec['best']}</strong>."
        lines.append(s)
    elif days:
        lines.append(f"Has not appeared on either list in the {len(days)} sessions tracked since "
                     f"{fmt_day(days[0]['date'], True)}.")
    out = '<div class="record-line">' + "<br>".join(lines) + "</div>" if lines else ""
    if rec:
        rows = []
        for a in list(reversed(rec["apps"]))[:12]:
            f = a["f"]
            cells = "".join(f'<td class="num {chg_class(f.get(k))}">{fmt_pct(f.get(k), 1)}</td>'
                            for k in ("5", "20", "td"))
            rows.append(f'<tr><td>{fmt_day(a["date"])}</td><td>{"Momentum" if a["sec"] == 1 else "Quality"} '
                        f'#{a["r"]}</td><td class="num">{fmt_money(a["p"])}</td>'
                        f'<td>{a["tn"] if a["tn"] is not None else "—"}/8</td>{cells}</tr>')
        more = (f'<p class="viz-note">Showing the latest 12 of {rec["sessions"]} appearances.</p>'
                if rec["sessions"] > 12 else "")
        out += ('<div class="table-scroll"><table class="data-table"><thead><tr><th>Session</th>'
                '<th>List &amp; rank</th><th class="num">Close</th><th>Trend</th><th class="num">+5d</th>'
                '<th class="num">+20d</th><th class="num">To date</th></tr></thead><tbody>'
                + "".join(rows) + "</tbody></table></div>" + more)
    return out


def fundamentals_html(m: dict, price) -> str:
    cells = []

    def add(key, label, fmt):
        if is_num(m.get(key)):
            cells.append(metric_cell(label, fmt(m[key])))

    def pct1(v):
        return fmt_num(v, 1) + "%"

    add("trailing_pe", "P/E (TTM)", lambda v: fmt_num(v, 1))
    add("forward_pe", "Forward P/E", lambda v: fmt_num(v, 1))
    add("peg_ratio", "PEG Ratio", lambda v: fmt_num(v, 2))
    add("eps_qtr_growth_pct", "EPS Qtr Growth", fmt_pct)
    add("eps_ann_growth_pct", "EPS Ann Growth", fmt_pct)
    add("revenue_growth_pct", "Revenue Growth", fmt_pct)
    add("gross_margin_pct", "Gross Margin", pct1)
    add("operating_margin_pct", "Operating Margin", pct1)
    add("profit_margin_pct", "Profit Margin", pct1)
    add("roe_pct", "Return on Equity", pct1)
    add("debt_to_equity", "Debt/Equity", lambda v: fmt_num(v, 1))
    add("institutional_pct", "Institutional", pct1)
    add("short_pct_float", "Short % Float", pct1)
    add("beta", "Beta", lambda v: fmt_num(v, 2))
    if not cells:
        return ""
    out = ('<h2 id="fundamentals">Fundamentals</h2><p class="sp-sub">Traditional valuation and quality metrics, '
           'where published. Missing fields are skipped rather than counted against a stock.</p>'
           f'<div class="metric-grid">{"".join(cells)}</div>')
    rating = RATING_LABEL.get(m.get("analyst_rating"))
    if rating or is_num(m.get("target_mean")):
        parts = []
        if rating:
            parts.append(f"consensus <b>{rating}</b>" + (f" from {int(m['analyst_count'])} analysts"
                                                          if is_num(m.get("analyst_count")) else ""))
        if is_num(m.get("target_mean")):
            up = m.get("target_upside_pct")
            parts.append(f"mean price target <b>{fmt_money(m['target_mean'])}</b>"
                         + (f' (<span class="{chg_class(up)}">{fmt_pct(up, 1)}</span> vs. price)' if is_num(up) else ""))
        out += ('<div class="analyst-line">Wall Street view: ' + " &middot; ".join(parts) +
                '. <span class="ml-note">Third-party estimates via Yahoo Finance, not ours.</span></div>')
    return out


def signals_html(vm: dict) -> str:
    sig, fw = vm["signals"], vm["fw"] or {}
    if not sig:
        return ""
    blocks = []
    for key, title in (("canslim", "CANSLIM"), ("zanger", "Zanger"), ("qullamaggie", "Qullamaggie"),
                       ("value", "Value")):
        items = sig.get(key) or []
        score = f" ({esc(fw.get(key))})" if fw.get(key) else ""
        body = ('<ul class="signal-list">' + "".join(f"<li>{esc(s)}</li>" for s in items) + "</ul>"
                if items else '<p class="signal-empty">No signals triggered.</p>')
        blocks.append(f'<div class="signal-block"><div class="signal-block-head">{title}{score}</div>{body}</div>')
    return ('<h2 id="signals">Framework Signals</h2><p class="sp-sub">Framework-specific triggers from the latest '
            "scan. An empty section means no signal fired &mdash; which is normal, not a negative reading.</p>"
            + "".join(blocks))


def company_html(vm: dict) -> str:
    prof = vm["profile"] or {}
    chips = []
    if vm["sector"]:
        chips.append(f'<span class="chip teal">{esc(vm["sector"])}</span>')
    if vm["industry"]:
        chips.append(f'<span class="chip">{esc(vm["industry"])}</span>')
    summary = (f'<p class="company-summary">{esc(prof["summary"])}</p>' if prof.get("summary") else
               '<p class="company-summary" style="color:var(--text-faint)">No business description was available '
               'from the data source for this company.</p>')
    facts = []
    if prof.get("hq"):
        facts.append(f"HQ <b>{esc(prof['hq'])}</b>")
    if is_num(prof.get("employees")):
        facts.append(f"Employees <b>{fmt_num(prof['employees'], 0)}</b>")
    if is_num(prof.get("market_cap")):
        facts.append(f"Market cap <b>{fmt_cap(prof['market_cap'])}</b>")
    w = prof.get("website") or ""
    if re.match(r"^https?://[^\s\"'<>]+$", w):
        host = re.sub(r"^https?://(www\.)?", "", w).split("/")[0]
        facts.append(f'<a href="{esc(w)}" target="_blank" rel="noopener noreferrer nofollow">{esc(host)} &#8599;</a>')
    return ('<div class="company-block">' + (f'<div class="company-chips">{"".join(chips)}</div>' if chips else "")
            + summary + (f'<div class="company-facts">{"".join(facts)}</div>' if facts else "") + "</div>")


def earnings_html(e: Optional[dict]) -> str:
    if not e:
        return ""
    if is_num(e.get("days")) and (e.get("next") or e["days"] >= 0):
        when = "today" if e["days"] == 0 else ("tomorrow" if e["days"] == 1 else f"in {e['days']} days")
        date = (f" &middot; {fmt_day_long(e['next'])}" + (" (estimated date)" if e.get("estimate") else "")
                if e.get("next") else "")
        return f'<div class="earn-callout{" soon" if e["days"] <= 14 else ""}"><strong>Next earnings {when}</strong>{date}</div>'
    if e.get("last"):
        ago = "today" if e.get("days_ago") == 0 else f'{e.get("days_ago")} day{"" if e.get("days_ago") == 1 else "s"} ago'
        return (f'<div class="earn-callout"><strong>Reported earnings {ago}</strong> &middot; {fmt_day_long(e["last"])}'
                ". Recent price and volume moves may reflect the report.</div>")
    return ""


def peers(vm: dict, data: SiteData, by_ticker: Dict[str, dict], limit: int = 8) -> List[dict]:
    same_ind = [u for u in data.universe if u["industry"] == vm["industry"] and u["t"] != vm["t"]]
    pool = same_ind if len(same_ind) >= 3 else [u for u in data.universe
                                                 if u["sector"] == vm["sector"] and u["t"] != vm["t"]]
    return sorted(pool, key=lambda u: u.get("mr") or 10**6)[:limit]


def stock_page(vm: dict, data: SiteData, shell: "Shell", by_ticker: Dict[str, dict]) -> str:
    t, name = vm["t"], vm["name"]
    url = f"{shell.site}/stock/{vm['slug']}"
    sd = data.session_date
    lst = vm["list"]
    list_name = {1: "Momentum Leaders", 2: "Trend Quality &amp; Value"}

    title = f"{t} Stock: {title_name(name)} Trend & Momentum | Stonks Run"
    desc = (f"{name} ({t}) meets {vm['tn']} of 8 Trend Template criteria"
            + (f" with an RS Rating of {vm['rs']}" if is_num(vm["rs"]) else "")
            + (f" and ranks #{vm['mr']} of {data.total_scored} on momentum" if is_num(vm["mr"]) else "")
            + (f" as of {fmt_day(sd, True)}" if sd else "")
            + ". Moving averages, relative strength, screen history and fundamentals, updated hourly.")

    if lst:
        status = (f'<div class="sp-onlist">On today\'s <a href="/#{"momentum" if lst["sec"] == 1 else "value"}">'
                  f'{list_name[lst["sec"]]}</a> list at <strong>#{lst["rank"]}</strong>.</div>')
    else:
        status = ('<div class="sp-onlist sp-off">Not on today\'s lists'
                  + (f' &mdash; ranks #{vm["mr"]} of {data.total_scored} on momentum.' if is_num(vm["mr"]) else ".")
                  + "</div>")

    rs = vm["rs"]
    tiles = (
        '<div class="tiles sp-tiles">'
        f'<div class="tile"><div class="tile-label" data-tip="{esc(TIPS["trend"])}" tabindex="0">Trend Template</div>'
        f'<div class="tile-value">{vm["tn"]}<small>/8</small></div>'
        f'<div class="meter"><span style="width:{vm["tn"] / 8 * 100:.0f}%"></span></div>'
        f'<div class="tile-sub">{"Qualifies" if vm["q"] else "Does not qualify"}</div></div>'
        f'<div class="tile"><div class="tile-label" data-tip="{esc(TIPS["rs"])}" tabindex="0">RS Rating</div>'
        f'<div class="tile-value">{rs if is_num(rs) else "—"}<small>/99</small></div>'
        f'<div class="meter"><span style="width:{(rs or 0):.0f}%"></span></div>'
        '<div class="tile-sub">vs. the scanned universe</div></div>'
        f'<div class="tile"><div class="tile-label">Momentum Rank</div>'
        f'<div class="tile-value">{"#" + str(vm["mr"]) if is_num(vm["mr"]) else "—"}</div>'
        f'<div class="tile-sub">of {data.total_scored} scored</div></div>'
        f'<div class="tile"><div class="tile-label">Quality Rank</div>'
        f'<div class="tile-value">{"#" + str(vm["qr"]) if is_num(vm["qr"]) else "—"}</div>'
        f'<div class="tile-sub">trend quality &amp; value</div></div>'
        '</div>')

    m, price = vm["m"], vm["price"]
    chart = ""
    if vm["spark"] and vm["spark_dates"]:
        c, s = vm["spark"].get("c") or [], vm["spark"].get("sma50") or []
        # "</" inside a <script> block would end it early; JSON allows "<\/".
        payload = json.dumps({"x": vm["spark_dates"], "c": c, "m": s}, separators=(",", ":")).replace("</", "<\\/")
        chart = ('<h2 id="chart">Six-Month Price</h2><p class="sp-sub">Daily closes with the 50-day average. '
                 'Hover or use arrow keys for values.</p><div class="chart-wrap"><div class="viz-legend">'
                 '<span class="lg"><span class="lg-line" style="background:var(--series-1)"></span>Close</span>'
                 '<span class="lg"><span class="lg-line" style="background:var(--series-2)"></span>50-day average'
                 f'</span></div><div class="viz" id="price-chart">{static_chart_svg(c, s)}</div></div>'
                 f'<script type="application/json" id="price-data">{payload}</script>')

    momentum_cells = [
        metric_cell("3-Mo Return", fmt_pct(m.get("ret_3m_pct"))),
        metric_cell("6-Mo Return", fmt_pct(m.get("ret_6m_pct"))) if is_num(m.get("ret_6m_pct")) else "",
        metric_cell("12-Mo Return", fmt_pct(m.get("ret_12m_pct"))),
        metric_cell("ADR (20d)", fmt_num(m.get("adr20_pct"), 2) + "%" if is_num(m.get("adr20_pct")) else "—",
                    tip=TIPS["adr"]),
    ]
    if is_num(m.get("avg_dollar_vol20")):
        momentum_cells.append(metric_cell("Avg $ Vol (20d)", "$" + fmt_vol(m["avg_dollar_vol20"])))
    if is_num(m.get("prior_50d_high")):
        momentum_cells.append(metric_cell("50d Breakout Lvl", fmt_money(m["prior_50d_high"]),
                                          ma_distance(price, m["prior_50d_high"])))
    if is_num(vm["rv"]):
        momentum_cells.append(metric_cell("Relative Volume", fmt_num(vm["rv"], 2) + "x"))

    peer_rows = "".join(
        f'<tr><td><a class="tk-link" href="/stock/{slug(p["t"])}">{esc(p["t"])}</a></td>'
        f'<td class="co">{esc(p.get("n"))}</td><td>{trend_bar(int(p.get("tn") or 0))} {int(p.get("tn") or 0)}/8</td>'
        f'<td class="num">{p.get("rs") if is_num(p.get("rs")) else "—"}</td>'
        f'<td class="num">#{p.get("mr")}</td></tr>'
        for p in peers(vm, data, by_ticker))
    group = vm["industry"] if vm["industry"] and sum(
        1 for u in data.universe if u["industry"] == vm["industry"]) >= 4 else vm["sector"]
    peer_html = ""
    if peer_rows:
        peer_html = (f'<h2 id="peers">{esc(group)} Peers</h2><p class="sp-sub">Other stocks in the same group, by '
                     'momentum rank in the latest scan.</p><div class="table-scroll"><table class="data-table">'
                     '<thead><tr><th>Ticker</th><th>Company</th><th>Trend</th><th class="num">RS</th>'
                     f'<th class="num">Mom. rank</th></tr></thead><tbody>{peer_rows}</tbody></table></div>')

    readout = "".join(f'<div class="rd-line">{l}</div>' for l in readout_lines(vm, data))
    as_of = (f'Screen data from the {fmt_day_long(sd)} session &middot; updated {fmt_ts_et(data.generated_at)}'
             if sd else "Screen data from the latest scan")

    body = f"""
    <article class="article col-main stock-page">
    <nav class="crumbs" aria-label="Breadcrumb"><a href="/">Screen</a> <span aria-hidden="true">&rsaquo;</span>
      <a href="/stock/">Stocks</a> <span aria-hidden="true">&rsaquo;</span> <span>{esc(t)}</span></nav>
    <p class="article-eyebrow">{esc(vm['sector'] or 'Stock')}{(' &middot; ' + esc(vm['industry'])) if vm['industry'] else ''}</p>
    <h1>{esc(name)} <span class="sp-ticker">({esc(t)})</span></h1>
    <div class="article-meta">{as_of}. Prices delayed ~15&ndash;20 minutes.</div>
    <div class="sp-price">{fmt_money(price)} <span class="{chg_class(vm['chg'])}">{fmt_pct(vm['chg'])}</span></div>
    {status}
    {tiles}
    {earnings_html(vm['earnings'])}
    <h2 id="readout">What the Screen Sees</h2>
    <div class="readout">{readout}</div>
    {chart}
    <h2 id="trend-template">Trend Template Checklist</h2>
    <p class="sp-sub">{vm['tn']} of Minervini's 8 criteria currently met. <a href="/trend-template">What each
    criterion means &rarr;</a></p>
    {checklist_html(vm)}
    <h2 id="averages">Moving Averages &amp; 52-Week Range</h2>
    {range_bar(price, m.get('wk52_low'), m.get('wk52_high'))}
    {ma_stack(price, m)}
    <div class="metric-grid">{metric_cell('50-Day Avg', fmt_money(m.get('sma50')), ma_distance(price, m.get('sma50')), TIPS['sma50'])}{metric_cell('150-Day Avg', fmt_money(m.get('sma150')), ma_distance(price, m.get('sma150')), TIPS['sma150'])}{metric_cell('200-Day Avg', fmt_money(m.get('sma200')), ma_distance(price, m.get('sma200')), TIPS['sma200'])}</div>
    <h2 id="momentum">Momentum &amp; Volatility</h2>
    <div class="metric-grid">{''.join(momentum_cells)}</div>
    {signals_html(vm)}
    {fundamentals_html(m, price)}
    <h2 id="record">Screen Record</h2>
    {record_html(vm, data)}
    <h2 id="company">About {esc(short_name(name))}</h2>
    {company_html(vm)}
    {peer_html}
    <div class="modal-links">
      <a href="/#t={esc(t)}">Open {esc(t)} on the live screen &rarr;</a>
      {f'<a href="/history#t={esc(t)}">Full screen history for {esc(t)} &rarr;</a>' if vm['record'] else ''}
      <a href="https://finance.yahoo.com/quote/{esc(t)}/chart" target="_blank" rel="noopener noreferrer nofollow">Real-time chart on Yahoo Finance &rarr;</a>
      <span class="ml-note">External site. Prices here are delayed; verify against a real-time source.</span>
    </div>
    <div class="modal-footnote">This page describes what the data shows against published screening criteria. It is
    not a recommendation to buy or sell {esc(t)}, and it does not account for company news or your own
    circumstances. Company descriptions and analyst figures come from Yahoo Finance. See the
    <a href="/methodology">methodology</a> and <a href="/terms">terms</a>.</div>
    </article>
    {shell.rail(data, exclude=t)}"""

    jsonld = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage", "@id": url, "url": url, "name": f"{name} ({t}) trend & momentum screen",
             "description": desc, "inLanguage": "en-US", "isPartOf": {"@id": f"{shell.site}/#website"},
             **({"dateModified": sd} if sd else {}),
             "about": {"@type": "Corporation", "name": name}},
            {"@type": "BreadcrumbList", "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Screen", "item": f"{shell.site}/"},
                {"@type": "ListItem", "position": 2, "name": "Stocks", "item": f"{shell.site}/stock/"},
                {"@type": "ListItem", "position": 3, "name": t, "item": url}]},
        ],
    }
    script = """<script>
(function () {
  var el = document.getElementById('price-chart'), src = document.getElementById('price-data');
  if (!el || !src || !window.SR) return;
  var d = JSON.parse(src.textContent);
  SR.lineChart(el, {
    x: d.x, height: 220,
    series: [
      { name: 'Close', values: d.c, color: 'var(--series-1)', tipFmt: SR.fmtMoney },
      { name: '50-day avg', values: d.m, color: 'var(--series-2)', tipFmt: SR.fmtMoney }
    ],
    yFmt: function (v) { return '$' + SR.fmtNum(v, v < 20 ? 1 : 0); },
    xFmt: function (s) { return SR.fmtDay(s); },
    tipTitle: function (i) { return SR.fmtDayLong(d.x[i]); },
    ariaLabel: document.title.split(' Stock')[0] + ' daily closing price and 50-day average over six months'
  });
})();
</script>""" if chart else ""
    return shell.page(title=title, desc=desc, path=f"/stock/{vm['slug']}", body=body, jsonld=jsonld,
                      active="/stock/", tail=script, og_type="article")


def former_stock_page(t: str, data: SiteData, shell: "Shell") -> str:
    """A ticker that was on the lists but is no longer in the scanned universe
    (left the index, acquired, renamed). Kept so History links never 404;
    marked noindex because there is no current data on it."""
    names = (data.history.get("names") or {}).get(t) or []
    co = data.companies.get(t, {})
    name = (names[0] if names else None) or co.get("n") or t
    vm = {"t": t, "slug": slug(t), "name": name, "sector": (names[1] if len(names) > 1 else "") or co.get("s") or "",
          "industry": (names[2] if len(names) > 2 else "") or co.get("i") or "", "mr": None, "qr": None,
          "record": data.records.get(t),
          "profile": {"summary": co.get("d"), "website": co.get("w"), "hq": co.get("hq"), "employees": co.get("e")}}
    title = f"{t}: {short_name(name)} screen history | Stonks Run"
    desc = f"{name} ({t}) is no longer in the S&P 500/400 universe the screen scans. Its past record on the lists."
    body = f"""
    <article class="article col-main stock-page">
    <nav class="crumbs" aria-label="Breadcrumb"><a href="/">Screen</a> <span aria-hidden="true">&rsaquo;</span>
      <a href="/stock/">Stocks</a> <span aria-hidden="true">&rsaquo;</span> <span>{esc(t)}</span></nav>
    <p class="article-eyebrow">{esc(vm['sector'] or 'Former constituent')}</p>
    <h1>{esc(name)} <span class="sp-ticker">({esc(t)})</span></h1>
    <div class="sp-onlist sp-off">No longer in the scanned universe &mdash; it left the S&amp;P 500/400 (or was
    acquired or renamed), so the screen has no current data on it. Its record while it was scanned is below.</div>
    <h2 id="record">Screen Record</h2>
    {record_html(vm, data)}
    <h2 id="company">About {esc(short_name(name))}</h2>
    {company_html(vm)}
    <div class="modal-links"><a href="/history#t={esc(t)}">Full screen history for {esc(t)} &rarr;</a>
      <a href="/stock/">Browse every stock currently scanned &rarr;</a></div>
    </article>
    {shell.rail(data)}"""
    return shell.page(title=title, desc=desc, path=f"/stock/{slug(t)}", body=body, jsonld=None,
                      active="/stock/", robots="noindex, follow")


def directory_page(data: SiteData, shell: "Shell", vms: List[dict]) -> str:
    by_sector: Dict[str, List[dict]] = {}
    for vm in vms:
        by_sector.setdefault(vm["sector"] or "Other", []).append(vm)
    sectors = sorted(by_sector, key=lambda s: (s == "Other", s))
    jump = " &middot; ".join(f'<a href="#{slug(s)}">{esc(s)}</a>' for s in sectors)
    parts = []
    for s in sectors:
        rows = "".join(
            f'<tr data-q="{esc((vm["t"] + " " + vm["name"]).lower())}"><td><a class="tk-link" href="/stock/{vm["slug"]}">'
            f'{esc(vm["t"])}</a></td><td class="co">{esc(vm["name"])}</td><td class="co">{esc(vm["industry"])}</td>'
            f'<td>{trend_bar(vm["tn"])} {vm["tn"]}/8</td><td class="num">{vm["rs"] if is_num(vm["rs"]) else "—"}</td>'
            f'<td class="num">{"#" + str(vm["mr"]) if is_num(vm["mr"]) else "—"}</td></tr>'
            for vm in sorted(by_sector[s], key=lambda v: v["t"]))
        st = data.sector_stats.get(s)
        sub = (f'{len(by_sector[s])} stocks &middot; {st["pct_qualified"]:.0f}% clear the Trend Template gate'
               if st and is_num(st.get("pct_qualified")) else f"{len(by_sector[s])} stocks")
        parts.append(f'<section class="dir-sector"><h2 id="{slug(s)}">{esc(s)}</h2><p class="sp-sub">{sub}</p>'
                     '<div class="table-scroll"><table class="data-table"><thead><tr><th>Ticker</th><th>Company</th>'
                     '<th>Industry</th><th>Trend</th><th class="num">RS</th><th class="num">Mom. rank</th></tr>'
                     f'</thead><tbody>{rows}</tbody></table></div></section>')
    sd = data.session_date
    title = "All Stocks on the Screen: Trend Template, RS & Momentum Rank | Stonks Run"
    desc = (f"Every one of the {len(vms)} stocks the screen scores, by sector, with Trend Template compliance, "
            "RS Rating and momentum rank. Each links to a full breakdown.")
    body = f"""
    <article class="article col-main dir-page">
    <nav class="crumbs" aria-label="Breadcrumb"><a href="/">Screen</a> <span aria-hidden="true">&rsaquo;</span> <span>Stocks</span></nav>
    <p class="article-eyebrow">Directory</p>
    <h1>Every Stock on the Screen</h1>
    <div class="article-meta">{len(vms)} stocks{(' &middot; data from the ' + fmt_day_long(sd) + ' session') if sd else ''}</div>
    <p>The screen scores every S&amp;P 500 and S&amp;P 400 stock that clears its price and liquidity filters, every
    hour the market is open. Each stock has its own page with the full Trend Template checklist, moving averages,
    relative strength, framework signals, fundamentals and its record on the screen&rsquo;s lists.</p>
    <div class="lookup" role="search"><label for="dir-filter">Filter by ticker or company</label>
    <input id="dir-filter" type="search" autocomplete="off" spellcheck="false" placeholder="e.g. NVDA or bank"></div>
    <p class="dir-jump">{jump}</p>
    {''.join(parts)}
    </article>
    {shell.rail(data)}"""
    script = """<script>
(function () {
  var box = document.getElementById('dir-filter');
  if (!box) return;
  var rows = Array.prototype.slice.call(document.querySelectorAll('tr[data-q]'));
  var secs = Array.prototype.slice.call(document.querySelectorAll('.dir-sector'));
  box.addEventListener('input', function () {
    var q = box.value.trim().toLowerCase();
    rows.forEach(function (r) { r.hidden = q && r.getAttribute('data-q').indexOf(q) < 0; });
    secs.forEach(function (s) { s.hidden = q && !s.querySelector('tr[data-q]:not([hidden])'); });
  });
})();
</script>"""
    jsonld = {"@context": "https://schema.org", "@type": "CollectionPage", "url": f"{shell.site}/stock/",
              "name": "Every stock on the Stonks Run screen", "description": desc,
              "isPartOf": {"@id": f"{shell.site}/#website"}}
    return shell.page(title=title, desc=desc, path="/stock/", body=body, jsonld=jsonld, active="/stock/",
                      tail=script)


# --------------------------------------------------------------------------
# Page shell (head, masthead, footer) - taken from the built learn page so
# generated pages always match the hand-edited ones.
# --------------------------------------------------------------------------

class Shell:
    def __init__(self, site: str, learn_html: str, index_src: str, head_extra: str, body_extra: str):
        self.site = site
        self.head_extra = head_extra
        self.body_extra = body_extra
        self.masthead = re.search(r'<header class="masthead">.*?</header>', learn_html, re.S).group(0)
        self.masthead = re.sub(r'\s*class="active" aria-current="page"', "", self.masthead)
        self.footer = re.search(r"<footer>.*?</footer>", learn_html, re.S).group(0)
        fonts = re.search(r'<link href="https://fonts\.googleapis\.com/css2[^>]*>', index_src)
        ads = re.search(r'<script async src="https://pagead2\.googlesyndication\.com[^>]*>\s*</script>', index_src, re.S)
        self.fonts = fonts.group(0) if fonts else ""
        self.ads = ads.group(0) if ads else ""
        self.css = ""
        self.js = ""
        self.leaders: List[dict] = []

    def rail(self, data: SiteData, exclude: Optional[str] = None) -> str:
        rows = [r for r in (data.latest.get("section1") or []) if r.get("ticker") != exclude][:10]
        leaders = "".join(
            f'<div class="wl-row" style="cursor:default"><a class="tk-link wl-t" href="/stock/{slug(r["ticker"])}">'
            f'{esc(r["ticker"])}</a><span class="wl-meta">{esc(short_name(r.get("company") or ""))}</span>'
            f'<span class="wl-chg {chg_class(r.get("pct_chg"))}">{fmt_pct(r.get("pct_chg"))}</span></div>'
            for r in rows)
        leaders_card = (f'<div class="rail-card"><h3>Today&rsquo;s Momentum Leaders</h3>{leaders}'
                        '<p style="margin-top:10px"><a href="/" class="discord-link">See the full screen &rarr;</a></p></div>'
                        if leaders else "")
        return f"""<aside class="rail">
      {leaders_card}
      <div class="rail-card">
        <h3>Reading This Page</h3>
        <p>Every number here is the mechanical output of the screen&rsquo;s published filters applied to delayed
        public data. It describes a chart and a set of fundamentals; it is not a forecast or a recommendation.</p>
        <ul>
          <li><a href="/reading-the-screen">How to read the screen</a></li>
          <li><a href="/trend-template">The Trend Template, criterion by criterion</a></li>
          <li><a href="/canslim">What CANSLIM measures</a></li>
          <li><a href="/glossary">Glossary of every term</a></li>
        </ul>
      </div>
      <div class="rail-card">
        <h3>Browse</h3>
        <p><a href="/stock/">All stocks by sector &rarr;</a></p>
        <p><a href="/history">Screen history &amp; track record &rarr;</a></p>
      </div>
    </aside>"""

    def page(self, *, title: str, desc: str, path: str, body: str, jsonld: Optional[dict], active: str,
             tail: str = "", og_type: str = "website", robots: str = "index, follow, max-image-preview:large") -> str:
        url = self.site + path
        mast = self.masthead.replace(f'<a href="{active}">', f'<a href="{active}" class="active" aria-current="page">', 1)
        ld = (f'<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False)}</script>'
              if jsonld else "")
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{esc(url)}">
<meta name="robots" content="{robots}">
<meta name="theme-color" content="#05070A">
<meta property="og:type" content="{og_type}">
<meta property="og:site_name" content="Stonks Run">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{esc(url)}">
<meta property="og:image" content="{self.site}/social-card.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(title)}">
<meta name="twitter:description" content="{esc(desc)}">
<meta name="twitter:image" content="{self.site}/social-card.png">
<link rel="icon" href="/favicon.png" type="image/png">
<link rel="apple-touch-icon" href="/favicon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
{self.fonts}
<link rel="stylesheet" href="{self.css}">
{self.ads}
{self.head_extra}
</head>
<body>
<a href="#main-content" class="skip-link">Skip to main content</a>
{ld}
{mast}

<main id="main-content">
<div class="wrap">
  <div class="layout">{body}
  </div>
</div>
</main>

{self.footer}
<script src="{self.js}"></script>
{tail}
{self.body_extra}
</body>
</html>
"""


# --------------------------------------------------------------------------
# Build steps
# --------------------------------------------------------------------------

def copy_source(src: Path, out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        if p.is_dir() or p.name in SKIP_FILES or p.suffix == ".tmp":
            continue
        if rel.parts[0] == "data" and p.name in SKIP_DATA_FILES:
            continue
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)


def rewrite_html(text: str, pages: List[str], site: str, css_url: str, js_url: str) -> str:
    """Clean URLs + root-absolute assets. Source pages keep 'history.html'
    links (so the GitHub Pages copy keeps working); dist gets '/history'."""
    names = "|".join(re.escape(p) for p in sorted(pages, key=len, reverse=True))
    site_re = re.escape(site)
    text = re.sub(rf"{site_re}/index\.html", f"{site}/", text)
    text = re.sub(rf"{site_re}/({names})\.html", lambda mo: f"{site}/{mo.group(1)}", text)
    # Relative page links in attributes and in inline JS strings.
    text = re.sub(rf"(?<![\w./-])index\.html(?=[#\"'?])", "/", text)
    text = re.sub(rf"(?<![\w./-])({names})\.html(?=[#\"'?])", lambda mo: "/" + mo.group(1), text)
    text = re.sub(r'href="styles\.css(\?[^"]*)?"', f'href="{css_url}"', text)
    text = re.sub(r'src="common\.js(\?[^"]*)?"', f'src="{js_url}"', text)
    text = re.sub(r'href="favicon\.png"', 'href="/favicon.png"', text)
    text = re.sub(r"""(["'])data/(\w+\.json)""", r"\1/data/\2", text)
    # "Stocks" in the main and footer navigation (only there - not in body
    # text that happens to link to History), right after History.
    def add_stocks(nav):
        return re.sub(r'(<a href="/history"[^>]*>History</a>)', r'\1\n        <a href="/stock/">Stocks</a>',
                      nav.group(0), count=1)
    text = re.sub(r'<nav class="(?:quicknav|footer-links)".*?</nav>', add_stocks, text, flags=re.S)
    return text


def inject(text: str, head_extra: str, body_extra: str) -> str:
    if head_extra:
        text = text.replace("</head>", head_extra + "\n</head>", 1)
    if body_extra:
        text = text.replace("</body>", body_extra + "\n</body>", 1)
    return text


def replace_once(text: str, old: str, new: str, what: str) -> str:
    if old not in text:
        warn(f"Pre-render: could not find the {what} placeholder in index.html; left it to JavaScript.")
        return text
    return text.replace(old, new, 1)


def prerender_home(text: str, data: SiteData) -> str:
    """Bakes the current lists into index.html. JavaScript re-renders the same
    elements on load, so this only changes what is visible before it runs -
    which is exactly what search engines and slow phones see."""
    L = data.latest
    if not L.get("section1"):
        return text
    cls = status_class(L.get("market_status"))
    text = replace_once(text, '<span id="updated-at">&mdash;</span>',
                        f'<span id="updated-at">{fmt_ts_et(L.get("generated_at"))}</span>', "last-refresh")
    text = replace_once(text, '<div id="market-status" class="status-pill status-unknown"><span class="dot"></span> Market: Loading</div>',
                        f'<div id="market-status" class="status-pill {cls}"><span class="dot"></span> {STATUS_LABEL[cls]}</div>',
                        "market status")
    rows = (L.get("section1") or []) + (L.get("section2") or [])
    strip = "     •     ".join(f'{r["ticker"]} {fmt_pct(r.get("pct_chg"))}' for r in rows) + "     •     "
    text = replace_once(text, '<div class="tt-track" id="ticker-track">LOADING FEED&hellip;</div>',
                        f'<div class="tt-track" id="ticker-track">{esc(strip)}</div>', "ticker strip")
    q = L.get("qualifying_count")
    if q == 0:
        summary = ('<div id="scan-summary" class="scan-summary zero-qualified"><div class="zq-head">No stock currently '
                   'clears the Trend Template gate.</div></div>')
    else:
        summary = (f'<div id="scan-summary" class="scan-summary"><span class="label">Universe scanned</span> &nbsp; '
                   f'<strong>{L.get("total_scored")}</strong> stocks &nbsp;&middot;&nbsp; <span class="label">Fully '
                   f'qualified</span> &nbsp; <strong>{q}</strong> (&ge; {L.get("min_trend_needed")}/8 Trend Template)'
                   ' &nbsp;&middot;&nbsp; <span class="label">Remaining names shown are the closest available '
                   'candidates</span></div>')
    text = replace_once(text, '<div id="scan-summary"></div>', summary, "scan summary")
    text = replace_once(text, '<div id="section1-mount"></div>',
                        f'<div id="section1-mount">{ledger_table(L.get("section1") or [], 1)}</div>', "momentum list")
    text = replace_once(text, '<div id="section2-mount"></div>',
                        f'<div id="section2-mount">{ledger_table(L.get("section2") or [], 2)}</div>', "quality list")
    return text


def write_sitemap(out: Path, site: str, src_sitemap: Path, pages: List[str], stock_slugs: List[str],
                  session_date: Optional[str]) -> int:
    meta = {}
    if src_sitemap.exists():
        for block in re.findall(r"<url>(.*?)</url>", src_sitemap.read_text(encoding="utf-8"), re.S):
            loc = re.search(r"<loc>(.*?)</loc>", block)
            if not loc:
                continue
            path = loc.group(1).replace(site, "") or "/"
            path = "/" if path in ("/", "/index.html") else re.sub(r"\.html$", "", path)
            meta[path] = {k: (re.search(rf"<{k}>(.*?)</{k}>", block) or [None, None])[1]
                          for k in ("lastmod", "changefreq", "priority")}
    entries = []
    for p in ["index"] + sorted(x for x in pages if x not in ("index", "404")):
        path = "/" if p == "index" else "/" + p
        m = meta.get(path, {})
        lastmod = session_date if path in ("/", "/history") and session_date else m.get("lastmod")
        entries.append((path, lastmod, m.get("changefreq") or "monthly", m.get("priority") or "0.5"))
    if stock_slugs:
        entries.append(("/stock/", session_date, "daily", "0.8"))
        entries += [(f"/stock/{s}", session_date, "daily", "0.6") for s in stock_slugs]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, lastmod, freq, prio in entries:
        lines.append("  <url>")
        lines.append(f"    <loc>{esc(site + path)}</loc>")
        if lastmod:
            lines.append(f"    <lastmod>{lastmod}</lastmod>")
        lines.append(f"    <changefreq>{freq}</changefreq>")
        lines.append(f"    <priority>{prio}</priority>")
        lines.append("  </url>")
    lines.append("</urlset>")
    (out / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(entries)


def write_cloudflare_files(out: Path, site: str, pages: List[str]) -> None:
    (out / "robots.txt").write_text(
        "User-agent: *\nAllow: /\n\n"
        "# /data/*.json is fetched by the pages' own scripts; it is kept out of\n"
        "# search results with an X-Robots-Tag header (see _headers) rather than\n"
        "# blocked here, because blocking it would stop Google rendering pages.\n\n"
        f"Sitemap: {site}/sitemap.xml\n", encoding="utf-8")

    (out / "_headers").write_text("""# Cloudflare static-asset headers (generated by tools/build_site.py)
/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: camera=(), microphone=(), geolocation=()
  Strict-Transport-Security: max-age=31536000

# Fingerprinted by the build (?v=<hash> changes whenever the file does).
/styles.css
  Cache-Control: public, max-age=31536000, immutable
/common.js
  Cache-Control: public, max-age=31536000, immutable

/favicon.png
  Cache-Control: public, max-age=604800
/social-card.png
  Cache-Control: public, max-age=604800

# Scan data: refreshed hourly; pages fetch it with cache: no-store anyway.
/data/*
  Cache-Control: public, max-age=60
  X-Robots-Tag: noindex

/404.html
  X-Robots-Tag: noindex

# Keep the *.workers.dev preview copy out of search results.
https://:version.:subdomain.workers.dev/*
  X-Robots-Tag: noindex
""", encoding="utf-8")

    lines = ["# Old .html addresses -> clean URLs (permanent). Generated by tools/build_site.py.",
             "/index.html / 301"]
    lines += [f"/{p}.html /{p} 301" for p in sorted(pages) if p not in ("index", "404")]
    (out / "_redirects").write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_links(out: Path) -> List[str]:
    """Every root-absolute or relative link in dist/ must resolve to a file."""
    missing = []
    for page in out.rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        for ref in re.findall(r'(?:href|src)="([^"]+)"', text):
            if re.match(r"^(https?:|mailto:|tel:|#|data:|javascript:|//)", ref) or "{" in ref or "'" in ref:
                continue
            path = ref.split("#")[0].split("?")[0]
            if not path:
                continue
            target = (out / path.lstrip("/")) if path.startswith("/") else (page.parent / path)
            ok = (target.is_file() or (target / "index.html").is_file()
                  or target.with_name(target.name + ".html").is_file() or path == "/")
            if not ok:
                missing.append(f"{page.relative_to(out)} -> {ref}")
    return sorted(set(missing))


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the deployable Stonks Run site into dist/")
    ap.add_argument("--src", default="docs")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--site-url", default=os.environ.get("SITE_URL", "https://stonks.run"))
    ap.add_argument("--strict", action="store_true", help="Exit non-zero if any internal link is broken")
    args = ap.parse_args()

    src, out, site = Path(args.src), Path(args.out), args.site_url.rstrip("/")
    if not (src / "index.html").exists():
        print(f"ERROR: {src}/index.html not found")
        return 1

    copy_source(src, out)
    pages = sorted(p.stem for p in src.glob("*.html"))
    css_url = f"/styles.css?v={file_hash(src / 'styles.css')}"
    js_url = f"/common.js?v={file_hash(src / 'common.js')}"

    cf_token = (os.environ.get("CF_WEB_ANALYTICS_TOKEN") or "").strip()
    gsv = (os.environ.get("GOOGLE_SITE_VERIFICATION") or "").strip()
    head_extra = "<script>window.SR_STATIC={stockPages:true};</script>"
    if re.fullmatch(r"[\w-]{8,128}", gsv):
        head_extra += f'\n<meta name="google-site-verification" content="{esc(gsv)}">'
    body_extra = ""
    if re.fullmatch(r"[\w-]{8,128}", cf_token):
        body_extra = ("<script defer src='https://static.cloudflareinsights.com/beacon.min.js' "
                      f"data-cf-beacon='{{\"token\": \"{cf_token}\"}}'></script>")

    data = SiteData(src / "data")

    # 1-2. Rewrite the hand-edited pages.
    for name in pages:
        p = out / f"{name}.html"
        text = rewrite_html(p.read_text(encoding="utf-8"), [x for x in pages if x != "404"], site, css_url, js_url)
        text = inject(text, head_extra, body_extra)
        if name == "index":
            text = prerender_home(text, data)
        if name == "404":
            text = text.replace('<link rel="canonical" href="https://stonks.run/404.html">', "")
            text = re.sub(r'<meta name="robots" content="[^"]*">', '<meta name="robots" content="noindex">', text)
            # Stock pages are lowercase (/stock/nvda); send /stock/NVDA there.
            text = text.replace("</head>", "<script>(function(){var p=location.pathname;"
                                "if(/^\\/stock\\/.*[A-Z]/.test(p))location.replace(p.toLowerCase()+location.hash);})();"
                                "</script>\n</head>", 1)
        p.write_text(text, encoding="utf-8")
    for js in out.glob("*.js"):
        js.write_text(re.sub(r"""(["'])data/(\w+\.json)""", r"\1/data/\2", js.read_text(encoding="utf-8")),
                      encoding="utf-8")

    # 3-4. Generated pages.
    shell = Shell(site, (out / "learn.html").read_text(encoding="utf-8"), (src / "index.html").read_text(encoding="utf-8"),
                  head_extra, body_extra)
    shell.css, shell.js = css_url, js_url
    by_ticker = {u["t"]: u for u in data.universe}
    vms = [data.stock(u) for u in data.universe]
    stock_dir = out / "stock"
    stock_dir.mkdir(exist_ok=True)
    seen = set()
    for vm in vms:
        if vm["slug"] in seen:
            warn(f"Two tickers share the page address /stock/{vm['slug']}; skipped {vm['t']}.")
            continue
        seen.add(vm["slug"])
        (stock_dir / f"{vm['slug']}.html").write_text(stock_page(vm, data, shell, by_ticker), encoding="utf-8")
    former = 0
    for t in sorted(set(data.records) - set(by_ticker)):
        if slug(t) in seen:
            continue
        (stock_dir / f"{slug(t)}.html").write_text(former_stock_page(t, data, shell), encoding="utf-8")
        former += 1
    if former:
        print(f"Wrote {former} page(s) for former constituents (noindex, kept so History links resolve).")
    if vms:
        (stock_dir / "index.html").write_text(directory_page(data, shell, vms), encoding="utf-8")
    else:
        warn("universe.json is missing or empty, so no stock pages were generated.")
        shutil.rmtree(stock_dir)
        for p in out.glob("*.html"):   # no stock pages: drop the nav link and the JS ticker links
            text = (p.read_text(encoding="utf-8").replace('\n        <a href="/stock/">Stocks</a>', "")
                    .replace("window.SR_STATIC={stockPages:true}", "window.SR_STATIC={stockPages:false}"))
            text = re.sub(r'<a class="tk-link" href="/stock/[^"]*">([^<]*)</a>', r"\1", text)
            p.write_text(text, encoding="utf-8")

    # 5. Sitemap, robots, Cloudflare headers/redirects.
    n_urls = write_sitemap(out, site, src / "sitemap.xml", pages, sorted(seen), data.session_date)
    write_cloudflare_files(out, site, pages)

    # 6. Link check.
    missing = check_links(out)
    for m in missing[:25]:
        warn(f"Broken internal link: {m}")
    files = sum(1 for p in out.rglob("*") if p.is_file())
    print(f"Built {out}/: {files} files, {len(seen)} stock pages, {n_urls} sitemap URLs, "
          f"session {data.session_date or 'n/a'}"
          + (f", stocks.json detail for {len(data.details)} stocks" if data.details else ", no stocks.json yet"))
    if files > 19000:
        warn(f"{files} files is close to Cloudflare's free-plan limit of 20,000 per deploy.")
    if missing and args.strict:
        print(f"ERROR: {len(missing)} broken internal link(s).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
