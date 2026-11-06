#!/usr/bin/env python3
"""
tools/refresh_index_members.py
==============================
Keeps universe/index_members.json (which stocks belong to which index)
current, so the screen's universe never drifts out of date again. The
scheduled workflow runs it about once a week, before the scan.

SOURCES
  S&P 500, S&P MidCap 400   Wikipedia constituents tables (edited within hours
                            of each S&P Dow Jones Indices announcement)
  Nasdaq-100                Nasdaq's own list (api.nasdaq.com)
  Dow Jones Transportation  Wikipedia component tables
  and Utility Averages
  Dow Jones Industrial      CHANGE DETECTION ONLY. Wikipedia lists the 30
                            members by company name, not ticker, so the tool
                            compares those names with the ones stored in the
                            file and raises a warning when they differ. The
                            Dow changes about once a year; edit the file then.

SAFETY RULES (a source that breaks one is ignored and the previous list kept)
  - member count inside the expected range for that index
  - every ticker well-formed (Yahoo style: BRK.B becomes BRK-B)
  - no more than a set share of the list changed since the last check, so a
    vandalised or half-loaded page can't swap out the universe
  - the tool never fails the workflow: problems become ::warning:: lines in
    the run summary and the scan carries on with the last good lists

USAGE
    python tools/refresh_index_members.py                     # repo copy -> docs/data/index_members.json
    python tools/refresh_index_members.py --in A.json --out B.json
    python tools/refresh_index_members.py --if-older-than-days 6
    python tools/refresh_index_members.py --selftest           # offline tests

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
REPO_FILE = ROOT / "universe" / "index_members.json"
STATE_FILE = ROOT / "docs" / "data" / "index_members.json"

ORDER = ("SP500", "SP400", "NDX", "DJIA", "DJTA", "DJUA")
COUNT_RANGE = {"SP500": (495, 512), "SP400": (390, 410), "NDX": (98, 104),
               "DJIA": (30, 30), "DJTA": (20, 20), "DJUA": (15, 15)}
# Largest believable change between two checks, as members added + removed.
# S&P's quarterly rebalance moves roughly 5-15 names in the 400 at once.
MAX_CHURN = {"SP500": 30, "SP400": 45, "NDX": 12, "DJTA": 4, "DJUA": 4}
TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z]{1,2})?$")

WIKI_API = "https://en.wikipedia.org/w/api.php"
NASDAQ_100_API = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
WIKI_PAGES = {
    "SP500": "List of S&P 500 companies",
    "SP400": "List of S&P 400 companies",
    "DJTA": "Dow Jones Transportation Average",
    "DJUA": "Dow Jones Utility Average",
}
DJIA_TEMPLATE = "Template:Dow Jones Industrial Average companies"
# Wikimedia asks automated clients to identify themselves with a contact.
UA = "StonksRunIndexRefresh/1.0 (https://stonks.run; stonksrun@gmail.com)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")

IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))


def note(kind: str, msg: str) -> None:
    """kind: notice | warning. Shown on the Actions run summary."""
    print((f"::{kind}::" if IN_ACTIONS else f"{kind.upper()}: ") + msg)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

class TableParser(HTMLParser):
    """Collects every <table> as {"id", "class", "rows": [[cell text...]]}.
    Nested tables are flattened into their own entries."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: List[dict] = []
        self._stack: List[dict] = []
        self._cell: Optional[List[str]] = None
        self._skip = 0  # inside <style>/<script>/<sup> (footnote markers)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("style", "script", "sup"):
            self._skip += 1
        elif tag == "table":
            t = {"id": a.get("id") or "", "class": a.get("class") or "", "rows": []}
            self.tables.append(t)
            self._stack.append(t)
        elif tag == "tr" and self._stack:
            self._stack[-1]["rows"].append([])
        elif tag in ("td", "th") and self._stack and self._stack[-1]["rows"]:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "sup"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._cell is not None and self._stack and self._stack[-1]["rows"]:
            self._stack[-1]["rows"][-1].append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "table" and self._stack:
            self._stack.pop()

    def handle_data(self, data):
        if self._cell is not None and not self._skip:
            self._cell.append(data)


def tickers_from_tables(html_text: str, prefer_id: str = "constituents") -> List[str]:
    """Ticker column of the first table whose header row has a Symbol/Ticker
    column (the table with id=prefer_id first, if there is one)."""
    p = TableParser()
    p.feed(html_text)
    tables = sorted(p.tables, key=lambda t: t["id"] != prefer_id)
    for t in tables:
        if not t["rows"]:
            continue
        header = [c.lower() for c in t["rows"][0]]
        col = next((i for i, h in enumerate(header) if re.match(r"^(symbol|ticker)\b", h)), None)
        if col is None:
            continue
        out = []
        for row in t["rows"][1:]:
            if len(row) > col and row[col]:
                out.append(row[col].split()[0])
        if out:
            return out
    return []


