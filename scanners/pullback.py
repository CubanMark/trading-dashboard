from typing import Optional

import pandas as pd

from compute.indicators import add_sma, add_atr, add_adr, add_momentum, is_uptrend

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MA = 20
_DEPTH_PCT    = 0.03   # close within ±3% of SMA20 (MA20 variant)
_DEPTH_MA10   = 0.02   # close within ±2% of SMA10 (MA10 variant)
_MIN_PRICE    = 5.0
_MIN_AVG_VOL  = 500_000  # shares

_EXCLUDED_SUB_INDUSTRIES = frozenset({
    "Biotechnology",
    "Pharmaceuticals",
    "Health Care Services",
    "Consumer Staples Merchandise Retail",
    "Diversified Banks",
    "Property & Casualty Insurance",
})

_WARNING = "Research scanner only; not a validated tradable edge."

_SCANNER_LABELS = {
    "pullback_ma20": "MA20 Pullback",
    "pullback_ma10": "MA10 Pullback",
    "pullback_3d":   "3D Pullback",
}


# ---------------------------------------------------------------------------
# Regime check
# ---------------------------------------------------------------------------

def _spy_uptrend(spy_close: pd.Series) -> bool:
    """True if SPY close is above its 200-day moving average. Defaults True on insufficient data."""
    if spy_close is None or len(spy_close) < 201:
        return True
    sma200 = spy_close.rolling(200).mean().iloc[-1]
    return float(spy_close.iloc[-1]) > float(sma200)


# ---------------------------------------------------------------------------
# Individual scan functions
# ---------------------------------------------------------------------------

def scan_ma20(df: pd.DataFrame, date: str) -> bool:
    """Pullback to MA20: close within ±3% of SMA20, in uptrend, price/vol filters."""
    if date not in df.index:
        return False
    df = _prepare(df)
    row = df.loc[date]
    if pd.isna(row.get(f"sma{_MA}")) or pd.isna(row.get("atr")):
        return False
    if not row.get("uptrend", False):
        return False
    if row["close"] < _MIN_PRICE:
        return False
    if df["volume"].rolling(20).mean().loc[date] < _MIN_AVG_VOL:
        return False
    sma = row[f"sma{_MA}"]
    return abs(row["close"] - sma) / sma <= _DEPTH_PCT


def scan_ma10(df: pd.DataFrame, date: str) -> bool:
    """Pullback to MA10: close within ±2% of SMA10, uptrend, recently pulled back."""
    if date not in df.index:
        return False
    df = _prepare(df)
    row = df.loc[date]
    if pd.isna(row.get("sma10")) or pd.isna(row.get("sma50")) or pd.isna(row.get("sma200")):
        return False
    if not (row["close"] > row["sma50"] > row["sma200"]):
        return False
    if row["close"] < _MIN_PRICE:
        return False
    if df["volume"].rolling(20).mean().loc[date] < _MIN_AVG_VOL:
        return False
    sma10 = row["sma10"]
    if abs(row["close"] - sma10) / sma10 > _DEPTH_MA10:
        return False
    # Not at a new 10-day high — must have pulled back at least a little
    idx = df.index.get_loc(date)
    if idx < 10:
        return False
    recent_high = df["close"].iloc[max(0, idx - 10):idx].max()
    return row["close"] < recent_high * 0.995


def scan_3d(df: pd.DataFrame, date: str) -> bool:
    """3 consecutive lower closes, in uptrend, price/vol filters."""
    if date not in df.index:
        return False
    df = _prepare(df)
    idx = df.index.get_loc(date)
    if idx < 3:
        return False
    row = df.loc[date]
    if not row.get("uptrend", False):
        return False
    if row["close"] < _MIN_PRICE:
        return False
    if df["volume"].rolling(20).mean().loc[date] < _MIN_AVG_VOL:
        return False
    closes = df["close"].iloc[idx - 2: idx + 1].values  # [day-2, day-1, today]
    return float(closes[2]) < float(closes[1]) < float(closes[0])


# Alias for backwards compatibility
scan = scan_ma20


