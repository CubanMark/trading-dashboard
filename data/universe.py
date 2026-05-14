import logging
import sqlite3
from io import StringIO
from pathlib import Path
from typing import Optional

import pandas as pd

from data.db import connect

logger = logging.getLogger(__name__)

# Wikipedia table URLs for S&P index constituents
_WIKI = {
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

# Macro tickers stored in macro_series (not in universe table)
MACRO_TICKERS = [
    "SPY", "QQQ", "IWM",
    "^VIX", "^VIX3M",
    "^TNX", "^IRX",       # US10Y, US2Y
    "DX-Y.NYB",           # DXY
    "USO", "TLT", "GLD", "HYG", "LQD",
]

SECTOR_ETFS = [
    "XLK", "XLV", "XLF", "XLY", "XLP",
    "XLE", "XLI", "XLU", "XLB", "XLRE", "XLC",
]


def load(conn: Optional[sqlite3.Connection] = None) -> pd.DataFrame:
    c = conn or connect()
    df = pd.read_sql("SELECT * FROM universe WHERE active = 1", c)
    if conn is None:
        c.close()
    return df


def tickers(conn: Optional[sqlite3.Connection] = None) -> list[str]:
    return load(conn)["ticker"].tolist()


def seed_sp400_sp600(
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """
    Fetch S&P 400 (MidCap) and S&P 600 (SmallCap) constituents from Wikipedia
    and insert missing tickers into the universe table.
    Returns number of new rows inserted.

    Wikipedia column names differ by index — this function normalises them.
    Safe to re-run (INSERT OR IGNORE).
    """
    c = conn or connect()
    inserted = 0

    index_flags = {
        "sp400": {"in_sp500": 0, "in_sp1500": 1},
        "sp600": {"in_sp500": 0, "in_sp1500": 1},
    }

    for index_key, url in _WIKI.items():
        try:
            import requests
            logger.info("Fetching %s constituents from Wikipedia", index_key)
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; trading-dashboard-research/1.0)"},
                timeout=15,
            )
            resp.raise_for_status()
            html = StringIO(resp.text)
            tables = pd.read_html(html, attrs={"id": "constituents"})
            if not tables:
                html.seek(0)
                all_tables = pd.read_html(html)
                if not all_tables:
                    logger.error("%s: no tables found on page", index_key)
                    continue
                # Pick largest table — constituents table dominates in row count
                tables = [max(all_tables, key=len)]
            df = tables[0]
            logger.info("%s: using table with %d rows", index_key, len(df))

            # Normalise column names across Wikipedia page variations
            df.columns = [str(c).strip() for c in df.columns]
            logger.info("%s: table columns found: %s", index_key, df.columns.tolist())

            col_map = {
                # Ticker: any column containing "ticker" or "symbol"
                **{c: "ticker" for c in df.columns if any(k in c.lower() for k in ("ticker", "symbol"))},
                # Name
                **{c: "name" for c in df.columns if c.lower() in ("security", "company", "name", "company name")},
                # Sector: contains "sector" but not "sub"
                **{c: "gics_sector" for c in df.columns if "sector" in c.lower() and "sub" not in c.lower()},
                # Sub-industry
                **{c: "gics_sub_industry" for c in df.columns if "sub" in c.lower() and "industry" in c.lower()},
            }
            df = df.rename(columns=col_map)

            if "ticker" not in df.columns:
                logger.error(
                    "%s: could not identify ticker column. "
                    "Columns found: %s — add a mapping to seed_sp400_sp600()",
                    index_key, df.columns.tolist(),
                )
                continue

            # Clean ticker (some Wikipedia pages have footnote refs like "AAPL[1]")
            df["ticker"] = df["ticker"].astype(str).str.replace(r"\[.*?\]", "", regex=True).str.strip()
            df = df[df["ticker"].str.match(r"^[A-Z.\-]{1,10}$")]  # basic sanity filter

            flags = index_flags[index_key]
            for _, row in df.iterrows():
                c.execute(
                    """INSERT OR IGNORE INTO universe
                       (ticker, name, gics_sector, gics_sub_industry,
                        in_sp500, in_sp1500, active)
                       VALUES (?, ?, ?, ?, ?, ?, 1)""",
                    (
                        row.get("ticker"),
                        row.get("name"),
                        row.get("gics_sector"),
                        row.get("gics_sub_industry"),
                        flags["in_sp500"],
                        flags["in_sp1500"],
                    ),
                )
                inserted += c.execute("SELECT changes()").fetchone()[0]

            c.commit()
            logger.info("%s: inserted %d new tickers", index_key, inserted)

        except Exception as exc:
            logger.error("Failed to fetch %s from Wikipedia: %s", index_key, exc)

    if conn is None:
        c.close()
    return inserted


def seed_from_csv(
    csv_path: str | Path,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """
    One-time import from Swing Lab universe CSV.
    Only imports rows where passes_universe_filter is True (or column absent).
    Returns number of rows inserted.
    """
    df = pd.read_csv(csv_path)

    # Filter to universe members if the column exists
    if "passes_universe_filter" in df.columns:
        df = df[df["passes_universe_filter"].astype(str).str.lower() == "true"]

    # Normalise column names to our schema
    df = df.rename(columns={
        "security":        "name",
        "gics_sub_industry": "gics_sub_industry",  # already correct
    })

    # gics_industry is absent in the Swing Lab CSV – derive coarsely from sub_industry
    # (will be enriched later if needed; NULL is acceptable for Phase 1)
    if "gics_industry" not in df.columns:
        df["gics_industry"] = None

    df["in_sp500"]   = 1
    df["in_sp1500"]  = 1
    df["active"]     = 1

    required = ["ticker"]
    if not all(c in df.columns for c in required):
        raise ValueError(f"CSV missing required columns. Found: {df.columns.tolist()}")

    c = conn or connect()
    inserted = 0
    for _, row in df.iterrows():
        c.execute(
            """INSERT OR IGNORE INTO universe
               (ticker, name, gics_sector, gics_industry, gics_sub_industry,
                in_sp500, in_sp1500, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("ticker"),
                row.get("name"),
                row.get("gics_sector"),
                row.get("gics_industry"),
                row.get("gics_sub_industry"),
                int(row.get("in_sp500", 1)),
                int(row.get("in_sp1500", 1)),
                1,
            ),
        )
        inserted += c.execute("SELECT changes()").fetchone()[0]

    c.commit()
    if conn is None:
        c.close()
    return inserted
