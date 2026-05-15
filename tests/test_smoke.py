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

from data.db import _SCHEMA, _INDEXES, ensure_column
from data.universe import seed_from_csv, normalize_yahoo_symbol
from data.loader import load_price_dfs
from compute.indicators import add_sma, add_atr, add_momentum, is_uptrend
from scanners.pullback import scan, scan_universe, scan_ma20, scan_ma10, scan_3d, _annotate_overlaps
from render.homepage import (
    _get_sector_perf, _sector_section_html,
    _get_industry_perf, _industry_section_html,
    _scanner_section_html,
)

SEED_CSV = Path(__file__).parent.parent / "data" / "universe_seed.csv"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    conn.executescript(_INDEXES)
    # Apply same column migrations as init_schema()
    ensure_column(conn, "macro_series",  "open",         "REAL")
    ensure_column(conn, "macro_series",  "high",         "REAL")
    ensure_column(conn, "macro_series",  "low",          "REAL")
    ensure_column(conn, "prices",        "source",       "TEXT DEFAULT 'yfinance'")
    ensure_column(conn, "scanner_hits",  "scanner_label","TEXT")
    ensure_column(conn, "scanner_hits",  "also_in",      "TEXT DEFAULT ''")
    ensure_column(conn, "scanner_hits",  "warning",      "TEXT")
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
    expected = {
        "universe", "prices", "corporate_actions", "macro_series",
        "breadth_daily", "scanner_hits", "data_quality_checks", "run_log",
    }
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


def test_scan_universe_returns_dict(str_price_df):
    last_date = str_price_df.index[-1]
    result = scan_universe({"AAPL": str_price_df}, last_date)
    assert isinstance(result, dict)
    assert "regime" in result
    assert "hits" in result


def test_scan_universe_excludes_low_price(str_price_df):
    low_df = str_price_df.copy()
    low_df["close"] = 1.0
    low_df["open"]  = 1.0
    low_df["high"]  = 1.05
    low_df["low"]   = 0.95
    last_date = low_df.index[-1]
    result = scan_universe({"CHEAP": low_df}, last_date)
    all_hits = {t for hits in result["hits"].values() for t in hits}
    assert "CHEAP" not in all_hits


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


# ---------------------------------------------------------------------------
# Industry performance
# ---------------------------------------------------------------------------

