#!/usr/bin/env python3
"""
sepa_scanner.py
================
ONE-FILE, MANUAL-RUN scanner over the S&P 500 + S&P 400. Every run scores
the whole universe and always returns 20 ideas total, split into two
fixed-size top-10 sections - even on a quiet day where nothing fully
qualifies (each row is flagged Qualifies: Yes/No so you know which):

  SECTION 1 - Today's Momentum Leaders (top 10)
  SECTION 2 - Fundamentals & Trend-Quality Picks (next-best 10, no overlap)

Both sections are ranked PRIMARILY by how many of Minervini's 8 Trend
Template criteria a stock passes (Trade Like a Stock Market Wizard) -
that's the dominant sort key in both sections, not just one weighted
input among several. Within the same Trend Template count, Section 1
breaks ties with CANSLIM + Zanger-style breakout/volume triggers +
Qullamaggie-style momentum triggers using live intraday data; Section 2
breaks ties with CANSLIM's fundamental factors + traditional value
metrics (PEG, PE, margin, debt/equity), deliberately excluding the
intraday momentum triggers since it's the value-leaning cut.

DISCLAIMER: educational tool, NOT financial advice, places no trades.
Data is free Yahoo Finance (`yfinance`), typically delayed ~15-20 min.
Every "strategy" here is a simplified public-domain approximation, not
endorsed by Minervini, O'Neil, Zanger, or Qullamaggie.

SETUP
-----
    pip install yfinance pandas numpy requests lxml tabulate
    python sepa_scanner.py                 # one manual run, prints + saves CSV
    python sepa_scanner.py --selftest      # built-in logic tests, no network

This is a single file, one manual run per execution (no background
looping) - a full run takes a few minutes; a same-day re-run is faster
since fundamentals are cached to a local JSON file.

OUTPUTS (docs/data/, read by the website)
  latest.json     the two ranked lists with full per-stock detail, plus
                  market breadth and sector/industry leadership
  history.json    every session's final list + forward returns vs SPY
  universe.json   every scored stock (compact), for lookup + watchlist
  companies.json  company profiles (rewritten only when they change)
  stocks.json     full detail + 6-month closes for every scored stock; read
                  by tools/build_site.py to generate the per-stock pages
                  (build input only - not loaded by visitors' browsers)

UNIVERSE: S&P 500 + S&P 400 MidCap (hardcoded, ~903 tickers)
----------------------------------------------------------------
  Hardcoded snapshot (SP500_TICKERS / SP400_TICKERS below), not scraped
  live, so it can't silently degrade to an empty result if Wikipedia's
  page structure changes. To refresh later: ask an LLM with web access
  to re-pull the Symbol columns from
  https://en.wikipedia.org/wiki/List_of_S%26P_500_companies and
  .../List_of_S%26P_400_companies (converting '.' to '-', e.g.
  BRK.B -> BRK-B) and replace the two lists.
  Use --sp500-only for just the S&P 500. Add one-off names anytime with
  --extra-tickers TICKER1,TICKER2,...

LIMITATIONS
-----------
  - Data is delayed ~15-20 min, not true real time.
  - RS Rating approximates IBD's proprietary formula, ranked only within
    the scanned universe.
  - CANSLIM's C/A/S/I sub-scores depend on yfinance's `.info` dict, which
    is inconsistently populated; missing fields are skipped, not counted
    against a stock.
  - No earnings-calendar awareness - "episodic pivot" flags any big gap
    + volume spike, not specifically post-earnings gaps.
  - No market-holiday calendar for the informational market-hours note.
  - This tool only identifies candidates - it does not size positions,
    set stops, or manage risk. That discipline lives entirely with you.
"""

from __future__ import annotations


import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("Missing dependency 'yfinance'. Run: pip install yfinance pandas numpy requests lxml tabulate")
    sys.exit(1)

try:
    from tabulate import tabulate
    HAVE_TABULATE = True
except ImportError:
    HAVE_TABULATE = False

ET = ZoneInfo("America/New_York")
LOG = logging.getLogger("sepa_scanner")

# NYSE full-day closures. Used only to work out which trading session an
# old snapshot belongs to (history backfill) and for the market-hours note -
# live scans take their session date from the intraday data itself, so a
# missing future year here never mislabels a live scan. Extend yearly from
# https://www.nyse.com/markets/hours-calendars
NYSE_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def is_trading_day(d) -> bool:
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def session_for_timestamp(ts: datetime):
    """The trading session a scan taken at `ts` describes: today's session
    once the market has opened on a trading day, otherwise the most recent
    earlier trading day (pre-open, weekend, holiday)."""
    ts = ts.astimezone(ET)
    d = ts.date()
    opened = (ts.hour, ts.minute) >= (9, 30)
    if is_trading_day(d) and opened:
        return d
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def is_post_close(ts: datetime, session_date) -> bool:
    """True when a scan taken at `ts` saw the session's closing prices."""
    ts = ts.astimezone(ET)
    if ts.date() > session_date:
        return True
    return ts.date() == session_date and (ts.hour, ts.minute) >= (16, 0)


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # yfinance logs its own noisy multi-line "possibly delisted" / HTTP 404
    # dumps straight to the console for every failed ticker in a batch.
    # We already catch and handle those failures ourselves (skip the
    # ticker, keep going), so silence yfinance's own logger to keep the
    # console readable - a clean one-line summary is printed instead.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass
class Config:
    benchmark: str = "SPY"
    lookback_period: str = "15mo"
    extra_tickers: List[str] = field(default_factory=list)
    include_sp400: bool = True

    min_price: float = 5.0
    min_avg_dollar_volume: float = 5_000_000

    strict_trend_template: bool = False
    min_trend_criteria: int = 6          # out of 8; --strict-trend forces 8

    fundamentals_ttl_hours: float = 6.0
    fundamentals_cache_path: str = "fundamentals_cache.json"

    batch_size: int = 100
    # 8, not 16: fundamentals are the heaviest Yahoo call (one request per
    # ticker). Fewer parallel workers makes rate-limiting on shared cloud
    # IPs much less likely, and with the cross-run cache in the workflow
    # most runs refresh very few tickers anyway.
    max_workers_fundamentals: int = 8

    # A yf.download batch where fewer than this share of tickers came back
    # with data is treated as rate-limited/failed and retried.
    batch_min_success: float = 0.5
    batch_attempts: int = 3
    retry_backoff_seconds: float = 20.0

    # Publish gate: refuse to overwrite the site's data unless at least this
    # share of the universe was actually scored this run (see main()).
    min_coverage: float = 0.6

    top_n_section: int = 10
    output_dir: str = "scan_logs"

    # ---- Section 1: momentum composite weights ----
    # Minervini's Trend Template dominates the ranking by design: both
    # sections sort primarily by Trend Template pass-count first (see
    # run_scan), and these weights are the tiebreaker among ties on that
    # count. Trend carries roughly double the next-largest factor.
    w1_trend: float = 60.0
    w1_canslim: float = 30.0
    w1_zanger: float = 18.0
    w1_qmaggie: float = 18.0
    w1_value: float = 6.0

    # ---- Section 2: value / trend-quality composite weights ----
    # Deliberately no zanger/qmaggie weight - this section excludes
    # intraday momentum triggers by design, and leans on the Trend
    # Template even harder than Section 1 does.
    w2_trend: float = 70.0
    w2_canslim: float = 18.0
    w2_value: float = 12.0


# --------------------------------------------------------------------------
# Universe: S&P 500 constituents (+ optional extras)
# --------------------------------------------------------------------------

# S&P 500 constituents as of 2026-08-17 (503 tickers, verified against the
# official "contains 503 stocks" count - no duplicates, no overlap with
# SP400_TICKERS below). Source: en.wikipedia.org/wiki/List_of_S%26P_500_companies
SP500_TICKERS = [
    "MMM", "AOS", "ABT", "ABBV", "ACN", "ADBE", "AMD", "AES", "AFL", "A",
    "APD", "ABNB", "AKAM", "ALB", "ARE", "ALGN", "ALLE", "LNT", "ALL", "GOOGL",
    "GOOG", "MO", "AMZN", "AMCR", "AEE", "AEP", "AXP", "AIG", "AMT", "AWK",
    "AMP", "AME", "AMGN", "APH", "ADI", "AON", "APA", "APO", "AAPL", "AMAT",
    "APP", "APTV", "ACGL", "ADM", "ARES", "ANET", "AJG", "AIZ", "T", "ATO",
    "ADSK", "ADP", "AZO", "AVB", "AVY", "AXON", "BKR", "BALL", "BAC", "BAX",
    "BDX", "BRK-B", "BBY", "TECH", "BIIB", "BLK", "BX", "XYZ", "BNY", "BA",
    "BKNG", "BSX", "BMY", "AVGO", "BR", "BRO", "BF-B", "BLDR", "BG", "BXP",
    "CHRW", "CDNS", "CPT", "CPB", "COF", "CAH", "CCL", "CARR", "CVNA", "CASY",
    "CAT", "CBOE", "CBRE", "CDW", "COR", "CNC", "CNP", "CF", "CRL", "SCHW",
    "CHTR", "CVX", "CMG", "CB", "CHD", "CIEN", "CI", "CINF", "CTAS", "CSCO",
    "C", "CFG", "CLX", "CME", "CMS", "KO", "CTSH", "COHR", "COIN", "CL",
    "CMCSA", "FIX", "CAG", "COP", "ED", "STZ", "CEG", "COO", "CPRT", "GLW",
    "CPAY", "CTVA", "CSGP", "COST", "CRH", "CRWD", "CCI", "CSX", "CMI", "CVS",
    "DHR", "DRI", "DDOG", "DVA", "DECK", "DE", "DELL", "DAL", "DVN", "DXCM",
    "FANG", "DLR", "DG", "DLTR", "D", "DPZ", "DASH", "DOV", "DOW", "DHI",
    "DTE", "DUK", "DD", "ETN", "EBAY", "SATS", "ECL", "EIX", "EW", "EA",
    "ELV", "EME", "EMR", "ETR", "EOG", "EPAM", "EQT", "EFX", "EQIX", "EQR",
    "ERIE", "ESS", "EL", "EG", "EVRG", "ES", "EXC", "EXE", "EXPE", "EXPD",
    "EXR", "XOM", "FFIV", "FDS", "FICO", "FAST", "FRT", "FDX", "FIS", "FITB",
    "FSLR", "FE", "FISV", "F", "FTNT", "FTV", "FOXA", "FOX", "BEN", "FCX",
    "GRMN", "IT", "GE", "GEHC", "GEV", "GEN", "GNRC", "GD", "GIS", "GM",
    "GPC", "GILD", "GPN", "GL", "GDDY", "GS", "HAL", "HIG", "HAS", "HCA",
    "DOC", "HSIC", "HSY", "HPE", "HLT", "HD", "HON", "HRL", "HST", "HWM",
    "HPQ", "HUBB", "HUM", "HBAN", "HII", "IBM", "IEX", "IDXX", "ITW", "INCY",
    "IR", "PODD", "INTC", "IBKR", "ICE", "IFF", "IP", "INTU", "ISRG", "IVZ",
    "INVH", "IQV", "IRM", "JBHT", "JBL", "JKHY", "J", "JNJ", "JCI", "JPM",
    "KVUE", "KDP", "KEY", "KEYS", "KMB", "KIM", "KMI", "KKR", "KLAC", "KHC",
    "KR", "LHX", "LH", "LRCX", "LVS", "LDOS", "LEN", "LII", "LLY", "LIN",
    "LYV", "LMT", "L", "LOW", "LULU", "LITE", "LYB", "MTB", "MPC", "MAR",
    "MRSH", "MLM", "MAS", "MA", "MKC", "MCD", "MCK", "MDT", "MRK", "META",
    "MET", "MTD", "MGM", "MCHP", "MU", "MSFT", "MAA", "MRNA", "TAP", "MDLZ",
    "MPWR", "MNST", "MCO", "MS", "MOS", "MSI", "MSCI", "NDAQ", "NTAP", "NFLX",
    "NEM", "NWSA", "NWS", "NEE", "NKE", "NI", "NDSN", "NSC", "NTRS", "NOC",
    "NCLH", "NRG", "NUE", "NVDA", "NVR", "NXPI", "ORLY", "OXY", "ODFL", "OMC",
    "ON", "OKE", "ORCL", "OTIS", "PCAR", "PKG", "PLTR", "PANW", "PSKY", "PH",
    "PAYX", "PYPL", "PNR", "PEP", "PFE", "PCG", "PM", "PSX", "PNW", "PNC",
    "POOL", "PPG", "PPL", "PFG", "PG", "PGR", "PLD", "PRU", "PEG", "PTC",
    "PSA", "PHM", "PWR", "QCOM", "DGX", "Q", "RL", "RJF", "RTX", "O",
    "REG", "REGN", "RF", "RSG", "RMD", "RVTY", "HOOD", "ROK", "ROL", "ROP",
    "ROST", "RCL", "SPGI", "CRM", "SNDK", "SBAC", "SLB", "STX", "SRE", "NOW",
    "SHW", "SPG", "SWKS", "SJM", "SW", "SNA", "SOLV", "SO", "LUV", "SWK",
    "SBUX", "STT", "STLD", "STE", "SYK", "SMCI", "SYF", "SNPS", "SYY", "TMUS",
    "TROW", "TTWO", "TPR", "TRGP", "TGT", "TEL", "TDY", "TER", "TSLA", "TXN",
    "TPL", "TXT", "TMO", "TJX", "TKO", "TTD", "TSCO", "TT", "TDG", "TRV",
    "TRMB", "TFC", "TYL", "TSN", "USB", "UBER", "UDR", "ULTA", "UNP", "UAL",
    "UPS", "URI", "UNH", "UHS", "VLO", "VEEV", "VTR", "VLTO", "VRSN", "VRSK",
    "VZ", "VRTX", "VRT", "VTRS", "VICI", "V", "VST", "VMC", "WRB", "GWW",
    "WAB", "WMT", "DIS", "WBD", "WM", "WAT", "WEC", "WFC", "WELL", "WST",
    "WDC", "WY", "WSM", "WMB", "WTW", "WDAY", "WYNN", "XEL", "XYL", "YUM",
    "ZBRA", "ZBH", "ZTS",
]

