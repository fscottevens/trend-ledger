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
    max_workers_fundamentals: int = 16

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

def _batched_daily_download(tickers: List[str], cfg: Config) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    all_failed: List[str] = []
    for i in range(0, len(tickers), cfg.batch_size):
        chunk = tickers[i:i + cfg.batch_size]
        LOG.info(f"Daily bars: {i + 1}-{i + len(chunk)} / {len(tickers)}")
        try:
            data = yf.download(
                tickers=chunk, period=cfg.lookback_period, interval="1d",
                group_by="ticker", threads=True, progress=False, auto_adjust=False,
            )
        except Exception as e:
            LOG.warning(f"Daily batch download failed at offset {i}: {e}")
            all_failed.extend(chunk)
            continue

        if len(chunk) == 1:
            t = chunk[0]
            df = data.dropna(how="all")
            if not df.empty:
                out[t] = df
            else:
                all_failed.append(t)
            continue

        for t in chunk:
            try:
                df = data[t].dropna(how="all")
                if not df.empty and len(df) > 60:
                    out[t] = df
                else:
                    all_failed.append(t)
            except Exception:
                all_failed.append(t)

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

    return {
        "last_close": last_close, "sma50": float(sma50.iloc[-1]),
        "sma150": float(sma150.iloc[-1]), "sma200": sma200_now,
        "sma200_1mo_ago": sma200_1mo_ago, "wk52_high": wk52_high, "wk52_low": wk52_low,
        "adr20_pct": adr20, "avg_vol50": avg_vol50, "avg_dollar_vol20": avg_dollar_vol20,
        "ret_3m": ret_3m, "ret_6m": ret_6m, "ret_9m": ret_9m, "ret_12m": ret_12m,
        "tightness_ratio": tightness_ratio,
        "prior_20d_high": prior_20d_high, "prior_50d_high": prior_50d_high,
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

def _elapsed_session_fraction() -> float:
    now_et = datetime.now(ET)
    session_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed_min = max((now_et - session_open).total_seconds() / 60.0, 1.0)
    return min(elapsed_min / 390.0, 1.0)


def _parse_intraday_frame(df: pd.DataFrame, ind: dict, elapsed_frac: float) -> Optional[dict]:
    if df is None or df.empty:
        return None
    day_open = float(df["Open"].iloc[0])
    day_high = float(df["High"].max())
    last_price = float(df["Close"].iloc[-1])
    cum_volume = float(df["Volume"].sum())

    prev_close = ind.get("last_close")
    avg_vol50 = ind.get("avg_vol50")

    pct_change = ((last_price / prev_close) - 1.0) * 100.0 if prev_close else None
    gap_pct = ((day_open / prev_close) - 1.0) * 100.0 if prev_close else None
    expected_vol_by_now = avg_vol50 * elapsed_frac if avg_vol50 else None
    rel_volume = (cum_volume / expected_vol_by_now) if expected_vol_by_now else None

    return {
        "last_price": last_price, "day_open": day_open, "day_high": day_high,
        "cum_volume": cum_volume, "pct_change": pct_change, "gap_pct": gap_pct,
        "rel_volume": rel_volume,
    }


def fetch_intraday(tickers: List[str], daily_ind: Dict[str, dict], cfg: Config) -> Dict[str, dict]:
    """Batched, 'up to the minute' (per Yahoo's delay) intraday pull for the
    full candidate list - not just a pre-filtered subset."""
    out: Dict[str, dict] = {}
    if not tickers:
        return out
    elapsed_frac = _elapsed_session_fraction()
    failed: List[str] = []

    for i in range(0, len(tickers), cfg.batch_size):
        chunk = tickers[i:i + cfg.batch_size]
        LOG.info(f"Intraday quotes: {i + 1}-{i + len(chunk)} / {len(tickers)}")
        try:
            data = yf.download(
                tickers=chunk, period="1d", interval="1m",
                group_by="ticker", threads=True, progress=False, auto_adjust=False,
            )
        except Exception as e:
            LOG.warning(f"Intraday batch download failed at offset {i}: {e}")
            failed.extend(chunk)
            continue

        for t in chunk:
            try:
                df = data[t].dropna(how="all") if len(chunk) > 1 else data.dropna(how="all")
                ind = daily_ind.get(t)
                if ind is None:
                    continue
                parsed = _parse_intraday_frame(df, ind, elapsed_frac)
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

class FundamentalsCache:
    def __init__(self, cfg: Config):
        self.path = cfg.fundamentals_cache_path
        self.ttl = timedelta(hours=cfg.fundamentals_ttl_hours)
        self.max_workers = cfg.max_workers_fundamentals
        self._data: Dict[str, Tuple[datetime, dict]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r") as f:
                raw = json.load(f)
            for t, entry in raw.items():
                ts = datetime.fromisoformat(entry["ts"])
                self._data[t] = (ts, entry["data"])
            LOG.info(f"Loaded fundamentals cache: {len(self._data)} tickers")
        except Exception as e:
            LOG.warning(f"Could not load fundamentals cache ({e}); starting fresh")

    def _save(self) -> None:
        try:
            raw = {t: {"ts": ts.isoformat(), "data": d} for t, (ts, d) in self._data.items()}
            with open(self.path, "w") as f:
                json.dump(raw, f, default=str)
        except Exception as e:
            LOG.warning(f"Could not save fundamentals cache: {e}")

    def get_many(self, tickers: List[str]) -> Dict[str, dict]:
        now = datetime.now()
        need = [t for t in tickers
                if t not in self._data or now - self._data[t][0] > self.ttl]

        if need:
            LOG.info(f"Refreshing fundamentals for {len(need)} tickers "
                      f"({self.max_workers} parallel workers)...")

            def _fetch_one(t):
                try:
                    return t, (yf.Ticker(t).get_info() or {})
                except Exception as e:
                    LOG.debug(f"fundamentals failed for {t}: {e}")
                    return t, {}

            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futures = [ex.submit(_fetch_one, t) for t in need]
                done = 0
                for fut in as_completed(futures):
                    t, info = fut.result()
                    self._data[t] = (now, info)
                    done += 1
                    if done % 50 == 0:
                        LOG.info(f"  fundamentals progress: {done}/{len(need)}")
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
    peg = fund.get("pegRatio")
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


def run_scan(cfg: Config) -> Tuple[pd.DataFrame, pd.DataFrame, str, int, int]:
    """Runs one full pass over the entire universe. Every candidate that
    clears the basic price/liquidity filter gets scored - none are
    dropped for failing the Minervini gate. Instead each row is flagged
    Qualifies=Yes/No, so if fewer than 20 names fully qualify (or none do)
    you still get a ranked top-20 of the CLOSEST candidates rather than
    an empty result. Returns
    (section1_df, section2_df, market_status, total_scored, qualifying_count)."""
    universe = get_universe(cfg)
    LOG.info(f"Universe size: {len(universe)} tickers (+ benchmark {cfg.benchmark})")

    daily_hist = fetch_daily_history(universe, cfg)
    indicators: Dict[str, dict] = {}
    for t, df in daily_hist.items():
        ind = compute_indicators(df)
        if ind is not None:
            indicators[t] = ind
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

        rows.append({
            "Ticker": t,
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
                "peg_ratio": _num(fund.get("pegRatio")),
                "profit_margin_pct": _pct(fund.get("profitMargins")),
                "debt_to_equity": _num(fund.get("debtToEquity")),
                "eps_qtr_growth_pct": _pct(fund.get("earningsQuarterlyGrowth")),
                "eps_ann_growth_pct": _pct(fund.get("earningsGrowth")),
                "institutional_pct": _pct(fund.get("heldPercentInstitutions")),
            },
        })

    if not rows:
        empty = pd.DataFrame()
        return empty, empty, market_status, 0, 0

    pool = pd.DataFrame(rows)
    qualifying_count = int((pool["Qualifies"] == "Yes").sum())

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

    # _TrendN was only needed for sorting; drop it now. The other hidden
    # _Xxx columns are kept - export_json still needs to read them - and
    # are stripped later, right before console printing / CSV export.
    section1 = section1.drop(columns=["_TrendN"]).reset_index(drop=True)
    section2 = section2.drop(columns=["_TrendN"]).reset_index(drop=True)

    return (section1, section2,
            market_status, len(pool), qualifying_count)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def _print_table(df: pd.DataFrame, score_col: str, drop_cols: List[str]) -> None:
    display_df = df.drop(columns=[c for c in drop_cols if c in df.columns])
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
                      drop_cols=["Why", "QualityScore", "_MiniCriteria", "_CanNotes", "_ZanNotes", "_QmNotes", "_ValNotes", "_Metrics"])

    print(f"\n--- SECTION 2: Fundamentals & Trend-Quality Picks "
          f"(value-weighted, Minervini-heaviest) ---")
    if section2.empty:
        print("No candidates this run (universe returned no usable data).")
    else:
        _print_table(section2, "QualityScore",
                      drop_cols=["Why", "MomentumScore", "%Chg", "RelVol", "Zanger", "Qullamaggie",
                                 "_MiniCriteria", "_CanNotes", "_ZanNotes", "_QmNotes", "_ValNotes", "_Metrics"])


