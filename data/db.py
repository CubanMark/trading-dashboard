import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "dashboard.db"

_SCHEMA = """
-- Ticker metadata + GICS classification
CREATE TABLE IF NOT EXISTS universe (
    ticker            TEXT PRIMARY KEY,
    name              TEXT,
    gics_sector       TEXT,
    gics_industry     TEXT,
    gics_sub_industry TEXT,
    in_sp500          INTEGER DEFAULT 0,
    in_sp1500         INTEGER DEFAULT 0,
    in_watchlist      INTEGER DEFAULT 0,
    active            INTEGER DEFAULT 1,
    updated_at        TEXT
);

-- Raw unadjusted OHLCV (one row per ticker × date)
CREATE TABLE IF NOT EXISTS prices (
    ticker  TEXT    NOT NULL,
    date    TEXT    NOT NULL,
    open    REAL    NOT NULL,
    high    REAL    NOT NULL,
    low     REAL    NOT NULL,
    close   REAL    NOT NULL,
    volume  INTEGER NOT NULL,
    PRIMARY KEY (ticker, date)
);

-- Splits and dividends stored separately so we own the adjustment math
CREATE TABLE IF NOT EXISTS corporate_actions (
    ticker      TEXT NOT NULL,
    date        TEXT NOT NULL,
    action_type TEXT NOT NULL,  -- 'split' | 'dividend'
    value       REAL NOT NULL,  -- split ratio (e.g. 2.0) or dividend per share
    PRIMARY KEY (ticker, date, action_type)
);

-- Macro + index time series (SPY, QQQ, VIX, FRED series, etc.)
CREATE TABLE IF NOT EXISTS macro_series (
    series_id TEXT NOT NULL,  -- ticker or FRED series ID, e.g. 'VIX', 'BAMLH0A0HYM2'
    date      TEXT NOT NULL,
    value     REAL,
    PRIMARY KEY (series_id, date)
);

-- Daily breadth snapshot for the S&P 500 universe
CREATE TABLE IF NOT EXISTS breadth_daily (
    date                     TEXT PRIMARY KEY,
    pct_above_50dma          REAL,
    pct_above_200dma         REAL,
    adv_decline_cumulative   REAL,   -- running A/D line (rebased to 0 at first stored date)
    new_highs_52w            INTEGER,
    new_lows_52w             INTEGER,
    pct_within_5pct_52w_high REAL
);

-- Scanner output: one row per (date, ticker, scanner) hit
CREATE TABLE IF NOT EXISTS scanner_hits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT    NOT NULL,
    ticker        TEXT    NOT NULL,
    scanner       TEXT    NOT NULL,  -- 'pullback_ma20' | 'vcp' | 'breakout'
    gics_sector   TEXT,
    gics_industry TEXT,
    rs_rank       REAL,              -- percentile 0-100 vs. full universe
    perf_1m       REAL,
    adr_pct       REAL,              -- avg daily range %
    atr           REAL,
    avg_volume    REAL,
    dist_52w_high REAL,              -- % below 52-week high (negative = below)
    earnings_date TEXT,
    UNIQUE (date, ticker, scanner)
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices (ticker);
CREATE INDEX IF NOT EXISTS idx_prices_date   ON prices (date);
CREATE INDEX IF NOT EXISTS idx_hits_date     ON scanner_hits (date);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema() -> None:
    with connect() as conn:
        conn.executescript(_SCHEMA)
        conn.executescript(_INDEXES)
