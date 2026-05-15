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

import numpy as np
import pandas as pd
import yfinance as yf

from data import universe as uni
from data import fred_client
from data.quality import log_quality

logger = logging.getLogger(__name__)

BULK_YEARS   = 5
BATCH_SIZE   = 100
BATCH_DELAY  = 2.0   # seconds between batches (yfinance rate limit courtesy)
STALE_DAYS   = 1     # re-fetch if last stored date is older than yesterday


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

        # Mock fallback for tickers still without any stored data
        stored_after = set(_last_stored_dates(conn).keys())
        still_missing = [t for t in bulk_tickers if t not in stored_after]
        if still_missing:
            logger.warning(
                "Mock fallback for %d tickers without yfinance data: %s ...",
                len(still_missing), still_missing[:5],
            )
            _store_mock_prices(conn, _mock_prices_for_tickers(still_missing))

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

    # --- Persistent DQ checks ---
    today_str  = str(today)
    thirty_ago = str(today - timedelta(days=30))

    n_today = conn.execute(
        "SELECT COUNT(*) FROM prices WHERE date=?", (today_str,)
    ).fetchone()[0]
    log_quality(conn, "prices_present",
                "ok" if n_today > 0 else "warning",
                f"{n_today} rows for {today_str}")

    active = conn.execute("SELECT COUNT(*) FROM universe WHERE active=1").fetchone()[0]
    priced = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM prices WHERE date=?", (today_str,)
    ).fetchone()[0]
    pct = round(priced / active * 100, 1) if active else 0.0
    log_quality(conn, "universe_coverage",
                "ok" if pct >= 90 else ("warning" if pct >= 70 else "error"),
                f"{priced}/{active} ({pct}%) tickers priced for {today_str}")

    bad = conn.execute(
        "SELECT COUNT(*) FROM prices WHERE close <= 0 OR high <= 0 OR low <= 0"
    ).fetchone()[0]
    log_quality(conn, "nonpositive_prices",
                "ok" if bad == 0 else "warning",
                f"{bad} rows with nonpositive OHLC")

    extreme = conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT close, LAG(close) OVER (PARTITION BY ticker ORDER BY date) AS prev
               FROM prices WHERE date >= ?
           ) sub
           WHERE sub.prev > 0 AND ABS(sub.close / sub.prev - 1) > 0.5""",
        (thirty_ago,),
    ).fetchone()[0]
    log_quality(conn, "extreme_returns",
                "ok" if extreme == 0 else "warning",
                f"{extreme} days with >50% move in last 30 days")

    mock_ct = conn.execute(
        "SELECT COUNT(*) FROM prices WHERE source='mock-fallback'"
    ).fetchone()[0]
    log_quality(conn, "fetch_source",
                "ok" if mock_ct == 0 else "warning",
                f"{mock_ct} mock-fallback rows in prices")


def _fetch_cnn_fear_greed() -> list[tuple[str, float]]:
    """
    Fetch CNN Fear & Greed scores from graphdata endpoint.

    Returns list of (date_str, score) tuples covering ~252 trading days of history.
    The endpoint always returns a rolling 1-year window, so INSERT OR IGNORE on
    repeated calls is safe — existing rows are silently skipped.
    Returns empty list on any failure.
    """
    import requests
    from datetime import datetime, timezone
    try:
        resp = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={"User-Agent": "Mozilla/5.0 (compatible; trading-dashboard/1.0)"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        rows: list[tuple[str, float]] = []
        for pt in data.get("fear_and_greed_historical", {}).get("data", []):
            ts_ms = pt.get("x")
            score = pt.get("y")
            if ts_ms is None or score is None:
                continue
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            rows.append((dt.strftime("%Y-%m-%d"), float(score)))

        return rows
    except Exception as exc:
        logger.warning("CNN Fear & Greed fetch failed: %s", exc)
        return []


def fetch_macro(conn: sqlite3.Connection, start: str) -> None:
    """Fetch macro + sector ETFs via yfinance, FRED series, and CNN Fear & Greed."""
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
                    rows.append((
                        ticker, str(dt.date()), float(row["close"]),
                        float(row["open"]) if pd.notna(row.get("open")) else None,
                        float(row["high"]) if pd.notna(row.get("high")) else None,
                        float(row["low"])  if pd.notna(row.get("low"))  else None,
                    ))

        conn.executemany(
            "INSERT OR IGNORE INTO macro_series"
            " (series_id, date, value, open, high, low) VALUES (?,?,?,?,?,?)",
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

    # CNN Fear & Greed (optional — silent skip on failure)
    # Returns ~252 days of history on every call; INSERT OR IGNORE is idempotent.
    fng_rows = _fetch_cnn_fear_greed()
    if fng_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO macro_series (series_id, date, value) VALUES (?, ?, ?)",
            [("CNN_FNG", d, v) for d, v in fng_rows],
        )
        conn.commit()
        latest_date, latest_score = max(fng_rows, key=lambda r: r[0])
        logger.info(
            "CNN Fear & Greed: %d rows stored (latest %s → %.1f)",
            len(fng_rows), latest_date, latest_score,
        )


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


def _mock_prices_for_tickers(
    tickers: list[str],
    days: int = 400,
) -> dict[str, pd.DataFrame]:
    """Generate deterministic synthetic OHLCV for offline/CI fallback.

    Seed derived from ticker characters so results are stable across Python sessions.
    """
    end = date.today()
    dates = pd.bdate_range(end=end, periods=days).strftime("%Y-%m-%d")
    result: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        # Stable cross-session seed: weighted char-code sum
        seed = sum(ord(c) * (i + 1) for i, c in enumerate(ticker)) % (2**31)
        rng = np.random.default_rng(seed)
        close = 50.0 * np.cumprod(1 + rng.normal(0, 0.01, days))
        daily_range = rng.uniform(0.005, 0.015, days)
        high   = close * (1 + daily_range)
        low    = close * (1 - daily_range)
        open_  = low + rng.random(days) * (high - low)
        volume = rng.integers(500_000, 5_000_000, days).astype(float)
        df = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=pd.Index(dates, name="date"),
        )
        result[ticker] = df
    return result


def _store_mock_prices(conn: sqlite3.Connection, mock_dfs: dict[str, pd.DataFrame]) -> None:
    """Store synthetic price data tagged with source='mock-fallback'."""
    total = 0
    for ticker, df in mock_dfs.items():
        rows = [
            (ticker, d, float(r["open"]), float(r["high"]),
             float(r["low"]), float(r["close"]), int(r["volume"]), "mock-fallback")
            for d, r in df.iterrows()
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO prices"
            " (ticker, date, open, high, low, close, volume, source)"
            " VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        total += len(rows)
    conn.commit()
    logger.info("Stored %d mock price rows for %d tickers", total, len(mock_dfs))


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