def save_results(section1: pd.DataFrame, section2: pd.DataFrame, cfg: Config) -> None:
    if section1.empty and section2.empty:
        return
    os.makedirs(cfg.output_dir, exist_ok=True)
    fname = os.path.join(cfg.output_dir, f"scan_{datetime.now(ET).strftime('%Y%m%d_%H%M%S')}.csv")
    hidden_cols = ["_MiniCriteria", "_CanNotes", "_ZanNotes", "_QmNotes", "_ValNotes", "_Metrics"]
    s1 = section1.drop(columns=[c for c in hidden_cols if c in section1.columns]).copy()
    s1.insert(0, "Section", "1_Momentum")
    s2 = section2.drop(columns=[c for c in hidden_cols if c in section2.columns]).copy()
    s2.insert(0, "Section", "2_ValueQuality")
    combined = pd.concat([s1, s2], ignore_index=True)
    combined.insert(0, "timestamp", datetime.now(ET).isoformat())
    combined.to_csv(fname, index=False)
    LOG.info(f"Saved results -> {fname}")


def _rows_for_json(df: pd.DataFrame, score_col: str) -> List[dict]:
    if df.empty:
        return []
    out = []
    for _, row in df.iterrows():
        trend_n = int(str(row["Trend"]).split("/")[0])
        out.append({
            "ticker": row["Ticker"],
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
        })
    return out



