#!/usr/bin/env python3
"""
tools/backfill_history.py
=========================
Rebuilds docs/data/history.json from the git history of docs/data/latest.json.

Every scheduled scan commits a fresh latest.json, so the repo already holds
every screen ever published. This walks those commits and keeps ONE snapshot
per trading session - the last post-close scan taken on that session's own
date when there is one, otherwise the latest scan that describes it - which
is the same rule the live scanner applies going forward.

Run from the repo root (needs full git history, not a shallow clone):

    python tools/backfill_history.py            # merge into existing history.json
    python tools/backfill_history.py --dry-run  # just print what it would keep

Sessions already present in history.json are left as they are (the live
scanner's record is richer). Forward returns are NOT computed here - they
need price history, so the next scanner run fills them in for every session.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sepa_scanner import (ET, HISTORY_HORIZONS, history_picks, is_post_close,  # noqa: E402
                          session_for_timestamp, write_json_atomic)

DATA = "docs/data/latest.json"
OUT = "docs/data/history.json"


def git(*args) -> str:
    return subprocess.check_output(["git", *args], text=True)


def snapshots():
    """(commit, payload) for every committed version of latest.json, oldest first."""
    shas = git("log", "--reverse", "--format=%H", "--", DATA).split()
    for sha in shas:
        try:
            yield sha, json.loads(git("show", f"{sha}:{DATA}"))
        except Exception as e:  # unparseable or missing in that commit
            print(f"  skip {sha[:7]}: {e}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    by_session: dict = {}
    names: dict = {}
    for sha, d in snapshots():
        gen = d.get("generated_at")
        if not gen or not (d.get("section1") or d.get("section2")):
            continue
        ts = datetime.fromisoformat(gen)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        session = (datetime.fromisoformat(d["session_date"]).date() if d.get("session_date")
                   else session_for_timestamp(ts))
        final = is_post_close(ts, session)
        same_day_final = final and ts.astimezone(ET).date() == session
        # Rank candidates: same-day post-close > any post-close > intraday; then latest.
        key = (same_day_final, final, ts)
        cur = by_session.get(session)
        if cur is None or key > cur[0]:
            by_session[session] = (key, sha, d, ts, final)
        for r in (d.get("section1") or []) + (d.get("section2") or []):
            if r.get("company"):
                names[r["ticker"]] = [r["company"], r.get("sector") or "", r.get("industry") or ""]

    days = []
    for session in sorted(by_session):
        _, sha, d, ts, final = by_session[session]
        picks = history_picks(d.get("section1"), d.get("section2"))
        # Scans before the Sept 29, 2026 fix (the ones without a session_date
        # field) compared today's price with itself, so their daily % change
        # was always ~0. Don't carry a known-wrong number into history.
        if not d.get("session_date"):
            for p in picks:
                p["c"] = None
        days.append({
            "date": session.isoformat(),
            "generated_at": ts.isoformat(),
            "final": final,
            "market": (d.get("market_status") or "").split(" (")[0] or None,
            "scored": d.get("total_scored"),
            "qualified": d.get("qualifying_count"),
            "picks": picks,
            "source": sha[:7],
        })
        print(f"{session}  {'final   ' if final else 'intraday'}  {ts.astimezone(ET):%a %m-%d %H:%M} ET  "
              f"{sha[:7]}  qualified={d.get('qualifying_count')}")

    history = {}
    if os.path.exists(args.out):
        with open(args.out) as f:
            history = json.load(f)
    existing = {d["date"]: d for d in history.get("days", [])}
    merged = {**{d["date"]: d for d in days}, **existing}  # live records win
    history["version"] = 1
    history["horizons"] = list(HISTORY_HORIZONS)
    history["days"] = [merged[k] for k in sorted(merged)]
    history["names"] = {**names, **history.get("names", {})}
    history.setdefault("updated_at", datetime.now(ET).isoformat())
    history.setdefault("as_of", history["days"][-1]["date"] if history["days"] else None)

    print(f"\n{len(days)} sessions from git, {len(existing)} already in {args.out}, "
          f"{len(history['days'])} total, {len(history['names'])} named tickers")
    if args.dry_run:
        return
    write_json_atomic(args.out, history)
    print(f"Wrote {args.out} ({os.path.getsize(args.out):,} bytes)")


if __name__ == "__main__":
    main()
