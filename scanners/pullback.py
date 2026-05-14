import pandas as pd

from compute.indicators import add_sma, add_atr, add_adr, add_momentum, is_uptrend

# Provisional rules — marked [~] in PROJECT_BRIEF.md section 10.
# Source: Swing Lab legacy_quality_filter.py. Not final; revisit after Swing Lab matures.
_MA = 20
_DEPTH_PCT = 0.03   # close within ±3% of SMA20
_MIN_PRICE = 5.0
_MIN_AVG_VOL = 500_000  # shares


def scan(df: pd.DataFrame, date: str) -> bool:
    """
    Returns True if the ticker shows a pullback-to-MA20 setup on `date`.
    `df` must contain at least 220 rows of OHLCV ending on or after `date`.
    """
    if date not in df.index:
        return False

    df = _prepare(df)
    row = df.loc[date]

    if pd.isna(row.get(f"sma{_MA}")) or pd.isna(row.get("atr")):
        return False

    # Trend filter
    if not row.get("uptrend", False):
        return False

    # Price + volume filter
    if row["close"] < _MIN_PRICE:
        return False
    if df["volume"].rolling(20).mean().loc[date] < _MIN_AVG_VOL:
        return False

    # Pullback depth: close within ±3% of SMA20
    sma = row[f"sma{_MA}"]
    dist = abs(row["close"] - sma) / sma
    return dist <= _DEPTH_PCT


def scan_universe(
    prices: dict[str, pd.DataFrame],
    date: str,
) -> list[str]:
    """Return list of tickers that pass the pullback scan on `date`."""
    return [ticker for ticker, df in prices.items() if scan(df, date)]


def build_hit_row(ticker: str, df: pd.DataFrame, date: str, meta: dict) -> dict:
    """Build a scanner_hits row dict for one confirmed hit."""
    df = _prepare(df)
    row = df.loc[date]
    high_52w = df["high"].rolling(252).max().loc[date]
    avg_vol   = df["volume"].rolling(20).mean().loc[date]
    return {
        "date":          date,
        "ticker":        ticker,
        "scanner":       "pullback_ma20",
        "gics_sector":   meta.get("gics_sector"),
        "gics_industry": meta.get("gics_industry"),
        "rs_rank":       meta.get("rs_rank"),
        "perf_1m":       row.get("mom21d"),
        "adr_pct":       row.get("adr_pct"),
        "atr":           row.get("atr"),
        "avg_volume":    avg_vol,
        "dist_52w_high": (row["close"] / high_52w - 1) * 100 if high_52w else None,
        "earnings_date": meta.get("earnings_date"),
    }


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df = add_sma(df, [_MA, 50, 200])
    df = add_atr(df)
    df = add_adr(df)
    df = add_momentum(df, [21, 63])
    df["uptrend"] = is_uptrend(df)
    return df