def djia_names_from_wikitext(wikitext: str) -> List[str]:
    """Display names in the DJIA navbox: '* [[Alphabet Inc.|Alphabet]]' -> 'Alphabet'."""
    names = []
    for m in re.finditer(r"^\*\s*\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", wikitext, re.M):
        names.append((m.group(2) or m.group(1)).strip())
    return names


def normalize(tickers: List[str]) -> List[str]:
    out = []
    for t in tickers:
        t = t.strip().upper().replace(".", "-")
        if t and t not in out:
            out.append(t)
    return out


def check_list(code: str, new: List[str], old: List[str]) -> Optional[str]:
    """Reason to reject a freshly fetched list, or None to accept it."""
    lo, hi = COUNT_RANGE[code]
    if not lo <= len(new) <= hi:
        return f"{len(new)} members (expected {lo}-{hi})"
    bad = [t for t in new if not TICKER_RE.match(t)]
    if bad:
        return f"malformed tickers {bad[:5]}"
    if old:
        churn = len(set(new) ^ set(old))
        if churn > MAX_CHURN.get(code, 0):
            return f"{churn} names changed at once (limit {MAX_CHURN.get(code, 0)}) - looks like a bad source page"
    return None


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def http_get(url: str, headers: Dict[str, str], attempts: int = 3, timeout: int = 30) -> bytes:
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # network errors, HTTP errors, timeouts
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET {url.split('?')[0]} failed: {last}")


def wiki_parse(page: str, prop: str) -> str:
    q = urllib.parse.urlencode({"action": "parse", "page": page, "prop": prop, "format": "json",
                                "formatversion": "2", "redirects": "1"})
    data = json.loads(http_get(f"{WIKI_API}?{q}", {"User-Agent": UA, "Accept": "application/json"}))
    if "parse" not in data:
        raise RuntimeError(f"Wikipedia returned no page for {page!r}: {str(data)[:200]}")
    return data["parse"]["text" if prop == "text" else "wikitext"]


def fetch_nasdaq_100() -> List[str]:
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*",
               "Accept-Language": "en-US,en;q=0.9", "Origin": "https://www.nasdaq.com",
               "Referer": "https://www.nasdaq.com/"}
    data = json.loads(http_get(NASDAQ_100_API, headers, timeout=20))
    rows = (((data or {}).get("data") or {}).get("data") or {}).get("rows") or []
    return [r.get("symbol", "") for r in rows if r.get("symbol")]


def fetch_all() -> Tuple[Dict[str, List[str]], Optional[List[str]], Dict[str, str]]:
    """Returns (fresh lists by index, DJIA names or None, errors by index)."""
    lists: Dict[str, List[str]] = {}
    errors: Dict[str, str] = {}
    for code, page in WIKI_PAGES.items():
        try:
            lists[code] = normalize(tickers_from_tables(wiki_parse(page, "text")))
        except Exception as e:
            errors[code] = str(e)
    try:
        lists["NDX"] = normalize(fetch_nasdaq_100())
    except Exception as e:
        errors["NDX"] = str(e)
    names = None
    try:
        names = djia_names_from_wikitext(wiki_parse(DJIA_TEMPLATE, "wikitext"))
    except Exception as e:
        errors["DJIA"] = str(e)
    return lists, names, errors


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------

def load(path: Path) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def freshest(paths: List[Path]) -> Optional[dict]:
    """Most recently checked file; later paths win ties (same rule as the
    scanner), so a hand-fixed repo copy dated today takes effect."""
    best = None
    for d in (load(p) for p in paths):
        if isinstance(d, dict) and isinstance(d.get("indexes"), dict):
            if best is None or str(d.get("checked_at", "")) >= str(best.get("checked_at", "")):
                best = d
    return best


