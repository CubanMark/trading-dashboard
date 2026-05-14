"""
Bulk initial load and daily incremental updates.

Flow:
  run_update(conn)
    ├─ tickers with no stored prices   → _fetch_prices(bulk_start = 5 years ago)
    ├─ tickers with stale prices       → _fetch_prices(start = 7 days ago)
    └─ macro/FRED                      → fetch_macro(start)
"""

import logging
import time
from datetime import date, timedelta
from typing import Optional
import sqlite3

import pandas as pd
import yfinance as yf

from data import universe as uni
from data import fred_client

logger = logging.getLogger(__name__)

BULK_YEARS   = 5
BATCH_SIZE   = 100
BATCH_DELAY  = 2.0   # seconds between batches (yfinance rate limit courtesy)
STALE_DAYS   = 3     # re-fetch if last stored date is older than this


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_update(conn: sqlite3.Connection) -> None:
    """Main entry point. Safe to re-run (INSERT OR IGNORE throughout)."""
    all_tickers = uni.tickers(conn)
    if not all_tickers:
        logger.error("Universe is empty — call universe.seed_from_csv() first")
        return

    stored = _last_stored_dates(conn)
    today  = date.today()
    cutoff = str(today - timedelta(days=STALE_DAYS))

    bulk_tickers = [t for t in all_tickers if t not in stored]
    incr_tickers = [t for t in all_tickers if stored.get(t, "") < cutoff and t not in bulk_tickers]
    skip_count   = len(all_tickers) - len(bulk_tickers) - len(incr_tickers)

    logger.info(
        "Tickers — bulk: %d  incremental: %d  up-to-date: %d",
        len(bulk_tickers), len(incr_tickers), skip_count,
    )

    if bulk_tickers:
        bulk_start = str(today - timedelta(days=BULK_YEARS * 365))
        logger.info("Bulk load from %s for %d tickers", bulk_start, len(bulk_tickers))
        _fetch_prices(conn, bulk_tickers, bulk_start)

    if incr_tickers:
        incr_start = str(today - timedelta(days=7))
        logger.info("Incremental update from %s for %d tickers", incr_start, len(incr_tickers))
        _fetch_prices(conn, incr_tickers, incr_start)

    # Macro: full load on first run, else last 7 days
    macro_empty = conn.execute("SELECT COUNT(*) FROM macro_series").fetchone()[0] == 0
    macro_start = (
        str(today - timedelta(days=BULK_YEARS * 365)) if macro_empty
        else str(today - timedelta(days=7))
    )
    fetch_macro(conn, macro_start)