# S&P 400 MidCap constituents as of 2026-08-17 (400 tickers, verified no
# duplicates and zero overlap with SP500_TICKERS above). Source:
# en.wikipedia.org/wiki/List_of_S%26P_400_companies
SP400_TICKERS = [
    "AA", "AAL", "AAON", "ACI", "ACM", "ADC", "AEIS", "AFG", "AGCO", "AHR",
    "AIT", "ALGM", "ALK", "ALLY", "ALV", "AM", "AMG", "AMH", "AMKR", "AN",
    "ANF", "APG", "APPF", "AR", "ARMK", "ARW", "ARWR", "ASB", "ASH", "ATI",
    "ATR", "AVAV", "AVNT", "AVT", "AVTR", "AXTA", "AYI", "BAH", "BBWI", "BC",
    "BCO", "BDC", "BHF", "BILL", "BIO", "BJ", "BKH", "BLD", "BLKB", "BMRN",
    "BRBR", "BRKR", "BROS", "BRX", "BSY", "BURL", "BWA", "BWXT", "BYD", "CACI",
    "CAR", "CART", "CAVA", "CBSH", "CBT", "CCK", "CDP", "CELH", "CFR", "CG",
    "CGNX", "CHDN", "CHE", "CHH", "CHRD", "CHWY", "CLF", "CLH", "CMC", "CNH",
    "CNM", "CNO", "CNX", "CNXC", "COKE", "COLB", "COLM", "COTY", "CPRI", "CR",
    "CRBG", "CROX", "CRS", "CRUS", "CSL", "CTRE", "CUBE", "CUZ", "CVLT", "CW",
    "CXT", "CYTK", "DAR", "DBX", "DCI", "DINO", "DKS", "DLB", "DOCN", "DOCS",
    "DOCU", "DT", "DTM", "DUOL", "DY", "EEFT", "EGP", "EHC", "ELAN", "ELF",
    "ELS", "ENS", "ENSG", "ENTG", "EPR", "EQH", "ESAB", "ESNT", "EVR", "EWBC",
    "EXEL", "EXLS", "EXP", "EXPO", "FAF", "FBIN", "FCFS", "FCN", "FFIN", "FHI",
    "FHN", "FIVE", "FLEX", "FLG", "FLO", "FLR", "FLS", "FN", "FNB", "FND",
    "FNF", "FOUR", "FR", "FTI", "G", "GAP", "GATX", "GBCI", "GEF", "GGG",
    "GHC", "GLPI", "GME", "GMED", "GNTX", "GPK", "GT", "GTLS", "GWRE", "GXO",
    "H", "HAE", "HALO", "HGV", "HIMS", "HL", "HLI", "HLNE", "HOG", "HOMB",
    "HQY", "HR", "HRB", "HWC", "HXL", "IBOC", "IDA", "IDCC", "ILMN", "INGR",
    "IPGP", "IRT", "ITT", "JAZZ", "JEF", "JHG", "JLL", "KBH", "KBR", "KD",
    "KEX", "KNF", "KNSL", "KNX", "KRC", "KRG", "KTOS", "LAD", "LAMR", "LEA",
    "LECO", "LFUS", "LIVN", "LNTH", "LOPE", "LPX", "LSCC", "LSTR", "M", "MANH",
    "MAT", "MEDP", "MIDD", "MKSI", "MLI", "MMS", "MOG-A", "MORN", "MP",
    "MSA", "MSM", "MTDR", "MTG", "MTN", "MTSI", "MTZ", "MUR", "MUSA", "MZTI",
    "NBIX", "NEU", "NFG", "NJR", "NLY", "NNN", "NOV", "NOVT", "NSA", "NTNX",
    "NVST", "NVT", "NWE", "NXST", "NXT", "NYT", "OC", "OGE", "OGS", "OHI",
    "OKTA", "OLED", "OLLI", "OLN", "ONB", "ONTO", "OPCH", "ORA", "ORI", "OSK",
    "OVV", "OZK", "PAG", "PATH", "PB", "PBF", "PCTY", "PEGA", "PEN", "PFGC",
    "PII", "PINS", "PK", "PLNT", "PNFP", "POR", "POST", "PPC", "PR", "PRI",
    "PSN", "PVH", "QLYS", "R", "RBA", "RBC", "REXR", "RGA", "RGEN",
    "RGLD", "RH", "RLI", "RMBS", "RNR", "ROIV", "RPM", "RRC", "RRX", "RS",
    "RYAN", "RYN", "SAIA", "SAIC", "SAM", "SARO", "SBRA", "SCI", "SEIC", "SF",
    "SFM", "SGI", "SHC", "SIGI", "SIRI", "SITM", "SLAB", "SLGN", "SLM", "SMG", "SNX",
    "SOLS", "SON", "SPXC", "SR", "SSB", "SSD", "ST", "STAG", "STRL", "STWD",
    "SWX", "SYNA", "TCBI", "TEX", "THC", "THG", "THO", "TKR", "TLN", "TMHC",
    "TNL", "TOL", "TREX", "TRU", "TTC", "TTEK", "TTMI", "TWLO", "TXNM", "TXRH",
    "UBSI", "UFPI", "UGI", "ULS", "UMBF", "UNM", "USFD", "UTHR", "VAL", "VC",
    "VFC", "VICR", "VLY", "VMI", "VNO", "VNOM", "VNT", "VOYA", "VVV", "WAL",
    "WBS", "WCC", "WEX", "WFRD", "WH", "WHR", "WING", "WLK", "WMG", "WMS",
    "WPC", "WSO", "WTFC", "WTRG", "WTS", "WWD", "XPO", "XRAY", "YETI", "ZION",
]


def get_universe(cfg: Config) -> List[str]:
    """Builds the scan universe from the hardcoded SP500_TICKERS /
    SP400_TICKERS lists above (no network call, no parsing to fail)."""
    base = list(SP500_TICKERS)
    if cfg.include_sp400:
        base = base + list(SP400_TICKERS)
    extras = [t.strip().upper() for t in cfg.extra_tickers if t.strip()]
    universe = list(dict.fromkeys(base + extras))  # de-dupe, keep order
    LOG.info(f"Universe: {len(base)} index constituents"
              + (f" + {len(extras)} extra ticker(s) {extras}" if extras else ""))
    return universe


# --------------------------------------------------------------------------
# Daily history + indicators (used for the whole universe every run)
# --------------------------------------------------------------------------

def _frame_for(data: Optional[pd.DataFrame], ticker: str) -> Optional[pd.DataFrame]:
    """Pulls one ticker's OHLCV frame out of a yf.download result.

    With group_by="ticker", yfinance returns (ticker, field) MultiIndex
    columns - and current versions do that even for a single-ticker call -
    so always look the ticker up by name instead of assuming a flat frame
    when the batch happens to contain one symbol."""
    if data is None or data.empty:
        return None
    try:
        if isinstance(data.columns, pd.MultiIndex):
            if ticker not in data.columns.get_level_values(0):
                return None
            df = data[ticker]
        else:
            df = data
        df = df.dropna(how="all")
        return df if not df.empty else None
    except Exception:
        return None


def _download_batch(chunk: List[str], cfg: Config, *, period: str, interval: str,
                    what: str, offset: int) -> Optional[pd.DataFrame]:
    """yf.download with retry.

    When Yahoo rate-limits a shared cloud IP, yfinance usually does NOT
    raise: it returns a frame of empty columns for the affected tickers and
    reports the problem through its own logger (which setup_logging
    silences). So a call that "succeeded" can still be mostly empty. A
    batch where most tickers came back empty is treated as a failure and
    retried after a pause, instead of quietly scoring a fraction of the
    universe."""
    need = max(1, int(len(chunk) * cfg.batch_min_success))
    best: Optional[pd.DataFrame] = None
    best_ok = -1
    for attempt in range(1, cfg.batch_attempts + 1):
        data = None
        try:
            data = yf.download(
                tickers=chunk, period=period, interval=interval,
                group_by="ticker", threads=True, progress=False, auto_adjust=False,
            )
        except Exception as e:
            LOG.warning(f"{what} batch at offset {offset} raised on attempt {attempt}: {e}")

        ok = sum(1 for t in chunk if _frame_for(data, t) is not None) if data is not None else 0
        if ok > best_ok:
            best, best_ok = data, ok
        if ok >= need:
            return data

        if attempt < cfg.batch_attempts:
            wait = cfg.retry_backoff_seconds * attempt
            LOG.warning(f"{what} batch at offset {offset}: only {ok}/{len(chunk)} tickers returned data "
                        f"(attempt {attempt}/{cfg.batch_attempts}) - likely rate-limited, retrying in {wait:.0f}s")
            time.sleep(wait)

    LOG.warning(f"{what} batch at offset {offset}: giving up with {max(best_ok, 0)}/{len(chunk)} tickers")
    return best


def _batched_daily_download(tickers: List[str], cfg: Config) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    all_failed: List[str] = []
    dead_batches = 0
    for i in range(0, len(tickers), cfg.batch_size):
        chunk = tickers[i:i + cfg.batch_size]
        LOG.info(f"Daily bars: {i + 1}-{i + len(chunk)} / {len(tickers)}")
        data = _download_batch(chunk, cfg, period=cfg.lookback_period, interval="1d",
                               what="Daily", offset=i)
        got = 0
        for t in chunk:
            df = _frame_for(data, t)
            if df is not None and len(df) > 60:
                out[t] = df
                got += 1
            else:
                all_failed.append(t)
        # Circuit breaker: if Yahoo returned nothing at all for two batches in
        # a row even after retries, it is blocking this runner. Stop instead
        # of spending ten more minutes on retries that will also fail.
        dead_batches = dead_batches + 1 if got == 0 else 0
        if dead_batches >= 2 and i + cfg.batch_size < len(tickers):
            LOG.error("Yahoo returned no daily data for two batches in a row after retries - stopping early.")
            all_failed.extend(tickers[i + cfg.batch_size:])
            break

    if all_failed:
        LOG.info(f"{len(all_failed)} ticker(s) had no usable data (delisted/renamed/no history): "
                  f"{', '.join(all_failed)}")
    return out


def fetch_daily_history(universe: List[str], cfg: Config) -> Dict[str, pd.DataFrame]:
    all_tickers = list(dict.fromkeys(universe + [cfg.benchmark]))
    return _batched_daily_download(all_tickers, cfg)


def compute_indicators(df: pd.DataFrame) -> Optional[dict]:
    if df is None or len(df) < 210:
        return None

    # IMPORTANT: clean Open/High/Low/Close/Volume together as ONE frame,
    # not as four independently-.dropna()'d Series. Independent dropna()
    # lets Close and Volume end up with slightly different date indices
    # whenever either has an isolated one-day gap (common across a
    # 900-ticker batch download). Multiplying misaligned Series (e.g.
    # close * vol for dollar volume) then unions the indices and injects
    # NaN at every date one Series has but the other doesn't - and a
    # single NaN inside a rolling(20) window makes that whole window NaN,
    # which silently fails "NaN >= min_dollar_volume" for the WHOLE
    # ticker. This was previously excluding almost every stock in the
    # universe from the liquidity filter. Cleaning once, together, keeps
    # every remaining row fully aligned across all five columns.
    needed = ["Open", "High", "Low", "Close", "Volume"]
    if not all(c in df.columns for c in needed):
        return None
    clean = df[needed].dropna(how="any")
    if len(clean) < 210:
        return None

    close = clean["Close"]
    high = clean["High"]
    low = clean["Low"]
    vol = clean["Volume"]

    last_close = float(close.iloc[-1])
    sma50 = close.rolling(50).mean()
    sma150 = close.rolling(150).mean()
    sma200 = close.rolling(200).mean()
    sma200_now = float(sma200.iloc[-1])
    sma200_1mo_ago = float(sma200.iloc[-22]) if len(sma200.dropna()) > 22 else np.nan

    wk52_high = float(close.iloc[-252:].max()) if len(close) >= 252 else float(close.max())
    wk52_low = float(close.iloc[-252:].min()) if len(close) >= 252 else float(close.min())

    daily_range_pct = (high - low) / close * 100.0
    adr20 = float(daily_range_pct.rolling(20).mean().iloc[-1])

    avg_vol50 = float(vol.rolling(50).mean().iloc[-1])
    avg_dollar_vol20 = float((close * vol).rolling(20).mean().iloc[-1])

    def ret(n):
        if len(close) <= n:
            return np.nan
        return float(close.iloc[-1] / close.iloc[-1 - n] - 1.0)

    ret_3m, ret_6m, ret_9m, ret_12m = ret(63), ret(126), ret(189), ret(252)

    range10 = float((high.iloc[-10:].max() - low.iloc[-10:].min()) / last_close)
    range30 = float((high.iloc[-30:].max() - low.iloc[-30:].min()) / last_close)
    tightness_ratio = range10 / range30 if range30 > 0 else np.nan

    prior_20d_high = float(high.iloc[-21:-1].max())
    prior_50d_high = float(high.iloc[-51:-1].max())

    # During (and after) a trading day, Yahoo's daily history already
    # contains TODAY's bar. The intraday step therefore cannot use
    # last_close as "yesterday's close" - doing so compared today's price
    # with itself and pinned every %Chg at ~0.00%. Keep the last few dated
    # closes so _parse_intraday_frame can pick the true prior-session close,
    # and a 50-day volume average that excludes the newest bar for the same
    # reason (today's partial volume should not be in its own baseline).
    recent_bars = [(pd.Timestamp(ts).date().isoformat(), float(c))
                   for ts, c in close.iloc[-3:].items()]
    avg_vol50_ex_last = float(vol.iloc[:-1].rolling(50).mean().iloc[-1])

    return {
        "last_close": last_close, "sma50": float(sma50.iloc[-1]),
        "sma150": float(sma150.iloc[-1]), "sma200": sma200_now,
        "sma200_1mo_ago": sma200_1mo_ago, "wk52_high": wk52_high, "wk52_low": wk52_low,
        "adr20_pct": adr20, "avg_vol50": avg_vol50, "avg_dollar_vol20": avg_dollar_vol20,
        "ret_3m": ret_3m, "ret_6m": ret_6m, "ret_9m": ret_9m, "ret_12m": ret_12m,
        "tightness_ratio": tightness_ratio,
        "prior_20d_high": prior_20d_high, "prior_50d_high": prior_50d_high,
        "recent_bars": recent_bars, "avg_vol50_ex_last": avg_vol50_ex_last,
    }