def merge(doc: dict, lists: Dict[str, List[str]], djia_names: Optional[List[str]],
          errors: Dict[str, str], today: str) -> Tuple[dict, List[str]]:
    """Applies accepted lists to a copy of doc. Returns (new doc, messages)."""
    out = json.loads(json.dumps(doc))
    msgs: List[str] = []
    changes = out.setdefault("changes", [])
    for code in ORDER:
        ix = out["indexes"].get(code)
        if ix is None:
            continue
        if code in errors:
            msgs.append(f"warning|{code}: could not fetch ({errors[code]}); kept the previous list")
            continue
        if code == "DJIA":
            if djia_names is None:
                continue
            stored = ix.get("names") or []
            if len(djia_names) != 30:
                msgs.append(f"warning|DJIA: source lists {len(djia_names)} names, expected 30; not compared")
            elif stored and sorted(stored) != sorted(djia_names):
                gone = sorted(set(stored) - set(djia_names))
                new = sorted(set(djia_names) - set(stored))
                msgs.append(f"warning|DJIA membership looks changed on Wikipedia (out: {', '.join(gone)}; "
                            f"in: {', '.join(new)}). Update DJIA in universe/index_members.json by hand.")
            continue
        new = lists.get(code)
        if not new:
            continue
        old = ix.get("tickers") or []
        problem = check_list(code, new, old)
        if problem:
            msgs.append(f"warning|{code}: fetched list rejected - {problem}; kept the previous list")
            continue
        added, removed = sorted(set(new) - set(old)), sorted(set(old) - set(new))
        if added or removed:
            ix["tickers"] = sorted(new)
            ix["count"] = len(new)
            ix["as_of"] = today
            changes.append({"date": today, "index": code, "added": added, "removed": removed})
            msgs.append(f"notice|{code}: +{', '.join(added) or 'none'} / -{', '.join(removed) or 'none'}")
    out["changes"] = changes[-60:]
    out["checked_at"] = today
    return out, msgs


def refresh(in_paths: List[Path], out_path: Path, today: Optional[str] = None,
            fetcher=fetch_all) -> int:
    today = today or date.today().isoformat()
    doc = freshest(in_paths)
    if doc is None:
        note("warning", "No index membership file to start from; nothing refreshed.")
        return 0
    lists, names, errors = fetcher()
    if len(errors) >= len(ORDER):
        # Nothing reachable (network trouble on the runner). Leave the file
        # and its date alone so the next scan simply tries again.
        note("warning", "Index membership refresh: no source reachable this run; will retry next scan. "
             + "; ".join(f"{k}: {v}" for k, v in sorted(errors.items()))[:600])
        return 0
    new_doc, msgs = merge(doc, lists, names, errors, today)
    for m in msgs:
        kind, text = m.split("|", 1)
        note(kind, "Index membership - " + text)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(new_doc, indent=1), encoding="utf-8")
    os.replace(tmp, out_path)
    n = len({t for ix in new_doc["indexes"].values() for t in ix.get("tickers", [])})
    print(f"Index membership checked {today}: {n} unique tickers -> {out_path}")
    return 0


def age_days(doc: Optional[dict], today: date) -> float:
    try:
        return (today - datetime.fromisoformat(str(doc["checked_at"])[:10]).date()).days
    except Exception:
        return float("inf")


# --------------------------------------------------------------------------
# Self-test (offline)
# --------------------------------------------------------------------------

FIXTURE_SP = """<table class="wikitable sortable" id="constituents"><tbody><tr><th><a href="/wiki/Ticker_symbol">Symbol</a></th>
<th>Security</th><th>GICS Sector</th></tr>
<tr><td><a class="external text" href="https://www.nyse.com/quote/XNYS:MMM">MMM</a></td><td><a href="/wiki/3M">3M</a></td><td>Industrials</td></tr>
<tr><td><a href="x">BRK.B</a></td><td>Berkshire Hathaway</td><td>Financials</td></tr>
<tr><td><a href="x">ABT</a><sup>[1]</sup></td><td>Abbott</td><td>Health Care</td></tr></tbody></table>"""

FIXTURE_DJTA = """<table class="wikitable"><tbody><tr><th>Year</th><th>Close</th></tr><tr><td>2025</td><td>1</td></tr></tbody></table>
<table class="wikitable sortable" border="1"><caption>DJTA component companies</caption><tbody><tr>
<th scope="col">Company </th><th scope="col">Ticker </th><th scope="col">Sector </th></tr>
<tr><th scope="row"><a href="/wiki/Alaska_Air_Group">Alaska Air Group</a></th><td>ALK </td><td>Airline </td></tr>
<tr><th scope="row"><a href="/wiki/Avis_Budget_Group">Avis Budget Group</a></th><td>CAR </td><td>Rental</td></tr></tbody></table>"""

FIXTURE_DJIA = """{{ navbox
| list1  =
* [[3M]]
* [[Alphabet Inc.|Alphabet]]
* [[Honeywell|Honeywell Technologies]]
}}"""