def fetch_macro(conn: sqlite3.Connection, start: str) -> None:
    """Fetch macro + sector ETFs via yfinance, and FRED series."""
    yf_tickers = uni.MACRO_TICKERS + uni.SECTOR_ETFS
    logger.info("Fetching %d macro/sector tickers (yfinance)", len(yf_tickers))

    try:
        raw = yf.download(
            yf_tickers,
            start=start,
            auto_adjust=False,
            group_by="ticker",
            progress=False,
            threads=True,
        )
        rows = []
        for ticker in yf_tickers:
            df = _extract_from_batch(raw, ticker, len(yf_tickers))
            if df is None or df.empty:
                logger.warning("Macro %s: no data", ticker)
                continue
            for dt, row in df.iterrows():
                if pd.notna(row.get("close")):
                    rows.append((ticker, str(dt.date()), float(row["close"])))

        conn.executemany(
            "INSERT OR IGNORE INTO macro_series (series_id, date, value) VALUES (?,?,?)",
            rows,
        )
        conn.commit()
        logger.info("Stored %d macro rows", len(rows))
    except Exception as exc:
        logger.error("Macro yfinance fetch failed: %s", exc)

    # FRED (optional — skipped if FRED_API_KEY not set)
    try:
        fred_data = fred_client.fetch_all(start=start)
        fred_rows = []
        for name, series in fred_data.items():
            sid = fred_client.SERIES[name]
            for dt, val in series.items():
                fred_rows.append((sid, str(dt.date()), float(val)))
        if fred_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO macro_series (series_id, date, value) VALUES (?,?,?)",
                fred_rows,
            )
            conn.commit()
            logger.info("Stored %d FRED rows", len(fred_rows))
    except Exception as exc:
        logger.warning("FRED fetch skipped: %s", exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_prices(
    conn: sqlite3.Connection,
    tickers: list[str],
    start: str,
) -> None:
    batches = [tickers[i : i + BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
    for i, batch in enumerate(batches, 1):
        logger.info("Batch %d/%d (%d tickers)", i, len(batches), len(batch))
        try:
            _download_and_store(conn, batch, start)
        except Exception as exc:
            logger.error("Batch %d failed: %s", i, exc)
        if i < len(batches):
            time.sleep(BATCH_DELAY)


def _download_and_store(
    conn: sqlite3.Connection,
    tickers: list[str],
    start: str,
) -> None:
    raw = yf.download(
        tickers,
        start=start,
        auto_adjust=False,
        actions=False,
        group_by="ticker",
        progress=False,
        threads=True,
    )
    if raw is None or raw.empty:
        logger.warning("Empty response for batch starting with %s", tickers[:3])
        return

    total_rows = 0
    for ticker in tickers:
        df = _extract_from_batch(raw, ticker, len(tickers))
        if df is None or df.empty:
            logger.warning("%s: no data in batch response", ticker)
            continue

        for w in _sanity_check(df, ticker):
            logger.warning(w)

        rows = [
            (ticker, str(dt.date()), row["open"], row["high"], row["low"], row["close"], int(row["volume"]))
            for dt, row in df.iterrows()
            if pd.notna(row.get("close")) and row.get("close", 0) > 0
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO prices (ticker, date, open, high, low, close, volume)"
            " VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        total_rows += len(rows)

    conn.commit()
    logger.debug("Stored %d price rows for batch", total_rows)


def _extract_from_batch(
    raw: pd.DataFrame,
    ticker: str,
    n_tickers: int,
) -> Optional[pd.DataFrame]:
    """Extract one ticker's OHLCV from a yf.download() result (handles single + multi)."""
    try:
        if n_tickers == 1:
            df = raw.copy()
            # Single-ticker download may still return MultiIndex in some yfinance versions
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [col[0].lower() for col in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]
        else:
            # group_by="ticker" → level 0 is ticker name
            if ticker not in raw.columns.get_level_values(0):
                return None
            df = raw[ticker].copy()
            df.columns = [c.lower() for c in df.columns]

        df.index = pd.to_datetime(df.index).tz_localize(None)
        df.index.name = "date"
        keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        if not keep:
            return None
        return df[keep].dropna(subset=["close"])

    except Exception as exc:
        logger.debug("Extract failed for %s: %s", ticker, exc)
        return None


def load_price_dfs(
    conn: sqlite3.Connection,
    tickers: list[str],
    rows: int = 400,
) -> dict[str, pd.DataFrame]:
    """
    Load the most recent `rows` OHLCV rows per ticker from the prices table.
    Returns dict: ticker → DataFrame(string date index, cols=[open,high,low,close,volume]).
    Batches the IN clause to stay within SQLite variable limits.
    """
    if not tickers:
        return {}

    result: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), 900):
        batch = tickers[i : i + 900]
        placeholders = ",".join("?" * len(batch))
        chunk = pd.read_sql(
            f"""
            SELECT ticker, date, open, high, low, close, volume
            FROM (
                SELECT ticker, date, open, high, low, close, volume,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
                FROM prices
                WHERE ticker IN ({placeholders})
            )
            WHERE rn <= {rows}
            ORDER BY ticker, date
            """,
            conn,
            params=batch,
        )
        if chunk.empty:
            continue
        for ticker, grp in chunk.groupby("ticker"):
            result[ticker] = grp.drop(columns="ticker").set_index("date").sort_index()

    return result


def _last_stored_dates(conn: sqlite3.Connection) -> dict[str, str]:
    df = pd.read_sql(
        "SELECT ticker, MAX(date) AS last_date FROM prices GROUP BY ticker", conn
    )
    return dict(zip(df["ticker"], df["last_date"]))


def _sanity_check(df: pd.DataFrame, ticker: str) -> list[str]:
    """Return warning strings for data quality issues. Never modifies df."""
    warnings = []
    n = len(df)
    if n == 0:
        return [f"{ticker}: empty DataFrame"]

    nan_pct = df["close"].isna().mean() * 100
    if nan_pct > 1:
        warnings.append(f"{ticker}: {nan_pct:.1f}% NaN close values")

    if (df["close"] <= 0).any():
        warnings.append(f"{ticker}: {(df['close'] <= 0).sum()} rows with close ≤ 0")

    if "high" in df.columns and "low" in df.columns:
        bad_hl = (df["high"] < df["low"]).sum()
        if bad_hl:
            warnings.append(f"{ticker}: {bad_hl} rows where high < low")

    # Day-over-day moves > 50% may be splits not yet in corporate_actions
    big_moves = df["close"].pct_change().abs() > 0.5
    if big_moves.any():
        dates = df.index[big_moves].strftime("%Y-%m-%d").tolist()
        warnings.append(
            f"{ticker}: {big_moves.sum()} day(s) with >50% move — "
            f"possible unadjusted split: {dates[:3]}"
        )

    return warnings