def compute_rs_ratings(indicators: Dict[str, dict]) -> Dict[str, int]:
    rows = []
    for t, ind in indicators.items():
        parts = [ind.get("ret_3m"), ind.get("ret_6m"), ind.get("ret_9m"), ind.get("ret_12m")]
        if any(p is None or (isinstance(p, float) and np.isnan(p)) for p in parts):
            continue
        weighted = 0.4 * parts[0] + 0.2 * parts[1] + 0.2 * parts[2] + 0.2 * parts[3]
        rows.append((t, weighted))
    if not rows:
        return {}
    s = pd.Series({t: w for t, w in rows})
    pct = s.rank(pct=True) * 98 + 1
    return {t: int(round(v)) for t, v in pct.items()}


def market_health(spy_ind: Optional[dict]) -> str:
    if not spy_ind:
        return "UNKNOWN (no SPY data)"
    price, sma50, sma200 = spy_ind["last_close"], spy_ind["sma50"], spy_ind["sma200"]
    if price > sma50 > sma200:
        return "CONFIRMED UPTREND (SPY > 50MA > 200MA)"
    if price > sma200:
        return "MIXED (SPY above 200MA but below/near 50MA - be selective)"
    return "UNDER PRESSURE (SPY below 200MA - O'Neil would say raise cash / reduce new buys)"


# --------------------------------------------------------------------------
# Intraday ("up to the minute") data - pulled for the WHOLE candidate pool
# --------------------------------------------------------------------------

def _elapsed_session_fraction(session_date=None, now_et: Optional[datetime] = None) -> float:
    """Share of the regular session (9:30-16:00 ET) that has elapsed for the
    session the intraday data belongs to. If that session is an EARLIER day
    (a pre-open run, a weekend, a holiday) it is complete: 1.0. The old
    version always measured from today's 9:30, so a pre-open run divided a
    full day of volume by one minute's worth and reported RelVol ~390x."""
    now_et = now_et or datetime.now(ET)
    if session_date is not None and session_date < now_et.date():
        return 1.0
    session_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed_min = max((now_et - session_open).total_seconds() / 60.0, 1.0)
    return min(elapsed_min / 390.0, 1.0)


def _session_date(df: pd.DataFrame):
    """Trading date (ET) of the last bar in an intraday frame."""
    ts = pd.Timestamp(df.index[-1])
    if ts.tzinfo is not None:
        ts = ts.tz_convert(ET)
    return ts.date()


def _parse_intraday_frame(df: pd.DataFrame, ind: dict,
                          now_et: Optional[datetime] = None) -> Optional[dict]:
    if df is None or df.empty:
        return None
    needed = ["Open", "High", "Close", "Volume"]
    if not all(c in df.columns for c in needed):
        return None
    df = df.dropna(subset=["Close"])
    if df.empty:
        return None

    day_open = float(df["Open"].dropna().iloc[0]) if df["Open"].notna().any() else float(df["Close"].iloc[0])
    day_high = float(df["High"].max())
    last_price = float(df["Close"].iloc[-1])
    cum_volume = float(df["Volume"].fillna(0).sum())

    # Pick the close of the session BEFORE the one this intraday data is
    # from. If the daily history already includes this session's bar (it
    # does whenever the scan runs during or after the trading day), that
    # is the second-to-last daily bar, not the last one.
    session_date = _session_date(df)
    bars = ind.get("recent_bars") or []
    if len(bars) >= 2 and bars[-1][0] == session_date.isoformat():
        prev_close = bars[-2][1]
        avg_vol50 = ind.get("avg_vol50_ex_last") or ind.get("avg_vol50")
    else:
        prev_close = ind.get("last_close")
        avg_vol50 = ind.get("avg_vol50")

    elapsed_frac = _elapsed_session_fraction(session_date, now_et)

    pct_change = ((last_price / prev_close) - 1.0) * 100.0 if prev_close else None
    gap_pct = ((day_open / prev_close) - 1.0) * 100.0 if prev_close else None
    expected_vol_by_now = avg_vol50 * elapsed_frac if avg_vol50 else None
    rel_volume = (cum_volume / expected_vol_by_now) if expected_vol_by_now else None

    return {
        "last_price": last_price, "day_open": day_open, "day_high": day_high,
        "cum_volume": cum_volume, "pct_change": pct_change, "gap_pct": gap_pct,
        "rel_volume": rel_volume, "prev_close": prev_close,
        "session_date": session_date.isoformat(),
    }


def fetch_intraday(tickers: List[str], daily_ind: Dict[str, dict], cfg: Config) -> Dict[str, dict]:
    """Batched, 'up to the minute' (per Yahoo's delay) intraday pull for the
    full candidate list - not just a pre-filtered subset."""
    out: Dict[str, dict] = {}
    if not tickers:
        return out
    now_et = datetime.now(ET)
    failed: List[str] = []

    for i in range(0, len(tickers), cfg.batch_size):
        chunk = tickers[i:i + cfg.batch_size]
        LOG.info(f"Intraday quotes: {i + 1}-{i + len(chunk)} / {len(tickers)}")
        data = _download_batch(chunk, cfg, period="1d", interval="1m",
                               what="Intraday", offset=i)

        for t in chunk:
            try:
                ind = daily_ind.get(t)
                if ind is None:
                    continue
                parsed = _parse_intraday_frame(_frame_for(data, t), ind, now_et)
                if parsed:
                    out[t] = parsed
                else:
                    failed.append(t)
            except Exception:
                failed.append(t)

    if failed:
        LOG.info(f"{len(failed)} ticker(s) had no usable intraday data this run: {', '.join(failed)}")
    return out


# --------------------------------------------------------------------------
# Fundamentals - threaded pull + on-disk JSON cache (persists across runs
# the same day so re-running the script isn't a full 500-ticker re-pull)
# --------------------------------------------------------------------------

# Only the fields the scorers and the dashboard actually read. Yahoo's
# .info payload has ~150 keys per ticker; keeping all of them made the cache
# file several MB, which matters now that the workflow carries it between
# runs with actions/cache.
FUND_FIELDS = (
    "longName", "shortName", "sector", "industry",
    "earningsQuarterlyGrowth", "earningsGrowth", "heldPercentInstitutions",
    "trailingPE", "pegRatio", "trailingPegRatio", "profitMargins", "debtToEquity",
    # Company profile + context for the drill-down. All of these arrive in
    # the SAME get_info() response the scanner already requests, so adding
    # them costs no extra Yahoo calls.
    "longBusinessSummary", "website", "fullTimeEmployees", "marketCap",
    "city", "state", "country",
    "earningsTimestamp", "earningsTimestampStart", "earningsTimestampEnd",
    "isEarningsDateEstimate",
    "forwardPE", "revenueGrowth", "grossMargins", "operatingMargins", "returnOnEquity",
    "recommendationKey", "recommendationMean", "numberOfAnalystOpinions", "targetMeanPrice",
    "shortPercentOfFloat", "beta",
)

# Bump whenever FUND_FIELDS changes. Cache entries written under an older
# schema are treated as expired (but kept as a fallback if the refresh
# fails), so new fields appear on the next run instead of up to a day later.
FUND_SCHEMA = 2

# Business summaries can run to several thousand characters; the site shows
# a short paragraph, so the cache keeps only the opening portion.
SUMMARY_CACHE_CHARS = 1500


def _peg(fund: dict):
    """Yahoo has published PEG under both keys over time."""
    v = fund.get("pegRatio")
    return v if v is not None else fund.get("trailingPegRatio")


class FundamentalsCache:
    def __init__(self, cfg: Config):
        self.path = cfg.fundamentals_cache_path
        self.ttl = timedelta(hours=cfg.fundamentals_ttl_hours)
        self.max_workers = cfg.max_workers_fundamentals
        self._data: Dict[str, Tuple[datetime, dict]] = {}
        self._schema: Dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r") as f:
                raw = json.load(f)
            old = 0
            for t, entry in raw.items():
                ts = datetime.fromisoformat(entry["ts"])
                self._data[t] = (ts, entry["data"])
                self._schema[t] = int(entry.get("v", 1))
                old += self._schema[t] != FUND_SCHEMA
            LOG.info(f"Loaded fundamentals cache: {len(self._data)} tickers"
                     + (f" ({old} from an older schema, will refresh)" if old else ""))
        except Exception as e:
            LOG.warning(f"Could not load fundamentals cache ({e}); starting fresh")

    def _save(self) -> None:
        try:
            raw = {t: {"ts": ts.isoformat(), "v": self._schema.get(t, FUND_SCHEMA), "data": d}
                   for t, (ts, d) in self._data.items()}
            with open(self.path, "w") as f:
                json.dump(raw, f, default=str)
        except Exception as e:
            LOG.warning(f"Could not save fundamentals cache: {e}")

    def _expired(self, t: str, now: datetime) -> bool:
        return (t not in self._data
                or self._schema.get(t, 1) != FUND_SCHEMA
                or now - self._data[t][0] > self.ttl)

    def get_many(self, tickers: List[str]) -> Dict[str, dict]:
        now = datetime.now()
        need = [t for t in tickers if self._expired(t, now)]

        if need:
            LOG.info(f"Refreshing fundamentals for {len(need)} tickers "
                      f"({self.max_workers} parallel workers)...")

            def _fetch_one(t):
                try:
                    info = yf.Ticker(t).get_info() or {}
                    kept = {k: info[k] for k in FUND_FIELDS if info.get(k) is not None}
                    s = kept.get("longBusinessSummary")
                    if isinstance(s, str) and len(s) > SUMMARY_CACHE_CHARS:
                        kept["longBusinessSummary"] = s[:SUMMARY_CACHE_CHARS]
                    return t, kept
                except Exception as e:
                    LOG.debug(f"fundamentals failed for {t}: {e}")
                    return t, {}

            refreshed = kept_old = missing = 0
            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futures = [ex.submit(_fetch_one, t) for t in need]
                done = 0
                for fut in as_completed(futures):
                    t, info = fut.result()
                    if info:
                        self._data[t] = (now, info)
                        self._schema[t] = FUND_SCHEMA
                        refreshed += 1
                    elif t in self._data:
                        # Fetch failed (often a rate-limit). Keep the older
                        # copy rather than replacing good data with nothing;
                        # its timestamp is untouched so it retries next run.
                        kept_old += 1
                    else:
                        # Never cache an empty result - that used to blank a
                        # company's name/sector/fundamentals for the whole TTL.
                        missing += 1
                    done += 1
                    if done % 100 == 0:
                        LOG.info(f"  fundamentals progress: {done}/{len(need)}")
            LOG.info(f"Fundamentals: {refreshed} refreshed, {kept_old} kept from cache after a failed "
                     f"refresh, {missing} unavailable")
            self._save()

        return {t: self._data[t][1] for t in tickers if t in self._data}


# --------------------------------------------------------------------------
# Strategy scoring
# --------------------------------------------------------------------------

