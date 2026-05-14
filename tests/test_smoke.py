"""
Smoke tests — no network calls, no yfinance.
Verifies schema init, universe seeding, and basic indicator math.
Run with: python -m pytest tests/test_smoke.py -v
"""

import sqlite3
from pathlib import Path

import pandas as pd
import numpy as np
import pytest

from data.db import _SCHEMA, _INDEXES
from data.universe import seed_from_csv
from data.loader import load_price_dfs
from compute.indicators import add_sma, add_atr, add_momentum, is_uptrend
from scanners.pullback import scan, scan_universe
from render.homepage import _get_sector_perf, _sector_section_html

SEED_CSV = Path(__file__).parent.parent / "data" / "universe_seed.csv"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    conn.executescript(_INDEXES)
    return conn


@pytest.fixture
def price_df() -> pd.DataFrame:
    """220 rows of synthetic OHLCV with DatetimeIndex (for indicator tests)."""
    rng = np.random.default_rng(42)
    n = 220
    close = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "open":   close * (1 - rng.uniform(0, 0.005, n)),
        "high":   close * (1 + rng.uniform(0, 0.01, n)),
        "low":    close * (1 - rng.uniform(0, 0.01, n)),
        "close":  close,
        "volume": rng.integers(500_000, 5_000_000, n).astype(float),
    }, index=dates)


