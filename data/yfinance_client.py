import logging
import time
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_RETRY_DELAYS = [2, 8, 30]  # seconds between attempts


def fetch_ticker(
    ticker: str,
    start: str,
    end: Optional[str] = None,
    retries: int = 3,
) -> pd.DataFrame:
    """Download unadjusted OHLCV for one ticker. Returns empty DataFrame on failure."""
    for attempt, delay in enumerate(([0] + _RETRY_DELAYS)[:retries]):
        if delay:
            time.sleep(delay)
        try:
            raw = yf.download(
                ticker,
                start=start,
                end=end,
                auto_adjust=False,
                actions=True,
                progress=False,
                threads=False,
            )
            if raw.empty:
                logger.warning("%s: empty response (attempt %d)", ticker, attempt + 1)
                continue
            return _normalize(raw, ticker)
        except Exception as exc:
            logger.warning("%s: attempt %d failed: %s", ticker, attempt + 1, exc)
    logger.error("%s: all attempts failed", ticker)
    return pd.DataFrame()


def fetch_tickers_bulk(
    tickers: list[str],
    start: str,
    end: Optional[str] = None,
) -> dict[str, pd.DataFrame]:
    """Download multiple tickers, return dict ticker → DataFrame."""
    results = {}
    for ticker in tickers:
        df = fetch_ticker(ticker, start=start, end=end)
        if not df.empty:
            results[ticker] = df
    return results


def extract_corporate_actions(
    ticker: str,
    start: str,
    end: Optional[str] = None,
) -> pd.DataFrame:
    """Return splits and dividends as long-form DataFrame."""
    t = yf.Ticker(ticker)
    rows = []
    for date, ratio in t.splits.items():
        if ratio != 1.0:
            rows.append({"ticker": ticker, "date": str(date.date()), "action_type": "split", "value": ratio})
    for date, amount in t.dividends.items():
        rows.append({"ticker": ticker, "date": str(date.date()), "action_type": "dividend", "value": amount})
    df = pd.DataFrame(rows)
    if not df.empty and start:
        df = df[df["date"] >= start]
    if not df.empty and end:
        df = df[df["date"] < end]
    return df


def _normalize(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0].lower() for col in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "date"
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].copy()
    df.dropna(subset=["close"], inplace=True)
    return df