def score_minervini(ind: dict, rs_rating: Optional[int]) -> Tuple[int, List[Tuple[str, bool]]]:
    """Minervini Trend Template - 8 classic criteria. Returns
    (count_passed, criteria) where criteria is ALWAYS all 8 labeled
    (label, met) pairs in a fixed order - not just the ones that passed -
    so callers can render a full checklist (used by the dashboard's
    per-stock drill-down detail view)."""
    c1 = ind["last_close"] > ind["sma150"] and ind["last_close"] > ind["sma200"]
    c2 = ind["sma150"] > ind["sma200"]
    c3 = not np.isnan(ind["sma200_1mo_ago"]) and ind["sma200"] > ind["sma200_1mo_ago"]
    c4 = ind["sma50"] > ind["sma150"] and ind["sma50"] > ind["sma200"]
    c5 = ind["last_close"] > ind["sma50"]
    c6 = ind["last_close"] >= ind["wk52_low"] * 1.30
    c7 = ind["last_close"] >= ind["wk52_high"] * 0.75
    c8 = (rs_rating is not None) and (rs_rating >= 70)

    rs_label = f"RS Rating >= 70 (currently {rs_rating})" if rs_rating is not None else "RS Rating >= 70 (unavailable)"
    criteria = [
        ("Price above rising 150-day & 200-day average", c1),
        ("150-day average above 200-day average", c2),
        ("200-day average trending up vs. 1 month ago", c3),
        ("50-day average above both 150-day & 200-day", c4),
        ("Price above 50-day average", c5),
        ("At least 30% above 52-week low", c6),
        ("Within 25% of 52-week high", c7),
        (rs_label, c8),
    ]
    passed = sum(1 for _, met in criteria if met)
    return passed, criteria


def score_canslim(fund: dict, ind: dict, rs_rating: Optional[int]) -> Tuple[float, List[str]]:
    """Approximate CANSLIM per-stock factors (M is handled at market level)."""
    notes = []
    score = 0.0

    q_growth = fund.get("earningsQuarterlyGrowth")
    if q_growth is not None and q_growth > 0.25:
        score += 1; notes.append(f"C: qtr EPS growth {q_growth*100:.0f}%")
    a_growth = fund.get("earningsGrowth")
    if a_growth is not None and a_growth > 0.20:
        score += 1; notes.append(f"A: ann EPS growth {a_growth*100:.0f}%")
    near_high = ind["last_close"] >= ind["wk52_high"] * 0.90
    if near_high:
        score += 1; notes.append("N: near new highs")
    if fund.get("_rel_volume_ok"):
        score += 1; notes.append("S: heavy volume / demand")
    if rs_rating is not None and rs_rating >= 80:
        score += 1; notes.append(f"L: RS leader ({rs_rating})")
    inst_pct = fund.get("heldPercentInstitutions")
    if inst_pct is not None and inst_pct > 0.40:
        score += 1; notes.append(f"I: institutional {inst_pct*100:.0f}%")

    return score, notes


def score_zanger(ind: dict, intraday: dict) -> Tuple[float, List[str]]:
    """Zanger-style: explosive breakout on heavy volume, big % day."""
    notes = []
    score = 0.0
    if intraday.get("day_high", 0) > ind["prior_20d_high"] > 0:
        score += 1; notes.append("New 20d-high breakout")
    rvol = intraday.get("rel_volume")
    if rvol is not None and rvol >= 1.5:
        score += 1; notes.append(f"RelVol {rvol:.1f}x")
    pct_change = intraday.get("pct_change")
    if pct_change is not None and pct_change >= 4.0:
        score += 1; notes.append(f"+{pct_change:.1f}% today")
    return score, notes


def score_qullamaggie(ind: dict, intraday: dict, rs_rating: Optional[int]) -> Tuple[float, List[str]]:
    """Qullamaggie-style: strong RS, sufficient ADR%, tight base, breakout/EP."""
    notes = []
    score = 0.0
    if rs_rating is not None and rs_rating >= 90:
        score += 1; notes.append(f"Top-decile RS ({rs_rating})")
    if ind["adr20_pct"] >= 4.0:
        score += 1; notes.append(f"ADR {ind['adr20_pct']:.1f}%")
    if not np.isnan(ind["tightness_ratio"]) and ind["tightness_ratio"] < 0.6:
        score += 1; notes.append("Tight base (contraction)")

    gap_pct = intraday.get("gap_pct")
    rvol = intraday.get("rel_volume")
    is_ep = gap_pct is not None and gap_pct >= 6.0 and rvol is not None and rvol >= 2.0
    is_breakout = intraday.get("day_high", 0) > ind["prior_50d_high"] > 0
    if is_ep:
        score += 1; notes.append(f"Episodic pivot (gap {gap_pct:.1f}%)")
    elif is_breakout:
        score += 1; notes.append("50d breakout")

    dist_from_50 = (ind["last_close"] / ind["sma50"] - 1.0) * 100.0
    if dist_from_50 > 25:
        notes.append(f"CAUTION: {dist_from_50:.0f}% above 50MA (extended)")

    return score, notes


def score_value(fund: dict) -> Tuple[float, List[str]]:
    """Traditional value/quality metrics - PEG, PE, margins, debt/equity."""
    notes = []
    score = 0.0
    pe = fund.get("trailingPE")
    peg = _peg(fund)
    if peg is not None and 0 < peg < 2.0:
        score += 1; notes.append(f"PEG {peg:.2f}")
    elif pe is not None and 0 < pe < 25:
        score += 0.5; notes.append(f"PE {pe:.1f}")
    profit_margin = fund.get("profitMargins")
    if profit_margin is not None and profit_margin > 0.10:
        score += 1; notes.append(f"Profit margin {profit_margin*100:.0f}%")
    de = fund.get("debtToEquity")
    if de is not None and de < 100:
        score += 0.5; notes.append("Reasonable debt/equity")
    return score, notes


def composite_scores(mini_passed: int, can_score: float, zan_score: float,
                      qm_score: float, val_score: float, cfg: Config) -> Tuple[float, float]:
    """Returns (momentum_score, quality_value_score)."""
    momentum = (
        cfg.w1_trend * (mini_passed / 8.0)
        + cfg.w1_canslim * (can_score / 6.0)
        + cfg.w1_zanger * (zan_score / 3.0)
        + cfg.w1_qmaggie * (qm_score / 3.0)
        + cfg.w1_value * (val_score / 2.5)
    )
    quality_value = (
        cfg.w2_trend * (mini_passed / 8.0)
        + cfg.w2_canslim * (can_score / 6.0)
        + cfg.w2_value * (val_score / 2.5)
    )
    return momentum, quality_value


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def is_market_open(now_et: Optional[datetime] = None) -> bool:
    now_et = now_et or datetime.now(ET)
    if now_et.weekday() >= 5:
        return False
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now_et <= close_t
    # NOTE: informational only in manual mode; doesn't block a run, and
    # doesn't account for market holidays.


def _clean_num(v):
    """Sanitizes a value for JSON export: NaN/inf -> None (real JSON has no
    NaN token), everything else passed through unchanged."""
    try:
        if v is None:
            return None
        if isinstance(v, (float, np.floating)) and (np.isnan(v) or np.isinf(v)):
            return None
    except Exception:
        return None
    return v


def _pct(v, digits: int = 1):
    """Converts a fraction (0.05) to a rounded percentage-point number
    (5.0) for display, sanitizing NaN/None along the way."""
    v = _clean_num(v)
    return round(v * 100, digits) if v is not None else None


def _num(v, digits: int = 2):
    v = _clean_num(v)
    return round(v, digits) if v is not None else None