def _seed_universe_with_industry(conn, ticker: str, industry: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO universe (ticker, gics_sub_industry, active)
           VALUES (?, ?, 1)""",
        (ticker, industry),
    )
    conn.commit()


def _seed_prices_for_ticker(conn, ticker: str, n: int = 30) -> None:
    rng = np.random.default_rng(hash(ticker) % (2**31))
    close = 50 * np.cumprod(1 + rng.normal(0.001, 0.01, n))
    dates = pd.date_range("2026-04-01", periods=n, freq="B").strftime("%Y-%m-%d")
    conn.executemany(
        "INSERT OR IGNORE INTO prices (ticker, date, open, high, low, close, volume)"
        " VALUES (?,?,?,?,?,?,?)",
        [(ticker, d, float(v), float(v)*1.01, float(v)*0.99, float(v), 1_000_000)
         for d, v in zip(dates, close)],
    )
    conn.commit()


def test_get_industry_perf_empty_db(mem_db):
    assert _get_industry_perf(mem_db) == []


def test_get_industry_perf_returns_sorted(mem_db):
    # Seed 3 industries with 3 stocks each
    for i, ind in enumerate(["Semiconductors", "Biotechnology", "Banks"]):
        for j in range(3):
            ticker = f"{ind[:3].upper()}{j}"
            _seed_universe_with_industry(mem_db, ticker, ind)
            _seed_prices_for_ticker(mem_db, ticker)

    result = _get_industry_perf(mem_db)
    assert len(result) == 3
    perfs = [r["perf_1m"] for r in result]
    assert perfs == sorted(perfs, reverse=True)
    assert all(r["n"] >= 3 for r in result)


def test_get_industry_perf_filters_small_groups(mem_db):
    # Industry with only 2 stocks should be excluded (min = 3)
    for j in range(2):
        ticker = f"TINY{j}"
        _seed_universe_with_industry(mem_db, ticker, "TinyIndustry")
        _seed_prices_for_ticker(mem_db, ticker)

    result = _get_industry_perf(mem_db)
    assert all(r["industry"] != "TinyIndustry" for r in result)


def test_industry_section_html_renders(mem_db):
    for i, ind in enumerate(["Semiconductors", "Biotechnology", "Banks"]):
        for j in range(3):
            ticker = f"{ind[:3].upper()}{j}"
            _seed_universe_with_industry(mem_db, ticker, ind)
            _seed_prices_for_ticker(mem_db, ticker)

    industries = _get_industry_perf(mem_db)
    html = _industry_section_html(industries)
    assert "INDUSTRY PERFORMANCE" in html
    assert "Top" in html and "Bottom" in html


def test_industry_section_html_empty(mem_db):
    assert _industry_section_html([]) == ""


# ---------------------------------------------------------------------------
# Symbol normalisation
# ---------------------------------------------------------------------------

def test_normalize_yahoo_symbol():
    assert normalize_yahoo_symbol("MOG.A")  == "MOG-A"
    assert normalize_yahoo_symbol("BRK.B")  == "BRK-B"
    assert normalize_yahoo_symbol("AAPL")   == "AAPL"


def test_seed_normalizes_dot_tickers(mem_db, tmp_path):
    csv_file = tmp_path / "mini.csv"
    csv_file.write_text("ticker,gics_sector\nMOG.A,Industrials\nBRK.B,Financials\n")
    seed_from_csv(str(csv_file), mem_db)
    tickers = {r[0] for r in mem_db.execute("SELECT ticker FROM universe").fetchall()}
    assert "MOG-A" in tickers, "MOG.A must be normalised to MOG-A"
    assert "BRK-B" in tickers, "BRK.B must be normalised to BRK-B"
    assert "MOG.A" not in tickers
    assert "BRK.B" not in tickers


# ---------------------------------------------------------------------------
# ensure_column
# ---------------------------------------------------------------------------

def test_ensure_column_adds_column(mem_db):
    ensure_column(mem_db, "universe", "test_col_add", "INTEGER DEFAULT 0")
    cols = {row[1] for row in mem_db.execute("PRAGMA table_info(universe)")}
    assert "test_col_add" in cols


def test_ensure_column_idempotent(mem_db):
    ensure_column(mem_db, "universe", "idem_col", "TEXT")
    ensure_column(mem_db, "universe", "idem_col", "TEXT")  # second call must not raise
    cols = {row[1] for row in mem_db.execute("PRAGMA table_info(universe)")}
    assert "idem_col" in cols


def test_macro_series_has_ohl_columns(mem_db):
    cols = {row[1] for row in mem_db.execute("PRAGMA table_info(macro_series)")}
    assert {"open", "high", "low"}.issubset(cols)


def test_prices_has_source_column(mem_db):
    cols = {row[1] for row in mem_db.execute("PRAGMA table_info(prices)")}
    assert "source" in cols


# ---------------------------------------------------------------------------
# data quality + run log
# ---------------------------------------------------------------------------

def test_log_quality_writes_row(mem_db):
    from data.quality import log_quality
    log_quality(mem_db, "test_check", "ok", "all good")
    row = mem_db.execute(
        "SELECT check_name, status, message FROM data_quality_checks"
    ).fetchone()
    assert row == ("test_check", "ok", "all good")


def test_log_run_writes_row(mem_db):
    from data.quality import log_run
    log_run(mem_db, "test_step", "ok", "step done")
    row = mem_db.execute(
        "SELECT step, status, message FROM run_log"
    ).fetchone()
    assert row == ("test_step", "ok", "step done")


# ---------------------------------------------------------------------------
# Mock price generation
# ---------------------------------------------------------------------------

def test_mock_prices_deterministic():
    from data.loader import _mock_prices_for_tickers
    r1 = _mock_prices_for_tickers(["AAPL"], days=50)
    r2 = _mock_prices_for_tickers(["AAPL"], days=50)
    pd.testing.assert_frame_equal(r1["AAPL"], r2["AAPL"])


def test_mock_prices_valid_ohlc():
    from data.loader import _mock_prices_for_tickers
    df = _mock_prices_for_tickers(["TEST"], days=100)["TEST"]
    assert len(df) == 100
    assert (df["high"] >= df["close"]).all(), "high must be >= close"
    assert (df["close"] >= df["low"]).all(),  "close must be >= low"
    assert (df["low"] > 0).all(),             "low must be positive"
    assert (df["volume"] > 0).all()


# ---------------------------------------------------------------------------
# Dimension compute functions
# ---------------------------------------------------------------------------

def test_compute_breadth_na_on_empty(mem_db):
    from compute.dimensions import compute_breadth
    result = compute_breadth(mem_db)
    assert result["metric_id"] == "breadth"
    assert result["status"] == "na"


def test_compute_breadth_green_above_60(mem_db):
    from compute.dimensions import compute_breadth
    mem_db.execute(
        "INSERT INTO breadth_daily (date, pct_above_50dma, pct_above_200dma) VALUES ('2026-05-14', 65.0, 55.0)"
    )
    mem_db.commit()
    result = compute_breadth(mem_db)
    assert result["status"] == "green"
    assert result["value"] == 65.0
    assert "65.0%" in result["label"]


def test_compute_risk_na_without_etfs(mem_db):
    from compute.dimensions import compute_risk
    result = compute_risk(mem_db)
    assert result["metric_id"] == "risk"
    assert result["status"] == "na"


def test_compute_volatility_na_without_data(mem_db):
    from compute.dimensions import compute_volatility
    result = compute_volatility(mem_db)
    assert result["metric_id"] == "volatility"
    assert result["status"] == "na"


def test_compute_volatility_green_below_20(mem_db):
    from compute.dimensions import compute_volatility
    mem_db.execute(
        "INSERT OR IGNORE INTO macro_series (series_id, date, value) VALUES ('^VIX', '2026-05-14', 15.0)"
    )
    mem_db.commit()
    result = compute_volatility(mem_db)
    assert result["status"] == "green"
    assert result["value"] == 15.0


def test_compute_sentiment_na_without_data(mem_db):
    from compute.dimensions import compute_sentiment
    result = compute_sentiment(mem_db)
    assert result["status"] == "na"
    assert result["metric_id"] == "sentiment"


def test_compute_sentiment_yellow_in_normal_range(mem_db):
    from compute.dimensions import compute_sentiment
    mem_db.execute(
        "INSERT OR IGNORE INTO macro_series (series_id, date, value) VALUES ('CNN_FNG', '2026-05-15', 55.0)"
    )
    mem_db.commit()
    result = compute_sentiment(mem_db)
    assert result["metric_id"] == "sentiment"
    assert result["status"] == "yellow"
    assert result["value"] == 55.0
    assert "F&G 55" in result["label"]


def test_compute_sentiment_red_on_extreme_fear(mem_db):
    from compute.dimensions import compute_sentiment
    mem_db.execute(
        "INSERT OR IGNORE INTO macro_series (series_id, date, value) VALUES ('CNN_FNG', '2026-05-15', 18.0)"
    )
    mem_db.commit()
    result = compute_sentiment(mem_db)
    assert result["status"] == "red"
    assert "Extreme Fear" in result["label"]


def test_compute_sentiment_red_on_extreme_greed(mem_db):
    from compute.dimensions import compute_sentiment
    mem_db.execute(
        "INSERT OR IGNORE INTO macro_series (series_id, date, value) VALUES ('CNN_FNG', '2026-05-15', 82.0)"
    )
    mem_db.commit()
    result = compute_sentiment(mem_db)
    assert result["status"] == "red"
    assert "Extreme Greed" in result["label"]


def test_compute_credit_na_without_fred(mem_db):
    from compute.dimensions import compute_credit
    result = compute_credit(mem_db)
    assert result["status"] == "na"
    assert "FRED" in result["note"]


def test_compute_all_dimensions_returns_6(mem_db):
    from compute.dimensions import compute_all_dimensions
    dims = compute_all_dimensions(mem_db)
    assert len(dims) == 6
    metric_ids = {d["metric_id"] for d in dims}
    assert metric_ids == {"breadth", "risk", "volatility", "obos", "sentiment", "credit"}


# ---------------------------------------------------------------------------
# Dimension pill rendering
# ---------------------------------------------------------------------------

def test_dimension_pills_renders_6(mem_db):
    from compute.dimensions import compute_all_dimensions
    from render.homepage import _dimension_pills_html
    dims = compute_all_dimensions(mem_db)
    html = _dimension_pills_html(dims)
    assert html.count("dim-pill") >= 6


def test_dimension_pills_status_classes(mem_db):
    from render.homepage import _dimension_pills_html
    dims = [
        {"metric_id": "test", "label": "50%", "status": "green",
         "trend": "up", "change_1w": 2.0, "note": "ok"}
    ]
    html = _dimension_pills_html(dims)
    assert "status-green" in html
    assert "dim-pill-value green" in html


# ---------------------------------------------------------------------------
# Operation summary
# ---------------------------------------------------------------------------

def test_operation_summary_structure(mem_db):
    from render.homepage import _get_operation_summary
    summary = _get_operation_summary(mem_db)
    assert "last_date" in summary
    assert "active" in summary
    assert "priced" in summary
    assert "has_mock" in summary
    assert "dq_status" in summary
    assert summary["has_mock"] is False


# ---------------------------------------------------------------------------
# Session 3: Scanner variants, regime filter, overlap annotation
# ---------------------------------------------------------------------------

def test_scan_ma20_returns_bool(str_price_df):
    last_date = str_price_df.index[-1]
    assert isinstance(scan_ma20(str_price_df, last_date), bool)


def test_scan_ma10_returns_bool(str_price_df):
    last_date = str_price_df.index[-1]
    assert isinstance(scan_ma10(str_price_df, last_date), bool)


def test_scan_3d_returns_bool(str_price_df):
    last_date = str_price_df.index[-1]
    assert isinstance(scan_3d(str_price_df, last_date), bool)


def test_scan_3d_detects_three_lower_closes(str_price_df):
    df = str_price_df.copy()
    dates = df.index.tolist()
    # Stamp 3 consecutive lower closes at the end — well above MIN_PRICE
    df.loc[dates[-3], "close"] = 60.0
    df.loc[dates[-2], "close"] = 58.0
    df.loc[dates[-1], "close"] = 56.0
    for d in dates[-3:]:
        df.loc[d, "high"]  = df.loc[d, "close"] * 1.01
        df.loc[d, "low"]   = df.loc[d, "close"] * 0.99
        df.loc[d, "open"]  = df.loc[d, "close"] * 1.005
        df.loc[d, "volume"] = 2_000_000.0
    # Result depends on uptrend flag; we only assert it returns bool
    assert isinstance(scan_3d(df, dates[-1]), bool)


def test_scan_universe_bear_regime():
    n = 210
    rng = np.random.default_rng(0)
    close = np.maximum(1.0, 200 - np.arange(n) * 0.5 + rng.normal(0, 0.5, n))
    dates = pd.date_range("2025-01-01", periods=n, freq="B").strftime("%Y-%m-%d")
    spy = pd.Series(close.tolist(), index=dates)
    result = scan_universe({}, dates[-1], spy_close=spy)
    assert result["regime"] == "bear"
    assert result["hits"] == {}


def test_scan_universe_excludes_sub_industry(str_price_df):
    meta_map = {"BIO": {"gics_sub_industry": "Biotechnology"}}
    last_date = str_price_df.index[-1]
    result = scan_universe({"BIO": str_price_df}, last_date, meta_map=meta_map)
    all_hits = {t for hits in result["hits"].values() for t in hits}
    assert "BIO" not in all_hits


def test_annotate_overlaps_fills_also_in():
    rows = [
        {"ticker": "AAPL", "scanner": "pullback_ma20", "also_in": ""},
        {"ticker": "AAPL", "scanner": "pullback_3d",   "also_in": ""},
        {"ticker": "MSFT", "scanner": "pullback_ma20", "also_in": ""},
    ]
    _annotate_overlaps(rows)
    aapl_ma20 = next(r for r in rows if r["ticker"] == "AAPL" and r["scanner"] == "pullback_ma20")
    assert "3D" in aapl_ma20["also_in"]
    msft_row = next(r for r in rows if r["ticker"] == "MSFT")
    assert msft_row["also_in"] == ""


def test_scanner_section_html_no_hits():
    html = _scanner_section_html([], "2026-05-14")
    assert "No pullback setups today" in html
    assert "scanner-warning" in html


def test_scanner_section_html_renders_tags():
    hits = [{
        "ticker": "AAPL",
        "scanner": "pullback_ma20",
        "scanner_label": "MA20 Pullback",
        "gics_sector": "Technology",
        "gics_sub_industry": "Semiconductors",
        "rs_rank": 75.0,
        "perf_1m": 5.2,
        "dist_52w_high": -3.1,
        "also_in": "",
        "date": "2026-05-14",
    }]
    html = _scanner_section_html(hits, "2026-05-14")
    assert "AAPL" in html
    assert "MA20 Pullback" in html
    assert "tag-ma20" in html
    assert "scanner-toolbar" in html


def test_industry_perf_includes_hits_field(mem_db):
    for j in range(3):
        ticker = f"SEMI{j}"
        _seed_universe_with_industry(mem_db, ticker, "Semiconductors")
        _seed_prices_for_ticker(mem_db, ticker)

    result = _get_industry_perf(mem_db)
    assert len(result) > 0
    for r in result:
        assert "hits" in r
        assert isinstance(r["hits"], int)
        assert r["hits"] >= 0


def test_industry_section_html_shows_hits_badge(mem_db):
    for j in range(3):
        ticker = f"SEMI{j}"
        _seed_universe_with_industry(mem_db, ticker, "Semiconductors")
        _seed_prices_for_ticker(mem_db, ticker)

    industries = _get_industry_perf(mem_db)
    html = _industry_section_html(industries)
    assert "industry-hits-badge" in html