def export_json(section1: pd.DataFrame, section2: pd.DataFrame, market_status: str,
                 total_scored: int, qualifying_count: int, min_needed: int, path: str) -> None:
    """Writes the dashboard's data feed - overwrites the SAME file every run
    (not timestamped) so the website always reads the latest scan."""
    payload = {
        "generated_at": datetime.now(ET).isoformat(),
        "market_status": market_status,
        "total_scored": total_scored,
        "qualifying_count": qualifying_count,
        "min_trend_needed": min_needed,
        "section1": _rows_for_json(section1, "MomentumScore"),
        "section2": _rows_for_json(section2, "QualityScore"),
    }
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    LOG.info(f"Wrote dashboard data -> {path}")


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
    )

    universe_desc = "S&P 500 + S&P 400 MidCap (~900 tickers)" if cfg.include_sp400 else "S&P 500 only (~500 tickers)"
    print("Educational tool only - not financial advice. Data may be delayed ~15-20 minutes.")
    print(f"Universe: {universe_desc}. Pulling it fresh - this can take a few minutes.\n")

    if not is_market_open():
        LOG.info("Note: market is currently closed - intraday figures reflect the most recent session.")

    section1, section2, market_status, total, qualifying = run_scan(cfg)
    min_needed = 8 if cfg.strict_trend_template else cfg.min_trend_criteria
    print_results(section1, section2, market_status, total, qualifying, min_needed)
    save_results(section1, section2, cfg)
    if args.json_out:
        export_json(section1, section2, market_status, total, qualifying, min_needed, args.json_out)


if __name__ == "__main__":
    main()