def trim_summary(text, max_chars: int = 520) -> str:
    """Opening sentences of a business description, cut at a sentence end so
    the site shows a clean paragraph rather than a mid-word fragment."""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    # A sentence ends at . ! ? followed by a space and a capital letter, which
    # keeps "Inc. designs" and "U.S. and" intact.
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    out = ""
    for s in sentences:
        if len(out) + len(s) + 1 > max_chars:
            break
        out = (out + " " + s).strip()
    if not out:  # first sentence alone is too long: cut at a word boundary
        out = text[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return out


def company_profile(fund: dict) -> Optional[dict]:
    """Who the company is, for the drill-down. None when Yahoo had nothing."""
    if not fund:
        return None
    city, state, country = fund.get("city"), fund.get("state"), fund.get("country")
    if city and country in ("United States", "USA", None) and state:
        hq = f"{city}, {state}"
    elif city and country:
        hq = f"{city}, {country}"
    else:
        hq = city or country or None
    prof = {
        "summary": trim_summary(fund.get("longBusinessSummary")) or None,
        "website": fund.get("website") or None,
        "employees": _clean_num(fund.get("fullTimeEmployees")),
        "hq": hq,
        "market_cap": _num(fund.get("marketCap"), 0),
    }
    return prof if any(v is not None for v in prof.values()) else None


def earnings_info(fund: dict, today=None) -> Optional[dict]:
    """Next (and very recent) earnings date from Yahoo's epoch timestamps.
    Earnings proximity is the single most practical risk fact for anyone
    reading a momentum screen: a stock reporting in two days can gap 15%
    either way regardless of how good its chart is."""
    if not fund:
        return None
    today = today or datetime.now(ET).date()
    dates = set()
    for k in ("earningsTimestamp", "earningsTimestampStart", "earningsTimestampEnd"):
        v = fund.get(k)
        try:
            if v is not None and float(v) > 0:
                secs = float(v) / 1000.0 if float(v) > 1e11 else float(v)  # tolerate milliseconds
                dates.add(datetime.fromtimestamp(secs, ET).date())
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    if not dates:
        return None
    out = {}
    upcoming = sorted(d for d in dates if d >= today)
    past = sorted(d for d in dates if d < today)
    if upcoming and (upcoming[0] - today).days <= 120:
        out["next"] = upcoming[0].isoformat()
        out["days"] = (upcoming[0] - today).days
        out["estimate"] = bool(fund.get("isEarningsDateEstimate"))
    if past and (today - past[-1]).days <= 30:
        out["last"] = past[-1].isoformat()
        out["days_ago"] = (today - past[-1]).days
    return out or None


def spark_series(close: Optional[pd.Series], dates: List[str]) -> Optional[dict]:
    """Closing prices and the 50-day average aligned to the shared date axis,
    for the drill-down's price chart. Missing sessions are null."""
    if close is None or close.empty or not dates:
        return None
    s = close.copy()
    s.index = [pd.Timestamp(ts).date().isoformat() for ts in s.index]
    s = s[~s.index.duplicated(keep="last")]
    sma = s.rolling(50).mean()
    c = [_num(s.get(d)) for d in dates]
    m = [_num(sma.get(d)) for d in dates]
    if sum(v is not None for v in c) < 20:
        return None
    return {"c": c, "sma50": m}


def run_scan(cfg: Config) -> Tuple[pd.DataFrame, pd.DataFrame, str, int, int, dict]:
    """Runs one full pass over the entire universe. Every candidate that
    clears the basic price/liquidity filter gets scored - none are
    dropped for failing the Minervini gate. Instead each row is flagged
    Qualifies=Yes/No, so if fewer than 20 names fully qualify (or none do)
    you still get a ranked top-20 of the CLOSEST candidates rather than
    an empty result. Returns
    (section1_df, section2_df, market_status, total_scored, qualifying_count, stats)
    where stats describes data coverage for the publish gate in main()."""
    universe = get_universe(cfg)
    LOG.info(f"Universe size: {len(universe)} tickers (+ benchmark {cfg.benchmark})")

    daily_hist = fetch_daily_history(universe, cfg)
    indicators: Dict[str, dict] = {}
    closes: Dict[str, pd.Series] = {}   # for the history tracker + price charts
    for t, df in daily_hist.items():
        ind = compute_indicators(df)
        if ind is not None:
            indicators[t] = ind
            try:
                closes[t] = df["Close"].dropna()
            except Exception:
                pass
    LOG.info(f"Computed indicators for {len(indicators)} / {len(daily_hist)} tickers "
              f"(some are dropped for insufficient history)")

    rs_ratings = compute_rs_ratings(indicators)
    market_status = market_health(indicators.get(cfg.benchmark))

    non_benchmark = [t for t in indicators if t != cfg.benchmark]
    nan_dollar_vol = sum(1 for t in non_benchmark if np.isnan(indicators[t]["avg_dollar_vol20"]))
    if nan_dollar_vol > len(non_benchmark) * 0.1:
        # More than 10% NaN is a red flag for a data-quality problem
        # upstream (e.g. batched download misalignment), not a normal
        # day - surface it loudly instead of it silently tanking the
        # liquidity filter pass rate.
        LOG.warning(f"{nan_dollar_vol} / {len(non_benchmark)} tickers have NaN avg-dollar-volume "
                    f"(missing/misaligned data) - if this is unusually high, something upstream may "
                    f"be corrupting the data batch rather than these being genuinely illiquid names.")

    candidates = [
        t for t in indicators
        if t != cfg.benchmark
        and indicators[t]["last_close"] >= cfg.min_price
        and indicators[t]["avg_dollar_vol20"] >= cfg.min_avg_dollar_volume
    ]
    LOG.info(f"{len(candidates)} / {len(non_benchmark)} pass price/liquidity filters "
              f"({nan_dollar_vol} excluded for missing/NaN data, "
              f"{max(0, len(non_benchmark) - len(candidates) - nan_dollar_vol)} genuinely below threshold) "
              f"-> pulling full intraday + fundamentals for this entire group")

    intraday = fetch_intraday(candidates, indicators, cfg)
    fund_cache = FundamentalsCache(cfg)
    fundamentals = fund_cache.get_many(candidates)

    min_needed = 8 if cfg.strict_trend_template else cfg.min_trend_criteria
    rows = []
    for t in candidates:
        ind = indicators.get(t)
        intr = intraday.get(t)
        fund = fundamentals.get(t, {})
        if ind is None or intr is None:
            continue

        rs = rs_ratings.get(t)
        fund = dict(fund)  # don't write scan-specific flags into the cache's dict
        fund["_rel_volume_ok"] = (intr.get("rel_volume") or 0) >= 1.3

        mini_passed, mini_criteria = score_minervini(ind, rs)
        mini_notes = [label for label, met in mini_criteria if met]
        can_score, can_notes = score_canslim(fund, ind, rs)
        zan_score, zan_notes = score_zanger(ind, intr)
        qm_score, qm_notes = score_qullamaggie(ind, intr, rs)
        val_score, val_notes = score_value(fund)
        momentum_score, quality_score = composite_scores(
            mini_passed, can_score, zan_score, qm_score, val_score, cfg)

        qualifies = mini_passed >= min_needed

        price_now = intr["last_price"]
        target = _clean_num(fund.get("targetMeanPrice"))
        rows.append({
            "_Profile": company_profile(fund),
            "_Earnings": earnings_info(fund),
            "_MiniBits": sum(1 << i for i, (_, met) in enumerate(mini_criteria) if met),
            "_MarketCap": _clean_num(fund.get("marketCap")),
            "Ticker": t,
            # Company identity: already present in the fundamentals payload we
            # fetch, previously discarded. Showing a bare ticker with no company
            # name is a usability failure - most people do not know what MRSH or
            # PODD trade as. Sector also matters here because CANSLIM's "L"
            # (leadership) is explicitly about being strong within a strong group.
            "Company": (fund.get("longName") or fund.get("shortName") or "") if fund else "",
            "Sector": (fund.get("sector") or "") if fund else "",
            "Industry": (fund.get("industry") or "") if fund else "",
            "Price": round(intr["last_price"], 2),
            "%Chg": round(intr["pct_change"], 2) if intr.get("pct_change") is not None else None,
            "RelVol": round(intr["rel_volume"], 2) if intr.get("rel_volume") is not None else None,
            "RS": rs,
            "Trend": f"{mini_passed}/8",
            "_TrendN": mini_passed,  # hidden helper: sort key, dropped before display
            "Qualifies": "Yes" if qualifies else "No",
            "CANSLIM": f"{can_score:.1f}/6",
            "Zanger": f"{zan_score:.0f}/3",
            "Qullamaggie": f"{qm_score:.0f}/3",
            "Value": f"{val_score:.1f}/2.5",
            "MomentumScore": round(momentum_score, 1),
            "QualityScore": round(quality_score, 1),
            "Why": "; ".join(mini_notes + can_notes + zan_notes + qm_notes + val_notes),
            # Hidden columns (underscore prefix, same convention as _TrendN):
            # not for CSV/console display, only read by _rows_for_json below
            # to power the dashboard's per-stock drill-down detail view.
            "_MiniCriteria": mini_criteria,   # ALL 8 (label, met) pairs
            "_CanNotes": can_notes,
            "_ZanNotes": zan_notes,
            "_QmNotes": qm_notes,
            "_ValNotes": val_notes,
            # Raw trader-facing metrics (not just synthesized signals) for
            # the drill-down detail view: moving averages, 52-week range,
            # volatility, trailing returns, volume, and fundamentals -
            # always populated where the underlying data exists, so the
            # detail view has real numbers to show even when few or no
            # "signals" happened to trigger that day.
            "_Metrics": {
                "sma50": _num(ind.get("sma50")),
                "sma150": _num(ind.get("sma150")),
                "sma200": _num(ind.get("sma200")),
                "wk52_high": _num(ind.get("wk52_high")),
                "wk52_low": _num(ind.get("wk52_low")),
                "pct_off_52w_high": _pct((ind["last_close"] / ind["wk52_high"] - 1.0) if ind.get("wk52_high") else None),
                "pct_off_52w_low": _pct((ind["last_close"] / ind["wk52_low"] - 1.0) if ind.get("wk52_low") else None),
                "adr20_pct": _num(ind.get("adr20_pct")),
                "tightness_ratio": _num(ind.get("tightness_ratio"), 2),
                "ret_3m_pct": _pct(ind.get("ret_3m")),
                "ret_6m_pct": _pct(ind.get("ret_6m")),
                "ret_9m_pct": _pct(ind.get("ret_9m")),
                "ret_12m_pct": _pct(ind.get("ret_12m")),
                "avg_vol50": _num(ind.get("avg_vol50"), 0),
                "avg_dollar_vol20": _num(ind.get("avg_dollar_vol20"), 0),
                "prior_20d_high": _num(ind.get("prior_20d_high")),
                "prior_50d_high": _num(ind.get("prior_50d_high")),
                "trailing_pe": _num(fund.get("trailingPE")),
                "peg_ratio": _num(_peg(fund)),
                "profit_margin_pct": _pct(fund.get("profitMargins")),
                "debt_to_equity": _num(fund.get("debtToEquity")),
                "eps_qtr_growth_pct": _pct(fund.get("earningsQuarterlyGrowth")),
                "eps_ann_growth_pct": _pct(fund.get("earningsGrowth")),
                "institutional_pct": _pct(fund.get("heldPercentInstitutions")),
                # Context beyond the scoring inputs (same get_info payload).
                "forward_pe": _num(fund.get("forwardPE")),
                "revenue_growth_pct": _pct(fund.get("revenueGrowth")),
                "gross_margin_pct": _pct(fund.get("grossMargins")),
                "operating_margin_pct": _pct(fund.get("operatingMargins")),
                "roe_pct": _pct(fund.get("returnOnEquity")),
                "short_pct_float": _pct(fund.get("shortPercentOfFloat")),
                "beta": _num(fund.get("beta")),
                "analyst_rating": fund.get("recommendationKey") or None,
                "analyst_mean": _num(fund.get("recommendationMean")),
                "analyst_count": _clean_num(fund.get("numberOfAnalystOpinions")),
                "target_mean": _num(target),
                "target_upside_pct": _pct(target / price_now - 1.0) if target and price_now else None,
            },
        })

    # Which trading session the intraday numbers describe (the most common
    # session date across tickers). Lets the website say "results from
    # Friday's session" instead of guessing from the timestamp alone.
    session_dates = pd.Series([v.get("session_date") for v in intraday.values() if v.get("session_date")])
    stats = {
        "universe": len(universe),
        "with_history": len(indicators),
        "with_intraday": len(intraday),
        "scored": len(rows),
        "benchmark_ok": cfg.benchmark in indicators,
        "session_date": session_dates.mode().iloc[0] if not session_dates.empty else None,
    }

    extras = {"closes": closes, "benchmark": cfg.benchmark}

    if not rows:
        empty = pd.DataFrame()
        return empty, empty, market_status, 0, 0, stats, extras

    pool = pd.DataFrame(rows)
    qualifying_count = int((pool["Qualifies"] == "Yes").sum())

    # Universe-wide rank on each list's own ordering, so a looked-up stock
    # can say "#147 of 887" rather than just "not in the top 10".
    for col, score in (("_MomRank", "MomentumScore"), ("_QualRank", "QualityScore")):
        order = pool.sort_values(["_TrendN", score], ascending=[False, False]).index
        pool.loc[order, col] = range(1, len(order) + 1)
        pool[col] = pool[col].astype(int)

    # Rank the WHOLE pool (qualifying and non-qualifying alike) so a name
    # that's close to the Minervini gate but not quite there still shows
    # up when nothing fully qualifies - the Qualifies column tells you
    # which is which. Minervini's Trend Template pass-count (_TrendN) is
    # the PRIMARY sort key in both sections - not just one weighted input
    # among several - so the ranking always favors the stock closest to
    # a full 8/8 Trend Template read first; the composite score only
    # breaks ties within the same Trend Template count.
    section1 = pool.sort_values(["_TrendN", "MomentumScore"], ascending=[False, False]).head(cfg.top_n_section)
    remaining = pool[~pool["Ticker"].isin(section1["Ticker"])]
    section2 = remaining.sort_values(["_TrendN", "QualityScore"], ascending=[False, False]).head(cfg.top_n_section)

    # Everything below is context for the website (breadth, sector
    # leadership, the searchable universe, price charts). None of it changes
    # the ranking, and a failure here must never stop the core screen from
    # publishing, so each piece is computed defensively.
    top_tickers = set(section1["Ticker"]) | set(section2["Ticker"])
    try:
        stats["breadth"] = compute_breadth(indicators, intraday, pool, cfg.benchmark)
    except Exception as e:
        LOG.warning(f"Breadth stats skipped: {e}")
    try:
        stats["sectors"], stats["industries"] = compute_group_stats(pool, top_tickers)
    except Exception as e:
        LOG.warning(f"Sector stats skipped: {e}")
    try:
        bench = closes.get(cfg.benchmark)
        spark_dates = ([pd.Timestamp(ts).date().isoformat() for ts in bench.index[-126:]]
                       if bench is not None else [])
        stats["spark_dates"] = spark_dates
        sparks = {t: spark_series(closes.get(t), spark_dates) for t in top_tickers}
        section1 = section1.assign(_Spark=[sparks.get(t) for t in section1["Ticker"]])
        section2 = section2.assign(_Spark=[sparks.get(t) for t in section2["Ticker"]])
    except Exception as e:
        LOG.warning(f"Price-chart data skipped: {e}")
    extras["pool"] = pool
    extras["fundamentals"] = fundamentals

    # _TrendN was only needed for sorting; drop it now. The other hidden
    # _Xxx columns are kept - export_json still needs to read them - and
    # are stripped later, right before console printing / CSV export.
    section1 = section1.drop(columns=["_TrendN"]).reset_index(drop=True)
    section2 = section2.drop(columns=["_TrendN"]).reset_index(drop=True)

    return (section1, section2,
            market_status, len(pool), qualifying_count, stats, extras)


def compute_breadth(indicators: Dict[str, dict], intraday: Dict[str, dict],
                    pool: pd.DataFrame, benchmark: str) -> dict:
    """Market breadth across the whole universe - how many stocks are
    participating, not just how the index looks. Minervini and O'Neil both
    lean on this: a rising index with shrinking participation is a warning,
    and the count of stocks passing the Trend Template is itself a breadth
    gauge."""
    inds = [v for t, v in indicators.items() if t != benchmark]
    n = len(inds)
    if n == 0:
        return {}
    above50 = sum(1 for v in inds if v["last_close"] > v["sma50"])
    above200 = sum(1 for v in inds if v["last_close"] > v["sma200"])
    new_hi = sum(1 for v in inds if v["last_close"] >= v["wk52_high"] * 0.9999)
    new_lo = sum(1 for v in inds if v["last_close"] <= v["wk52_low"] * 1.0001)
    chg = [v.get("pct_change") for t, v in intraday.items() if t != benchmark and v.get("pct_change") is not None]
    trend_counts = pool["_TrendN"].value_counts().to_dict()
    return {
        "n": n,
        "pct_above_50": round(above50 / n * 100, 1),
        "pct_above_200": round(above200 / n * 100, 1),
        "new_highs": new_hi,
        "new_lows": new_lo,
        "advancers": sum(1 for c in chg if c > 0),
        "decliners": sum(1 for c in chg if c < 0),
        "trend_8": int(trend_counts.get(8, 0)),
        "trend_dist": [int(trend_counts.get(k, 0)) for k in range(9)],
    }


def compute_group_stats(pool: pd.DataFrame, top_tickers: set) -> Tuple[List[dict], List[dict]]:
    """Leadership by sector and by industry. CANSLIM's 'L' and O'Neil's
    industry-group work both say strong stocks cluster: a name in a group
    where many peers also pass the Trend Template is better supported than a
    lone outlier."""
    df = pool.copy()
    df["_r3"] = [m.get("ret_3m_pct") if isinstance(m, dict) else None for m in df["_Metrics"]]
    df["_q"] = df["Qualifies"] == "Yes"
    df = df.sort_values(["_TrendN", "MomentumScore"], ascending=[False, False])

    def summarize(g: pd.DataFrame) -> dict:
        rs = pd.to_numeric(g["RS"], errors="coerce").dropna()
        r3 = pd.to_numeric(g["_r3"], errors="coerce").dropna()
        return {
            "n": int(len(g)),
            "qualified": int(g["_q"].sum()),
            "pct_qualified": round(float(g["_q"].mean()) * 100, 1),
            "avg_rs": round(float(rs.mean()), 1) if len(rs) else None,
            "median_ret_3m": round(float(r3.median()), 1) if len(r3) else None,
            "in_top": int(g["Ticker"].isin(top_tickers).sum()),
            "leaders": list(g["Ticker"].head(3)),
        }

    sectors = []
    for name, g in df[df["Sector"].astype(str) != ""].groupby("Sector", sort=False):
        sectors.append({"name": name, **summarize(g)})
    sectors.sort(key=lambda s: (s["pct_qualified"], s["avg_rs"] or 0), reverse=True)

    industries = []
    for name, g in df[df["Industry"].astype(str) != ""].groupby("Industry", sort=False):
        if len(g) < 4:  # too few members to say anything about a group
            continue
        industries.append({"name": name, "sector": g["Sector"].iloc[0], **summarize(g)})
    industries.sort(key=lambda s: (s["avg_rs"] or 0), reverse=True)
    return sectors, industries[:12]


def publish_problem(stats: dict, cfg: Config) -> Optional[str]:
    """Returns a reason string if this scan is too degraded to publish, else
    None. Without this, a run where Yahoo throttled most requests would
    still 'succeed' and overwrite a good screen with a thin or empty one -
    and the site would look freshly updated while showing garbage."""
    universe = max(stats.get("universe", 0), 1)
    coverage = stats.get("scored", 0) / universe
    if not stats.get("benchmark_ok"):
        return "no benchmark (SPY) data, so market status and RS ratings are unreliable"
    if coverage < cfg.min_coverage:
        return (f"only {stats.get('scored', 0)} of {universe} stocks could be scored "
                f"({coverage:.0%} < required {cfg.min_coverage:.0%})")
    return None


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def _print_table(df: pd.DataFrame, score_col: str, drop_cols: List[str]) -> None:
    display_df = df.drop(columns=[c for c in df.columns if c in drop_cols or c.startswith("_")])
    if HAVE_TABULATE:
        print(tabulate(display_df, headers="keys", tablefmt="simple", showindex=False))
    else:
        print(display_df.to_string(index=False))
    print("\nNotes:")
    for _, row in df.iterrows():
        flag = "" if row.get("Qualifies") == "Yes" else "  [does NOT fully meet Minervini gate - closest match shown]"
        print(f"  {row['Ticker']:<6} -> {row['Why']}{flag}")


def print_results(section1: pd.DataFrame, section2: pd.DataFrame, market_status: str,
                   total_scored: int, qualifying_count: int, min_needed: int) -> None:
    print("\n" + "=" * 100)
    print(f"  Scan @ {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}   |   "
          f"Market (M): {market_status}   |   Scored: {total_scored}   |   "
          f"Fully qualify (>= {min_needed}/8 Minervini): {qualifying_count}")
    if total_scored > 0 and qualifying_count == 0:
        print("  NOTE: No stock in today's universe fully meets the Minervini Trend Template gate.")
        print("        Showing the 20 CLOSEST candidates instead, ranked the same way - each is")
        print("        flagged 'Qualifies: No' below so you know none of these are a clean signal.")
    elif 0 < qualifying_count < 20:
        print(f"  NOTE: Only {qualifying_count} name(s) fully qualify today. The rest of this list")
        print("        is filled with the closest non-qualifying candidates (flagged 'Qualifies: No').")
    print("=" * 100)

    print(f"\n--- SECTION 1: Today's Momentum Leaders "
          f"(Minervini + CANSLIM + Zanger + Qullamaggie) ---")
    if section1.empty:
        print("No candidates this run (universe returned no usable data).")
    else:
        _print_table(section1, "MomentumScore",
                      drop_cols=["Why", "QualityScore", "Company", "Sector", "Industry", "_MiniCriteria", "_CanNotes", "_ZanNotes", "_QmNotes", "_ValNotes", "_Metrics"])

    print(f"\n--- SECTION 2: Fundamentals & Trend-Quality Picks "
          f"(value-weighted, Minervini-heaviest) ---")
    if section2.empty:
        print("No candidates this run (universe returned no usable data).")
    else:
        _print_table(section2, "QualityScore",
                      drop_cols=["Why", "MomentumScore", "%Chg", "RelVol", "Zanger", "Qullamaggie", "Sector", "Industry",
                                 "_MiniCriteria", "_CanNotes", "_ZanNotes", "_QmNotes", "_ValNotes", "_Metrics"])


def save_results(section1: pd.DataFrame, section2: pd.DataFrame, cfg: Config) -> None:
    if section1.empty and section2.empty:
        return
    os.makedirs(cfg.output_dir, exist_ok=True)
    fname = os.path.join(cfg.output_dir, f"scan_{datetime.now(ET).strftime('%Y%m%d_%H%M%S')}.csv")
    s1 = section1.drop(columns=[c for c in section1.columns if c.startswith("_")]).copy()
    s1.insert(0, "Section", "1_Momentum")
    s2 = section2.drop(columns=[c for c in section2.columns if c.startswith("_")]).copy()
    s2.insert(0, "Section", "2_ValueQuality")
    combined = pd.concat([s1, s2], ignore_index=True)
    combined.insert(0, "timestamp", datetime.now(ET).isoformat())
    combined.to_csv(fname, index=False)
    LOG.info(f"Saved results -> {fname}")


def _opt(row, key):
    """A hidden column's value, or None if the column is absent/NaN."""
    v = row.get(key) if hasattr(row, "get") else None
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return v


def _rows_for_json(df: pd.DataFrame, score_col: str,
                   list_stats: Optional[Dict[str, dict]] = None) -> List[dict]:
    if df.empty:
        return []
    out = []
    list_stats = list_stats or {}
    for _, row in df.iterrows():
        trend_n = int(str(row["Trend"]).split("/")[0])
        extra = {
            # Company profile, earnings calendar, 6-month price series, and
            # this ticker's record on the screen. Optional by design: older
            # scans and failed lookups simply omit them.
            "profile": _opt(row, "_Profile"),
            "earnings": _opt(row, "_Earnings"),
            "spark": _opt(row, "_Spark"),
            "universe_rank": {"momentum": _opt(row, "_MomRank"), "quality": _opt(row, "_QualRank")},
            "history": list_stats.get(row["Ticker"]),
        }
        out.append({**{k: v for k, v in extra.items() if v is not None}, **{
            "ticker": row["Ticker"],
            "company": row.get("Company", ""),
            "sector": row.get("Sector", ""),
            "industry": row.get("Industry", ""),
            "price": row["Price"],
            "pct_chg": row["%Chg"],
            "rel_vol": row["RelVol"],
            "rs": row["RS"],
            "trend": row["Trend"],
            "trend_n": trend_n,
            "qualifies": row["Qualifies"] == "Yes",
            "canslim": row["CANSLIM"],
            "zanger": row["Zanger"],
            "qullamaggie": row["Qullamaggie"],
            "value": row["Value"],
            "score": row[score_col],
            "why": row["Why"],
            # Structured detail data for the dashboard's per-stock
            # drill-down: the full 8-item Trend Template checklist
            # (met AND unmet, not just what passed) plus the other four
            # frameworks' triggered signals, grouped separately instead
            # of flattened into one "why" blob.
            "trend_criteria": [[label, bool(met)] for label, met in row["_MiniCriteria"]],
            "signals": {
                "canslim": list(row["_CanNotes"]),
                "zanger": list(row["_ZanNotes"]),
                "qullamaggie": list(row["_QmNotes"]),
                "value": list(row["_ValNotes"]),
            },
            "metrics": dict(row["_Metrics"]),
        }})
    return out


def write_json_atomic(path: str, payload, indent: Optional[int] = None) -> None:
    """Write to a temp file and swap it in, so an interrupted run can never
    leave a half-written (unparseable) file for the site to load."""
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        if indent is None:
            json.dump(payload, f, default=_json_default, separators=(",", ":"))
        else:
            json.dump(payload, f, default=_json_default, indent=indent)
    os.replace(tmp_path, path)


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if (np.isnan(o) or np.isinf(o)) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


def export_json(section1: pd.DataFrame, section2: pd.DataFrame, market_status: str,
                 total_scored: int, qualifying_count: int, min_needed: int, path: str,
                 stats: Optional[dict] = None, generated_at: Optional[datetime] = None,
                 list_stats: Optional[Dict[str, dict]] = None) -> None:
    """Writes the dashboard's data feed - overwrites the SAME file every run
    (not timestamped) so the website always reads the latest scan."""
    stats = stats or {}
    payload = {
        "generated_at": (generated_at or datetime.now(ET)).isoformat(),
        # Trading date (ET) the prices describe. Differs from generated_at's
        # date on pre-open, weekend and holiday runs.
        "session_date": stats.get("session_date"),
        "market_status": market_status,
        "universe_size": stats.get("universe"),
        "total_scored": total_scored,
        "qualifying_count": qualifying_count,
        "min_trend_needed": min_needed,
        # Market-wide context (optional; absent if it could not be computed).
        **{k: stats[k] for k in ("breadth", "sectors", "industries", "spark_dates") if stats.get(k)},
        "section1": _rows_for_json(section1, "MomentumScore", list_stats),
        "section2": _rows_for_json(section2, "QualityScore", list_stats),
    }
    write_json_atomic(path, payload)
    LOG.info(f"Wrote dashboard data -> {path}")


# --------------------------------------------------------------------------
# Site extras: searchable universe, company profiles, daily history
# --------------------------------------------------------------------------

UNIVERSE_FIELDS = ["t", "n", "s", "i", "p", "c", "rv", "rs", "tn", "tb", "q", "ms", "qs", "mr", "qr",
                   "r3", "r12", "hi", "adr", "s50", "s150", "s200", "h52", "l52", "mc", "ed"]


def build_universe_payload(pool: pd.DataFrame, generated_at: datetime, session_date) -> dict:
    """Every scored stock in compact column form (~100 KB), so visitors can
    look up any of the ~900 names - not just the 20 that made the lists.
    The top-20 keep their full breakdown in latest.json."""
    sectors: List[str] = []
    industries: List[str] = []

    def idx(lst, v):
        if not v:
            return None
        if v not in lst:
            lst.append(v)
        return lst.index(v)

    rows = []
    for _, r in pool.sort_values("_MomRank").iterrows():
        m = r["_Metrics"] if isinstance(r["_Metrics"], dict) else {}
        e = r.get("_Earnings") if isinstance(r.get("_Earnings"), dict) else {}
        mc = _clean_num(r.get("_MarketCap"))
        rows.append([
            r["Ticker"], r.get("Company") or "", idx(sectors, r.get("Sector")), idx(industries, r.get("Industry")),
            _num(r["Price"]), _num(r["%Chg"]), _num(r["RelVol"]), _clean_num(r["RS"]),
            int(r["_TrendN"]), int(r["_MiniBits"]), 1 if r["Qualifies"] == "Yes" else 0,
            _num(r["MomentumScore"], 1), _num(r["QualityScore"], 1), int(r["_MomRank"]), int(r["_QualRank"]),
            m.get("ret_3m_pct"), m.get("ret_12m_pct"), m.get("pct_off_52w_high"), m.get("adr20_pct"),
            m.get("sma50"), m.get("sma150"), m.get("sma200"), m.get("wk52_high"), m.get("wk52_low"),
            round(mc / 1e9, 2) if mc else None, e.get("days") if e else None,
        ])
    return {
        "generated_at": generated_at.isoformat(),
        "session_date": session_date,
        "fields": UNIVERSE_FIELDS,
        "sectors": sectors,
        "industries": industries,
        "rows": rows,
    }


def build_companies_payload(fundamentals: Dict[str, dict], existing: Optional[dict]) -> dict:
    """Company profiles keyed by ticker, merged onto the previous file so a
    stock that left the index keeps its description on the History page."""
    comp = dict((existing or {}).get("c") or {})
    for t, f in fundamentals.items():
        if not f:
            continue
        p = company_profile(f) or {}
        name = f.get("longName") or f.get("shortName")
        if not (name or p.get("summary")):
            continue
        entry = {"n": name, "s": f.get("sector"), "i": f.get("industry"),
                 "d": trim_summary(f.get("longBusinessSummary"), 420) or None,
                 "w": p.get("website"), "hq": p.get("hq"), "e": p.get("employees")}
        entry = {k: v for k, v in entry.items() if v is not None}
        # Never replace a richer old entry with a thinner one from a partial
        # Yahoo response.
        old = comp.get(t) or {}
        if len(entry) >= len(old) or "d" in entry:
            comp[t] = {**old, **entry}
    return {"c": dict(sorted(comp.items()))}


def build_stock_details(pool: pd.DataFrame, closes: Dict[str, pd.Series], spark_dates: List[str],
                        generated_at: datetime, session_date) -> dict:
    """Full per-stock detail for EVERY scored stock: the metrics, framework
    signals, earnings date, profile and six-month closing prices that
    latest.json only carries for the 20 listed names. tools/build_site.py
    turns this into one static page per stock (/stock/<ticker>).

    Build input only: visitors' browsers never download it (universe.json is
    still the compact lookup file), so its size (~3 MB) costs nothing on the
    site. Values are already rounded and NaN-free (see _Metrics / _num)."""
    def _dict(v):
        return v if isinstance(v, dict) else None

    def _notes(v):
        return [str(x) for x in v] if isinstance(v, (list, tuple)) else []

    stocks = {}
    for _, r in pool.iterrows():
        t = r["Ticker"]
        metrics = _dict(r.get("_Metrics")) or {}
        entry = {
            "rv": _num(r.get("RelVol")),
            "fw": {"canslim": r.get("CANSLIM"), "zanger": r.get("Zanger"),
                   "qullamaggie": r.get("Qullamaggie"), "value": r.get("Value")},
            "sig": {"canslim": _notes(r.get("_CanNotes")), "zanger": _notes(r.get("_ZanNotes")),
                    "qullamaggie": _notes(r.get("_QmNotes")), "value": _notes(r.get("_ValNotes"))},
            "m": {k: v for k, v in metrics.items() if v is not None},
            "e": _dict(r.get("_Earnings")),
            "pf": _dict(r.get("_Profile")),
            "sp": spark_series(closes.get(t), spark_dates) if spark_dates else None,
        }
        stocks[t] = {k: v for k, v in entry.items() if v is not None and v != {}}
    return {
        "generated_at": generated_at.isoformat(),
        "session_date": session_date,
        "spark_dates": spark_dates,
        "stocks": stocks,
    }


HISTORY_HORIZONS = (1, 5, 10, 20)   # trading days after the session a stock appeared


def history_picks(section1: List[dict], section2: List[dict]) -> List[dict]:
    """Compact record of one session's two lists (from latest.json-style rows)."""
    picks = []
    for sec, rows in ((1, section1), (2, section2)):
        for i, r in enumerate(rows or []):
            picks.append({
                "t": r.get("ticker"), "sec": sec, "r": i + 1,
                "p": r.get("price"), "c": r.get("pct_chg"), "rs": r.get("rs"),
                "tn": r.get("trend_n"), "sc": r.get("score"), "q": bool(r.get("qualifies")),
            })
    return picks


def upsert_history_day(history: dict, day: dict) -> bool:
    """Insert or replace one session in history. Each run rewrites the
    current session, so whichever scan runs LAST that day (normally the
    post-close run) becomes the session's permanent record. A finished
    session is never overwritten by a later weekend, holiday or next-morning
    rerun of the same data. Returns True if history changed."""
    days = history.setdefault("days", [])
    for i, d in enumerate(days):
        if d.get("date") == day["date"]:
            if d.get("final") and not day.get("same_day_final", day.get("final")):
                return False
            days[i] = {k: v for k, v in day.items() if k != "same_day_final"}
            return True
    days.append({k: v for k, v in day.items() if k != "same_day_final"})
    days.sort(key=lambda d: d["date"])
    return True


def _date_index(close: pd.Series) -> Tuple[Dict[str, int], List[float]]:
    dates = [pd.Timestamp(ts).date().isoformat() for ts in close.index]
    vals = [float(v) for v in close.values]
    return {d: i for i, d in enumerate(dates)}, vals


def compute_history_perf(history: dict, closes: Dict[str, pd.Series], benchmark: str,
                         last_bar_complete: bool) -> int:
    """Forward returns for every stock in history, measured close-to-close
    from the session it appeared: +1, +5, +10 and +20 trading days, and to
    date. The benchmark gets the same windows. A horizon that has not
    elapsed yet stays null. Values computed on an earlier run are kept when
    the current price data no longer reaches back that far.
    Returns the number of picks updated."""
    cache: Dict[str, Tuple[Dict[str, int], List[float]]] = {}

    def series(t):
        if t not in cache:
            s = closes.get(t)
            cache[t] = _date_index(s) if s is not None and not s.empty else ({}, [])
        return cache[t]

    def returns(t, date):
        pos_map, vals = series(t)
        i = pos_map.get(date)
        if i is None or not vals[i]:
            return None
        last = len(vals) - 1
        out = {}
        for h in HISTORY_HORIZONS:
            j = i + h
            ok = j <= last and (last_bar_complete or j < last)
            if ok:
                out[str(h)] = round((vals[j] / vals[i] - 1) * 100, 2)
        if last > i:
            out["td"] = round((vals[last] / vals[i] - 1) * 100, 2)
        return out

    updated = 0
    for day in history.get("days", []):
        b = returns(benchmark, day["date"])
        if b:
            day["spy"] = b
        for p in day.get("picks", []):
            f = returns(p["t"], day["date"])
            if f:
                p["f"] = f
                updated += 1
    return updated


def ticker_list_stats(history: dict, session_date: str) -> Dict[str, dict]:
    """Each ticker's record on the lists as of `session_date`: first session
    seen, sessions on the lists, current streak, and yesterday's rank."""
    days = [d for d in history.get("days", []) if d["date"] <= session_date]
    if not days:
        return {}
    by_day = [{p["t"]: p for p in d.get("picks", [])} for d in days]
    today = by_day[-1]
    prev = by_day[-2] if len(by_day) > 1 else {}
    out = {}
    for t, p in today.items():
        total = sum(1 for d in by_day if t in d)
        first = next(days[i]["date"] for i, d in enumerate(by_day) if t in d)
        streak = 0
        for d in reversed(by_day):
            if t not in d:
                break
            streak += 1
        pp = prev.get(t)
        out[t] = {"first_seen": first, "sessions": total, "streak": streak,
                  "prev_rank": pp["r"] if pp else None, "prev_section": pp["sec"] if pp else None}
    return out


def update_history(path: str, session_date: Optional[str], generated_at: datetime,
                   payload_rows: Tuple[List[dict], List[dict]], market_status: str,
                   total_scored: int, qualifying_count: int, stats: dict,
                   closes: Dict[str, pd.Series], benchmark: str,
                   fundamentals: Dict[str, dict]) -> Tuple[dict, bool]:
    """Load history.json, record this session, refresh forward returns."""
    history = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                history = json.load(f)
        except Exception as e:
            # Don't silently start over and wipe months of records.
            raise RuntimeError(f"history file unreadable ({e}); not overwriting it") from e
    history.setdefault("version", 1)
    history["horizons"] = list(HISTORY_HORIZONS)

    if not session_date:
        return history, False
    sd = datetime.fromisoformat(session_date).date()
    final = is_post_close(generated_at, sd)
    day = {
        "date": session_date,
        "generated_at": generated_at.isoformat(),
        "final": final,
        # Only a post-close run ON the session date may replace a final record.
        "same_day_final": final and generated_at.astimezone(ET).date() == sd,
        "market": market_status.split(" (")[0] if market_status else None,
        "scored": total_scored,
        "qualified": qualifying_count,
        "picks": history_picks(*payload_rows),
    }
    if stats.get("breadth"):
        day["breadth"] = {k: v for k, v in stats["breadth"].items() if k != "trend_dist"}
    if stats.get("sectors"):
        day["sectors"] = {s["name"]: s["qualified"] for s in stats["sectors"]}
    changed = upsert_history_day(history, day)

    # Names for every ticker ever listed (the History page shows them).
    names = history.setdefault("names", {})
    for d in history["days"]:
        for p in d.get("picks", []):
            f = fundamentals.get(p["t"]) or {}
            nm = f.get("longName") or f.get("shortName")
            if nm:
                names[p["t"]] = [nm, f.get("sector") or "", f.get("industry") or ""]

    compute_history_perf(history, closes, benchmark, last_bar_complete=final)
    history["updated_at"] = generated_at.isoformat()
    history["as_of"] = session_date
    return history, changed


# --------------------------------------------------------------------------
# Built-in self-test (no network, no second file - run with --selftest)
# --------------------------------------------------------------------------

def _make_synthetic_ohlcv(n=400, trend=0.0008, vol=0.015, start_price=50.0,
                           start_vol=1_000_000, spike_last_day=False, seed=42):
    rng = np.random.default_rng(seed)
    # Over-generate then slice to exactly n: pd.bdate_range(end=..., periods=n)
    # can return n-1 dates when `end` happens to land on a weekend (its
    # non-business-day roll-back interacts oddly with the periods count) -
    # padding by a few extra periods and slicing guarantees exactly n
    # regardless of what day `end` falls on.
    dates = pd.bdate_range(end=datetime.today(), periods=n + 5)[-n:]
    rets = rng.normal(trend, vol, n)
    close = start_price * np.cumprod(1 + rets)
    high = close * (1 + np.abs(rng.normal(0.006, 0.004, n)))
    low = close * (1 - np.abs(rng.normal(0.006, 0.004, n)))
    openp = close * (1 + rng.normal(0, 0.003, n))
    volume = np.clip(rng.normal(start_vol, start_vol * 0.15, n), 10000, None)
    if spike_last_day:
        close[-1] = close[-2] * 1.06
        high[-1] = close[-1] * 1.01
        volume[-1] = start_vol * 2.5
    return pd.DataFrame({"Open": openp, "High": high, "Low": low, "Close": close,
                          "Volume": volume}, index=dates)


def run_selftest() -> None:
    print("Running built-in self-test (synthetic data, no network calls)...\n")
    failures = []

    def check(name, cond):
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {name}")
        if not cond:
            failures.append(name)

    strong = _make_synthetic_ohlcv(trend=0.0025, vol=0.018, start_price=40, spike_last_day=True)
    weak = _make_synthetic_ohlcv(trend=-0.0010, vol=0.020, start_price=80, seed=7)
    spy = _make_synthetic_ohlcv(trend=0.0006, vol=0.009, start_price=450, seed=99)

    ind_strong = compute_indicators(strong)
    ind_weak = compute_indicators(weak)
    ind_spy = compute_indicators(spy)
    check("compute_indicators returns data for all synthetic series",
          all(x is not None for x in [ind_strong, ind_weak, ind_spy]))

    rs = compute_rs_ratings({"STRONG": ind_strong, "WEAK": ind_weak, "SPY": ind_spy})
    check("RS rating ranks strong uptrend above weak downtrend",
          rs.get("STRONG", 0) > rs.get("WEAK", 100))

    mini_strong, _ = score_minervini(ind_strong, rs.get("STRONG"))
    mini_weak, _ = score_minervini(ind_weak, rs.get("WEAK"))
    check("Minervini Trend Template scores strong stock higher than weak stock",
          mini_strong > mini_weak)
    check("Minervini Trend Template can hit a perfect 8/8 on an ideal uptrend", mini_strong == 8)

    status = market_health(ind_spy)
    check("market_health returns a non-empty string", isinstance(status, str) and len(status) > 0)

    intr = {
        "last_price": ind_strong["last_close"] * 1.06, "day_open": ind_strong["last_close"] * 1.001,
        "day_high": ind_strong["last_close"] * 1.065, "cum_volume": ind_strong["avg_vol50"] * 2.2,
        "pct_change": 6.0, "gap_pct": 0.5, "rel_volume": 2.2,
    }
    zscore, _ = score_zanger(ind_strong, intr)
    qscore, _ = score_qullamaggie(ind_strong, intr, rs_rating=92)
    check("Zanger scorer fires on a breakout+volume+big-day setup", zscore > 0)
    check("Qullamaggie scorer fires on strong RS + breakout setup", qscore > 0)

    fund = {"earningsQuarterlyGrowth": 0.35, "earningsGrowth": 0.28,
            "heldPercentInstitutions": 0.55, "trailingPE": 22.0, "pegRatio": 1.4,
            "profitMargins": 0.18, "debtToEquity": 40.0, "_rel_volume_ok": True}
    can_score, _ = score_canslim(fund, ind_strong, rs_rating=88)
    val_score, _ = score_value(fund)
    check("CANSLIM scorer awards most factors on strong synthetic fundamentals", can_score >= 4)
    check("Value scorer awards points on reasonable PEG/margins/debt", val_score > 0)

    mom, qual = composite_scores(mini_strong, can_score, zscore, qscore, val_score, Config())
    check("Momentum composite score is positive", mom > 0)
    check("Quality/value composite score is positive", qual > 0)
    check("Section-2 (quality) weighting excludes momentum triggers by construction",
          Config().w2_trend + Config().w2_canslim + Config().w2_value > 0)

    # --- %Chg must be measured against the PRIOR session's close ---------
    # Daily history whose last bar is "today" (what Yahoo returns during
    # and after the session), plus a 1-minute frame for that same day.
    session_day = pd.Timestamp(ind_strong["recent_bars"][-1][0])
    prior_close = ind_strong["recent_bars"][-2][1]
    today_close = ind_strong["recent_bars"][-1][1]
    idx = pd.date_range(session_day + pd.Timedelta(hours=9, minutes=30), periods=390, freq="min", tz=ET)
    intraday_df = pd.DataFrame({"Open": today_close, "High": today_close * 1.01, "Low": today_close * 0.99,
                                "Close": today_close, "Volume": 1000.0}, index=idx)
    after_close = datetime.combine(session_day.date(), datetime.min.time(), ET) + timedelta(hours=17)
    parsed = _parse_intraday_frame(intraday_df, ind_strong, now_et=after_close)
    expected = (today_close / prior_close - 1.0) * 100.0
    check("%Chg uses the prior session's close, not today's own daily bar",
          parsed is not None and abs(parsed["pct_change"] - expected) < 1e-6 and abs(expected) > 0.01)
    next_morning = after_close + timedelta(hours=16)   # 9:00 ET next day, pre-open
    check("Pre-open run treats yesterday's session as complete (no inflated RelVol)",
          _elapsed_session_fraction(session_day.date(), next_morning) == 1.0)

    # --- single-ticker MultiIndex frames are still found ------------------
    mi = pd.concat({"ONE": strong}, axis=1)
    check("_frame_for finds a ticker in a one-symbol MultiIndex download",
          _frame_for(mi, "ONE") is not None and _frame_for(mi, "TWO") is None)

    # --- publish gate ------------------------------------------------------
    good = {"universe": 900, "scored": 880, "benchmark_ok": True}
    thin = {"universe": 900, "scored": 120, "benchmark_ok": True}
    no_spy = {"universe": 900, "scored": 880, "benchmark_ok": False}
    check("Publish gate accepts a normal scan", publish_problem(good, Config()) is None)
    check("Publish gate rejects a mostly-empty (rate-limited) scan", publish_problem(thin, Config()) is not None)
    check("Publish gate rejects a scan with no SPY data", publish_problem(no_spy, Config()) is not None)

    # --- session dating (history backfill) -------------------------------
    sat = datetime(2026, 9, 5, 12, 0, tzinfo=ET)
    labor_day = datetime(2026, 9, 7, 17, 39, tzinfo=ET)
    tue_pre_open = datetime(2026, 9, 8, 8, 0, tzinfo=ET)
    check("Weekend and holiday scans map to the prior trading session",
          session_for_timestamp(sat).isoformat() == "2026-09-04"
          and session_for_timestamp(labor_day).isoformat() == "2026-09-04"
          and session_for_timestamp(tue_pre_open).isoformat() == "2026-09-04")
    check("A 4:20pm scan counts as post-close; a 2pm scan does not",
          is_post_close(datetime(2026, 10, 30, 16, 20, tzinfo=ET), datetime(2026, 10, 30).date())
          and not is_post_close(datetime(2026, 10, 30, 14, 0, tzinfo=ET), datetime(2026, 10, 30).date()))

    # --- history record keeping -------------------------------------------
    hist = {"days": []}
    upsert_history_day(hist, {"date": "2026-10-30", "final": False, "same_day_final": False, "picks": [{"t": "A"}]})
    upsert_history_day(hist, {"date": "2026-10-30", "final": True, "same_day_final": True, "picks": [{"t": "B"}]})
    kept = upsert_history_day(hist, {"date": "2026-10-30", "final": True, "same_day_final": False, "picks": [{"t": "C"}]})
    check("History keeps the same-day post-close scan; a weekend rerun cannot replace it",
          hist["days"][0]["picks"][0]["t"] == "B" and kept is False and len(hist["days"]) == 1)

    idx = pd.bdate_range("2026-09-01", periods=30)
    px = pd.Series([100.0 + i for i in range(30)], index=idx)
    spy_px = pd.Series([500.0] * 30, index=idx)
    d0 = idx[0].date().isoformat()
    hist = {"days": [{"date": d0, "picks": [{"t": "X", "r": 1, "sec": 1}]}]}
    compute_history_perf(hist, {"X": px, "SPY": spy_px}, "SPY", last_bar_complete=False)
    f = hist["days"][0]["picks"][0].get("f", {})
    check("Forward returns are close-to-close from the session the stock appeared",
          abs(f.get("5", 0) - 5.0) < 1e-9 and abs(f.get("20", 0) - 20.0) < 1e-9
          and abs(f.get("td", 0) - 29.0) < 1e-9 and hist["days"][0]["spy"]["20"] == 0.0)
    hist2 = {"days": [{"date": idx[-6].date().isoformat(), "picks": [{"t": "X", "r": 1, "sec": 1}]}]}
    compute_history_perf(hist2, {"X": px}, "SPY", last_bar_complete=False)
    check("A horizon ending on today's still-open bar is left blank",
          "5" not in hist2["days"][0]["picks"][0].get("f", {}) and "1" in hist2["days"][0]["picks"][0]["f"])

    hist3 = {"days": [
        {"date": "2026-10-28", "picks": [{"t": "A", "r": 3, "sec": 1}]},
        {"date": "2026-10-29", "picks": [{"t": "A", "r": 2, "sec": 1}, {"t": "B", "r": 1, "sec": 2}]},
        {"date": "2026-10-30", "picks": [{"t": "A", "r": 1, "sec": 1}, {"t": "C", "r": 4, "sec": 2}]},
    ]}
    ls = ticker_list_stats(hist3, "2026-10-30")
    check("List stats report streak, first session and yesterday's rank",
          ls["A"]["streak"] == 3 and ls["A"]["first_seen"] == "2026-10-28" and ls["A"]["prev_rank"] == 2
          and ls["C"]["streak"] == 1 and ls["C"]["prev_rank"] is None and "B" not in ls)

    # --- company profile + earnings -----------------------------------------
    long_text = "Acme Inc. makes rockets in the U.S. and abroad. " * 30
    s = trim_summary(long_text, 120)
    check("Business summaries are trimmed at a sentence end",
          len(s) <= 120 and s.endswith(".") and "Inc. makes" in s)
    today = datetime(2026, 10, 30).date()
    ts_next = datetime(2026, 11, 4, 16, 5, tzinfo=ET).timestamp()
    ts_prev = datetime(2026, 10, 27, 7, 0, tzinfo=ET).timestamp()
    e1 = earnings_info({"earningsTimestamp": ts_next, "isEarningsDateEstimate": True}, today)
    e2 = earnings_info({"earningsTimestamp": ts_prev}, today)
    check("Earnings dates resolve to days-until and days-since",
          e1 == {"next": "2026-11-04", "days": 5, "estimate": True} and e2 == {"last": "2026-10-27", "days_ago": 3})
    rich = {"c": {"Z": {"n": "Zed Corp", "d": "Zed makes things.", "s": "Technology"}}}
    merged = build_companies_payload({"Z": {"shortName": "Zed"}}, rich)
    check("A thin Yahoo response never erases a stored company description",
          merged["c"]["Z"].get("d") == "Zed makes things.")

    # --- per-stock detail file (static stock pages) ---------------------------
    spark_days = [pd.Timestamp(ts).date().isoformat() for ts in strong.index[-126:]]
    pool = pd.DataFrame([
        {"Ticker": "STRONG", "RelVol": 2.2, "CANSLIM": "5.0/6", "Zanger": "2/3", "Qullamaggie": "3/3",
         "Value": "2.0/2.5", "_CanNotes": ["C: qtr EPS growth 35%"], "_ZanNotes": [], "_QmNotes": ["ADR 4.1%"],
         "_ValNotes": [], "_Metrics": {"sma50": 61.2, "beta": None}, "_Earnings": {"next": "2026-11-20", "days": 9},
         "_Profile": None},
        {"Ticker": "NODATA", "RelVol": float("nan"), "CANSLIM": "0.0/6", "Zanger": "0/3", "Qullamaggie": "0/3",
         "Value": "0.0/2.5", "_CanNotes": [], "_ZanNotes": [], "_QmNotes": [], "_ValNotes": [],
         "_Metrics": {}, "_Earnings": None, "_Profile": None},
    ])
    det = build_stock_details(pool, {"STRONG": strong["Close"]}, spark_days,
                              datetime(2026, 11, 3, 16, 20, tzinfo=ET), "2026-11-03")
    s1, s2 = det["stocks"].get("STRONG", {}), det["stocks"].get("NODATA", {})
    check("stocks.json carries metrics, signals and a 6-month series for every scored stock",
          s1.get("m") == {"sma50": 61.2} and s1.get("sig", {}).get("qullamaggie") == ["ADR 4.1%"]
          and len(s1.get("sp", {}).get("c", [])) == len(spark_days) and s1.get("e", {}).get("days") == 9
          and "rv" not in s2 and "sp" not in s2 and "m" not in s2)
    json.dumps(det, default=_json_default)   # must serialize without NaN

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s) failed -> {failures}")
        sys.exit(1)
    print("ALL SELF-TESTS PASSED")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Full S&P 500 + S&P 400 momentum/value scanner - one manual run, prints a top-10 twice")
    p.add_argument("--selftest", action="store_true", help="Run built-in logic tests (no network) and exit")
    p.add_argument("--sp500-only", action="store_true",
                   help="Scan just the S&P 500 (skip the S&P 400 MidCap add-on) for a smaller/faster run")
    p.add_argument("--extra-tickers", type=str, default=None,
                   help="Comma-separated extra tickers to add on top of the universe, e.g. non-index small/mid caps")
    p.add_argument("--min-price", type=float, default=5.0)
    p.add_argument("--min-dollar-volume", type=float, default=5_000_000)
    p.add_argument("--strict-trend", action="store_true", help="Require ALL 8/8 Minervini criteria (default: 6/8)")
    p.add_argument("--min-trend-criteria", type=int, default=6, help="Minervini criteria required out of 8 (default 6)")
    p.add_argument("--fundamentals-ttl-hours", type=float, default=6.0,
                   help="How long cached fundamentals stay valid before re-fetching")
    p.add_argument("--json-out", type=str, default="docs/data/latest.json",
                   help="Path to write the dashboard's JSON data feed (overwritten every run). "
                        "Set to '' to skip.")
    p.add_argument("--min-coverage", type=float, default=0.6,
                   help="Refuse to publish (exit code 2, JSON left untouched) unless at least this "
                        "share of the universe was scored (default 0.6)")
    p.add_argument("--no-extras", action="store_true",
                   help="Write only latest.json (skip history.json, universe.json, companies.json)")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    if args.selftest:
        run_selftest()
        return

    cfg = Config(
        min_price=args.min_price,
        min_avg_dollar_volume=args.min_dollar_volume,
        strict_trend_template=args.strict_trend,
        min_trend_criteria=args.min_trend_criteria,
        fundamentals_ttl_hours=args.fundamentals_ttl_hours,
        include_sp400=not args.sp500_only,
        extra_tickers=[t.strip() for t in args.extra_tickers.split(",")] if args.extra_tickers else [],
        min_coverage=args.min_coverage,
    )

    universe_desc = "S&P 500 + S&P 400 MidCap (~900 tickers)" if cfg.include_sp400 else "S&P 500 only (~500 tickers)"
    print("Educational tool only - not financial advice. Data may be delayed ~15-20 minutes.")
    print(f"Universe: {universe_desc}. Pulling it fresh - this can take a few minutes.\n")

    if not is_market_open():
        LOG.info("Note: market is currently closed - intraday figures reflect the most recent session.")

    started = time.time()
    section1, section2, market_status, total, qualifying, stats, extras = run_scan(cfg)
    min_needed = 8 if cfg.strict_trend_template else cfg.min_trend_criteria
    print_results(section1, section2, market_status, total, qualifying, min_needed)
    save_results(section1, section2, cfg)
    LOG.info(f"Coverage: {stats['scored']}/{stats['universe']} scored, "
             f"{stats['with_history']} with daily history, {stats['with_intraday']} with intraday, "
             f"session {stats.get('session_date')}, {time.time() - started:.0f}s")

    problem = publish_problem(stats, cfg)
    if problem:
        # Non-zero exit fails the GitHub Actions run (so you get an email)
        # and the commit step never runs, so the site keeps showing the last
        # GOOD scan - with its real age - instead of a broken one.
        LOG.error(f"NOT publishing this scan: {problem}. Leaving {args.json_out or 'the data file'} unchanged.")
        sys.exit(2)

    if args.json_out:
        publish(section1, section2, market_status, total, qualifying, min_needed, stats, extras,
                args.json_out, write_extras=not args.no_extras)