def run_selftest() -> int:
    fails: List[str] = []

    def check(name, ok):
        print(("  [PASS] " if ok else "  [FAIL] ") + name)
        if not ok:
            fails.append(name)

    check("Constituents table: symbol column read, footnotes dropped, BRK.B -> BRK-B",
          normalize(tickers_from_tables(FIXTURE_SP)) == ["MMM", "BRK-B", "ABT"])
    check("Dow Transports: ticker column found even when company cells are <th>",
          tickers_from_tables(FIXTURE_DJTA) == ["ALK", "CAR"])
    check("DJIA navbox names use the display label",
          djia_names_from_wikitext(FIXTURE_DJIA) == ["3M", "Alphabet", "Honeywell Technologies"])

    base = [f"T{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(503)]
    check("A normal quarterly change is accepted", check_list("SP500", base[:-3] + ["NEWA", "NEWB", "NEWC"], base) is None)
    check("A truncated list is rejected", "members" in (check_list("SP500", base[:200], base) or ""))
    check("A list that swaps out too many names is rejected",
          "changed at once" in (check_list("SP500", base[:460] + [f"Z{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(43)], base) or ""))
    check("Malformed tickers are rejected", "malformed" in (check_list("DJUA", ["AEP"] * 14 + ["bad!"], []) or ""))

    doc = {"checked_at": "2026-11-01", "indexes": {
        "SP500": {"tickers": base, "as_of": "2026-11-01"},
        "NDX": {"tickers": base[:101], "as_of": "2026-11-01"},
        "DJIA": {"tickers": base[:30], "names": ["A"] * 29 + ["B"]},
        "DJUA": {"tickers": base[:15], "as_of": "2024-11-08"}}}
    lists = {"SP500": base[1:] + ["NEWA"], "DJUA": base[:200]}
    new, msgs = merge(doc, lists, ["A"] * 29 + ["C"], {"NDX": "timeout"}, "2026-11-08")
    check("Accepted change is applied, dated and logged",
          new["indexes"]["SP500"]["as_of"] == "2026-11-08" and "NEWA" in new["indexes"]["SP500"]["tickers"]
          and new["changes"][-1] == {"date": "2026-11-08", "index": "SP500", "added": ["NEWA"], "removed": [base[0]]})
    check("Rejected and unreachable sources keep the previous list",
          new["indexes"]["DJUA"]["tickers"] == base[:15] and new["indexes"]["NDX"]["tickers"] == base[:101])
    check("A changed Dow 30 raises a warning but is never auto-applied",
          any(m.startswith("warning|DJIA membership looks changed") for m in msgs)
          and new["indexes"]["DJIA"]["tickers"] == base[:30])
    check("checked_at moves forward even when nothing changed", new["checked_at"] == "2026-11-08")
    check("The original document is not modified", doc["indexes"]["SP500"]["tickers"] == base)

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        a, b, o = Path(td, "a.json"), Path(td, "b.json"), Path(td, "out.json")
        a.write_text(json.dumps(doc))
        b.write_text(json.dumps({**doc, "checked_at": "2026-10-01"}))
        rc = refresh([b, a], o, "2026-11-08", fetcher=lambda: ({}, None, {}))
        check("refresh() starts from the freshest input and always exits 0",
              rc == 0 and json.loads(o.read_text())["checked_at"] == "2026-11-08")
        o.unlink()
        all_down = {c: "offline" for c in ORDER}
        rc = refresh([a], o, "2026-11-09", fetcher=lambda: ({}, None, all_down))
        check("With every source unreachable nothing is written (retried next run)", rc == 0 and not o.exists())
    check("Age in days from checked_at", age_days({"checked_at": "2026-11-01"}, date(2026, 11, 8)) == 7
          and age_days(None, date(2026, 11, 8)) == float("inf"))
    repo = load(REPO_FILE)
    check("The committed universe/index_members.json passes every rule",
          repo is not None and all(check_list(c, repo["indexes"][c]["tickers"], []) is None for c in ORDER))

    print("\nALL SELF-TESTS PASSED" if not fails else f"\nSELF-TEST FAILED: {fails}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--in", dest="inputs", action="append", default=None,
                    help="membership file(s) to start from (the freshest wins); default: state copy + repo copy")
    ap.add_argument("--out", default=str(STATE_FILE))
    ap.add_argument("--if-older-than-days", type=float, default=0,
                    help="skip (exit 0) when the freshest input was checked more recently than this")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return run_selftest()
    inputs = [Path(p) for p in (args.inputs or [STATE_FILE, REPO_FILE])]
    if args.if_older_than_days:
        age = age_days(freshest(inputs), date.today())
        if age < args.if_older_than_days:
            print(f"Index membership was checked {age:.0f} day(s) ago; next check after "
                  f"{args.if_older_than_days:.0f} days.")
            return 0
    try:
        return refresh(inputs, Path(args.out))
    except Exception as e:  # never stop the scan over this
        note("warning", f"Index membership refresh skipped: {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
