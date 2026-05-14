import sqlite3
from typing import Optional

import pandas as pd

from data.db import connect


def compute_daily(date: str, conn: Optional[sqlite3.Connection] = None) -> dict:
    """
    Compute breadth metrics for one date from stored prices.
    Returns dict ready to INSERT into breadth_daily.
    Requires prices for the full universe to be loaded for at least 200 trading days prior.
    """
    c = conn or connect()

    # Load close prices for last 400 calendar days — sufficient for 200DMA + buffer
    df = pd.read_sql(
        """
        SELECT p.ticker, p.date, p.close
        FROM prices p
        JOIN universe u ON u.ticker = p.ticker
        WHERE u.active = 1
          AND p.date <= ?
          AND p.date >= date(?, '-400 days')
        ORDER BY p.date
        """,
        c,
        params=(date, date),
    )

    if df.empty:
        return {}

    pivot = df.pivot(index="date", columns="ticker", values="close")
    sma50  = pivot.rolling(50).mean()
    sma200 = pivot.rolling(200).mean()

    today   = pivot.loc[date] if date in pivot.index else None
    s50_row = sma50.loc[date] if date in sma50.index else None
    s200_row = sma200.loc[date] if date in sma200.index else None

    if today is None:
        return {}

    n = today.notna().sum()
    pct_above_50dma  = (today > s50_row).sum() / n * 100 if n else None
    pct_above_200dma = (today > s200_row).sum() / n * 100 if n else None

    high_52w = pivot.rolling(252).max().loc[date] if date in pivot.index else None
    low_52w  = pivot.rolling(252).min().loc[date] if date in pivot.index else None
    new_highs = int((today >= high_52w).sum()) if high_52w is not None else None
    new_lows  = int((today <= low_52w).sum()) if low_52w is not None else None
    pct_near_high = (today >= high_52w * 0.95).sum() / n * 100 if high_52w is not None and n else None

    if conn is None:
        c.close()

    return {
        "date": date,
        "pct_above_50dma": round(pct_above_50dma, 2) if pct_above_50dma is not None else None,
        "pct_above_200dma": round(pct_above_200dma, 2) if pct_above_200dma is not None else None,
        "new_highs_52w": new_highs,
        "new_lows_52w": new_lows,
        "pct_within_5pct_52w_high": round(pct_near_high, 2) if pct_near_high is not None else None,
    }


def upsert(row: dict, conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO breadth_daily
           (date, pct_above_50dma, pct_above_200dma, new_highs_52w, new_lows_52w, pct_within_5pct_52w_high)
           VALUES (:date, :pct_above_50dma, :pct_above_200dma, :new_highs_52w, :new_lows_52w, :pct_within_5pct_52w_high)
           ON CONFLICT(date) DO UPDATE SET
               pct_above_50dma          = excluded.pct_above_50dma,
               pct_above_200dma         = excluded.pct_above_200dma,
               new_highs_52w            = excluded.new_highs_52w,
               new_lows_52w             = excluded.new_lows_52w,
               pct_within_5pct_52w_high = excluded.pct_within_5pct_52w_high
        """,
        row,
    )
