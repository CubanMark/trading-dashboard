import logging
import os
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# FRED series used by the dashboard
SERIES = {
    "HY_OAS":       "BAMLH0A0HYM2",   # ICE BofA US HY OAS (credit)
    "YIELD_2Y":     "DGS2",            # 2-year Treasury
    "YIELD_10Y":    "DGS10",           # 10-year Treasury
    "YIELD_CURVE":  "T10Y2Y",          # 10Y-2Y spread
}


def fetch_series(
    series_id: str,
    start: str,
    end: Optional[str] = None,
    api_key: Optional[str] = None,
) -> pd.Series:
    """Fetch a single FRED series. Returns pd.Series indexed by date."""
    key = api_key or os.environ.get("FRED_API_KEY")
    if not key:
        raise ValueError("FRED_API_KEY not set")
    try:
        from fredapi import Fred
        fred = Fred(api_key=key)
        s = fred.get_series(series_id, observation_start=start, observation_end=end)
        s.index = pd.to_datetime(s.index).tz_localize(None)
        s.name = series_id
        return s.dropna()
    except Exception as exc:
        logger.error("FRED fetch failed for %s: %s", series_id, exc)
        return pd.Series(name=series_id, dtype=float)


def fetch_all(start: str, end: Optional[str] = None) -> dict[str, pd.Series]:
    """Fetch all dashboard FRED series."""
    return {name: fetch_series(sid, start=start, end=end) for name, sid in SERIES.items()}