@pytest.fixture
def str_price_df() -> pd.DataFrame:
    """220 rows of synthetic OHLCV with string date index (matches load_price_dfs output)."""
    rng = np.random.default_rng(42)
    n = 220
    close = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    df = pd.DataFrame({
        "open":   close * (1 - rng.uniform(0, 0.005, n)),
        "high":   close * (1 + rng.uniform(0, 0.01, n)),
        "low":    close * (1 - rng.uniform(0, 0.01, n)),
        "close":  close,
        "volume": rng.integers(500_000, 5_000_000, n).astype(float),
    }, index=dates.strftime("%Y-%m-%d"))
    df.index.name = "date"
    return df


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_schema_creates_all_tables(mem_db):
    tables = {r[0] for r in mem_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    expected = {"universe", "prices", "corporate_actions", "macro_series", "breadth_daily", "scanner_hits"}
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"


def test_schema_idempotent(mem_db):
    # Running schema again must not raise
    mem_db.executescript(_SCHEMA)
    mem_db.executescript(_INDEXES)


# ---------------------------------------------------------------------------
# Universe seeding
# ---------------------------------------------------------------------------

def test_seed_from_csv_inserts_rows(mem_db):
    if not SEED_CSV.exists():
        pytest.skip("universe_seed.csv not found")
    n = seed_from_csv(SEED_CSV, mem_db)
    assert n > 0, "Expected at least one row inserted"


def test_seed_from_csv_idempotent(mem_db):
    if not SEED_CSV.exists():
        pytest.skip("universe_seed.csv not found")
    n1 = seed_from_csv(SEED_CSV, mem_db)
    n2 = seed_from_csv(SEED_CSV, mem_db)  # second run → INSERT OR IGNORE
    assert n2 == 0, "Re-seeding should insert 0 new rows"


def test_seed_has_required_columns(mem_db):
    if not SEED_CSV.exists():
        pytest.skip("universe_seed.csv not found")
    seed_from_csv(SEED_CSV, mem_db)
    df = pd.read_sql("SELECT * FROM universe LIMIT 5", mem_db)
    for col in ("ticker", "gics_sector"):
        assert col in df.columns
        assert df[col].notna().any(), f"{col} is all NULL"


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def test_add_sma(price_df):
    df = add_sma(price_df.copy(), [20, 50, 200])
    assert "sma20" in df.columns
    assert "sma200" in df.columns
    assert df["sma200"].iloc[199:].notna().all()
    assert df["sma200"].iloc[:199].isna().all()


def test_add_atr(price_df):
    df = add_atr(price_df.copy())
    assert "atr" in df.columns
    assert (df["atr"].dropna() > 0).all()


def test_add_momentum(price_df):
    df = add_momentum(price_df.copy(), [21])
    assert "mom21d" in df.columns


def test_is_uptrend_returns_bool_series(price_df):
    result = is_uptrend(price_df.copy())
    assert result.dtype == bool or result.dtype == object
    assert len(result) == len(price_df)


# ---------------------------------------------------------------------------
# Prices table round-trip
# ---------------------------------------------------------------------------

def test_prices_insert_and_query(mem_db, price_df):
    rows = [
        ("AAPL", str(dt.date()), row["open"], row["high"], row["low"], row["close"], int(row["volume"]))
        for dt, row in price_df.iterrows()
    ]
    mem_db.executemany(
        "INSERT OR IGNORE INTO prices (ticker, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    mem_db.commit()
    count = mem_db.execute("SELECT COUNT(*) FROM prices WHERE ticker='AAPL'").fetchone()[0]
    assert count == len(price_df)


# ---------------------------------------------------------------------------
# load_price_dfs
# ---------------------------------------------------------------------------

def test_load_price_dfs_returns_correct_shape(mem_db, price_df):
    rows = [
        ("AAPL", str(dt.date()), row["open"], row["high"], row["low"], row["close"], int(row["volume"]))
        for dt, row in price_df.iterrows()
    ]
    mem_db.executemany(
        "INSERT OR IGNORE INTO prices (ticker, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    mem_db.commit()

    result = load_price_dfs(mem_db, ["AAPL"], rows=100)
    assert "AAPL" in result
    df = result["AAPL"]
    assert len(df) == 100
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    # Index must be strings, not Timestamps
    assert isinstance(df.index[0], str)


def test_load_price_dfs_empty_tickers(mem_db):
    result = load_price_dfs(mem_db, [])
    assert result == {}


def test_load_price_dfs_missing_ticker(mem_db):
    result = load_price_dfs(mem_db, ["NONEXISTENT"])
    assert result == {}


# ---------------------------------------------------------------------------
# Pullback scanner (string-indexed DataFrames)
# ---------------------------------------------------------------------------

def test_scan_returns_false_for_unknown_date(str_price_df):
    assert scan(str_price_df, "1900-01-01") is False


def test_scan_universe_returns_list(str_price_df):
    last_date = str_price_df.index[-1]
    result = scan_universe({"AAPL": str_price_df}, last_date)
    assert isinstance(result, list)


def test_scan_universe_excludes_low_price(str_price_df):
    low_df = str_price_df.copy()
    low_df["close"] = 1.0
    low_df["open"]  = 1.0
    low_df["high"]  = 1.05
    low_df["low"]   = 0.95
    last_date = low_df.index[-1]
    result = scan_universe({"CHEAP": low_df}, last_date)
    assert "CHEAP" not in result


# ---------------------------------------------------------------------------
# Sector heatmap
# ---------------------------------------------------------------------------

def _seed_macro_series(conn, ticker: str, n: int = 140) -> None:
    rng = np.random.default_rng(hash(ticker) % (2**31))
    close = 50 * np.cumprod(1 + rng.normal(0.0002, 0.008, n))
    dates = pd.date_range("2025-10-01", periods=n, freq="B").strftime("%Y-%m-%d")
    conn.executemany(
        "INSERT OR IGNORE INTO macro_series (series_id, date, value) VALUES (?,?,?)",
        [(ticker, d, float(v)) for d, v in zip(dates, close)],
    )
    conn.commit()


def test_get_sector_perf_empty_db(mem_db):
    result = _get_sector_perf(mem_db)
    assert result == []


def test_get_sector_perf_returns_all_periods(mem_db):
    for ticker in ["XLK", "XLV", "XLF"]:
        _seed_macro_series(mem_db, ticker)

    result = _get_sector_perf(mem_db)
    assert len(result) == 3
    for row in result:
        assert "1W" in row and "1M" in row and "3M" in row and "6M" in row
        assert row["1M"] is not None  # 140 rows > 21+1


def test_get_sector_perf_sorted_by_1m(mem_db):
    for ticker in ["XLK", "XLV", "XLF", "XLY", "XLP"]:
        _seed_macro_series(mem_db, ticker)

    result = _get_sector_perf(mem_db)
    perfs = [r["1M"] for r in result if r["1M"] is not None]
    assert perfs == sorted(perfs, reverse=True)


def test_sector_section_html_renders(mem_db):
    for ticker in ["XLK", "XLV"]:
        _seed_macro_series(mem_db, ticker)
    sectors = _get_sector_perf(mem_db)
    html = _sector_section_html(sectors)
    assert "sector_hm" in html
    assert "SECTOR PERFORMANCE" in html


def test_sector_section_html_empty(mem_db):
    html = _sector_section_html([])
    assert html == ""