def publish(section1: pd.DataFrame, section2: pd.DataFrame, market_status: str, total: int,
            qualifying: int, min_needed: int, stats: dict, extras: dict, json_out: str,
            write_extras: bool = True, generated_at: Optional[datetime] = None) -> None:
    """Writes latest.json (the core screen) plus the site extras next to it:
    history.json, universe.json and companies.json. The extras are strictly
    best-effort - any failure is logged and the core screen still publishes."""
    generated_at = generated_at or datetime.now(ET)
    data_dir = os.path.dirname(json_out) or "."
    list_stats: Dict[str, dict] = {}
    history = None

    if write_extras:
        try:
            rows = (_rows_for_json(section1, "MomentumScore"), _rows_for_json(section2, "QualityScore"))
            history, changed = update_history(
                os.path.join(data_dir, "history.json"), stats.get("session_date"), generated_at, rows,
                market_status, total, qualifying, stats, extras.get("closes", {}),
                extras.get("benchmark", "SPY"), extras.get("fundamentals", {}))
            if stats.get("session_date"):
                list_stats = ticker_list_stats(history, stats["session_date"])
            LOG.info(f"History: {len(history.get('days', []))} sessions"
                     + ("" if changed else " (kept the existing final record for this session)"))
        except Exception as e:
            history = None
            LOG.warning(f"History update skipped: {e}")

    export_json(section1, section2, market_status, total, qualifying, min_needed, json_out, stats,
                generated_at=generated_at, list_stats=list_stats)

    if not write_extras:
        return
    if history is not None:
        try:
            write_json_atomic(os.path.join(data_dir, "history.json"), history)
        except Exception as e:
            LOG.warning(f"Could not write history.json: {e}")
    if extras.get("pool") is not None:
        try:
            write_json_atomic(os.path.join(data_dir, "universe.json"),
                              build_universe_payload(extras["pool"], generated_at, stats.get("session_date")))
        except Exception as e:
            LOG.warning(f"Could not write universe.json: {e}")
        try:
            write_json_atomic(os.path.join(data_dir, "stocks.json"),
                              build_stock_details(extras["pool"], extras.get("closes", {}),
                                                  stats.get("spark_dates") or [], generated_at,
                                                  stats.get("session_date")))
        except Exception as e:
            LOG.warning(f"Could not write stocks.json: {e}")
    if extras.get("fundamentals"):
        try:
            cpath = os.path.join(data_dir, "companies.json")
            existing = None
            if os.path.exists(cpath):
                with open(cpath) as f:
                    existing = json.load(f)
            new = build_companies_payload(extras["fundamentals"], existing)
            # Only rewrite when something changed, so the repo isn't bloated
            # by a fresh copy of a ~400 KB file every half hour.
            if new != existing:
                write_json_atomic(cpath, new)
                LOG.info(f"Wrote company profiles -> {cpath} ({len(new['c'])} companies)")
        except Exception as e:
            LOG.warning(f"Could not write companies.json: {e}")


if __name__ == "__main__":
    main()