# ---------------------------------------------------------------------------
# Universe scan
# ---------------------------------------------------------------------------

def scan_universe(
    prices: dict[str, pd.DataFrame],
    date: str,
    spy_close: Optional[pd.Series] = None,
    meta_map: Optional[dict] = None,
) -> dict:
    """Run all 3 pullback variants across the universe.

    Returns:
        {
            "regime": "bull" | "bear",
            "hits": {
                "pullback_ma20": list[str],
                "pullback_ma10": list[str],
                "pullback_3d":   list[str],
            },
        }
    Bear regime is detected when spy_close is provided and SPY < SMA200.
    Tickers in _EXCLUDED_SUB_INDUSTRIES are skipped when meta_map is provided.
    """
    regime = "bull" if _spy_uptrend(spy_close) else "bear"
    if regime == "bear":
        return {"regime": "bear", "hits": {}}

    hits_ma20: list[str] = []
    hits_ma10: list[str] = []
    hits_3d:   list[str] = []

    for ticker, df in prices.items():
        if meta_map:
            sub_ind = meta_map.get(ticker, {}).get("gics_sub_industry") or ""
            if sub_ind in _EXCLUDED_SUB_INDUSTRIES:
                continue
        if scan_ma20(df, date):
            hits_ma20.append(ticker)
        if scan_ma10(df, date):
            hits_ma10.append(ticker)
        if scan_3d(df, date):
            hits_3d.append(ticker)

    return {
        "regime": "bull",
        "hits": {
            "pullback_ma20": hits_ma20,
            "pullback_ma10": hits_ma10,
            "pullback_3d":   hits_3d,
        },
    }


# ---------------------------------------------------------------------------
# Hit row builder
# ---------------------------------------------------------------------------

def build_hit_row(ticker: str, df: pd.DataFrame, date: str, meta: dict) -> dict:
    """Build a scanner_hits row dict for one confirmed hit."""
    df = _prepare(df)
    row = df.loc[date]
    high_52w = df["high"].rolling(252).max().loc[date]
    avg_vol   = df["volume"].rolling(20).mean().loc[date]
    return {
        "date":          date,
        "ticker":        ticker,
        "scanner":       meta.get("scanner", "pullback_ma20"),
        "scanner_label": meta.get("scanner_label", "MA20 Pullback"),
        "gics_sector":   meta.get("gics_sector"),
        "gics_industry": meta.get("gics_industry"),
        "rs_rank":       meta.get("rs_rank"),
        "perf_1m":       row.get("mom21d"),
        "adr_pct":       row.get("adr_pct"),
        "atr":           row.get("atr"),
        "avg_volume":    float(avg_vol) if pd.notna(avg_vol) else None,
        "dist_52w_high": (row["close"] / high_52w - 1) * 100 if pd.notna(high_52w) and high_52w else None,
        "earnings_date": meta.get("earnings_date"),
        "also_in":       "",  # populated by _annotate_overlaps()
        "warning":       meta.get("warning", _WARNING),
    }


# ---------------------------------------------------------------------------
# Overlap annotation
# ---------------------------------------------------------------------------

def _annotate_overlaps(hit_rows: list[dict]) -> list[dict]:
    """Set 'also_in' field on each row: other scanner variants that triggered for the same ticker."""
    ticker_scanners: dict[str, list[str]] = {}
    for row in hit_rows:
        t = row["ticker"]
        if t not in ticker_scanners:
            ticker_scanners[t] = []
        ticker_scanners[t].append(row["scanner"])

    for row in hit_rows:
        t      = row["ticker"]
        label  = _SCANNER_LABELS.get(row["scanner"], row["scanner"])
        others = [
            _SCANNER_LABELS.get(s, s)
            for s in ticker_scanners[t]
            if s != row["scanner"]
        ]
        row["also_in"] = ", ".join(sorted(others))

    return hit_rows


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df = add_sma(df, [10, _MA, 50, 200])
    df = add_atr(df)
    df = add_adr(df)
    df = add_momentum(df, [21, 63])
    df["uptrend"] = is_uptrend(df)
    return df
