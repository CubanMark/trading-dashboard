import logging
import os
from datetime import date
from pathlib import Path

import pandas as pd

from data.db import connect, init_schema
from data import universe as uni
from data.universe import seed_sp400_sp600
from data import loader
from compute import breadth
from compute.indicators import add_rs, rs_rank as compute_rs_rank
from render import homepage
from scanners import pullback as pb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Seed file: commit data/universe_seed.csv to the repo for CI.
# Falls back to local Swing Lab artifact for development.
_SEED_CANDIDATES = [
    Path(__file__).parent / "data" / "universe_seed.csv",
    Path(r"G:\Meine Ablage\05_Projekte\Trading\02_Swing-Lab\artifacts\2026-05-01_phase1_universe_filtered.csv"),
]


def main() -> None:
    logger.info("=== Daily build started ===")

    init_schema()

    with connect() as conn:
        # --- Seed universe (each index checked independently) ---
        sp500_count = conn.execute(
            "SELECT COUNT(*) FROM universe WHERE in_sp500=1"
        ).fetchone()[0]
        if sp500_count == 0:
            seed_csv = next((p for p in _SEED_CANDIDATES if p.exists()), None)
            if seed_csv is None:
                logger.error(
                    "Universe empty and no seed CSV found. "
                    "Copy the Swing Lab universe CSV to data/universe_seed.csv."
                )
                return
            n = uni.seed_from_csv(seed_csv, conn)
            logger.info("Seeded %d S&P 500 tickers from %s", n, seed_csv.name)

        mid_small_count = conn.execute(
            "SELECT COUNT(*) FROM universe WHERE in_sp500=0 AND in_sp1500=1"
        ).fetchone()[0]
        if mid_small_count == 0:
            n2 = seed_sp400_sp600(conn)
            logger.info("Seeded %d S&P 400/600 tickers from Wikipedia", n2)

        # --- Step 1+2: Fetch prices + macro (incremental or bulk) ---
        loader.run_update(conn)

        # --- Step 3: Compute breadth ---
        # Use last available trading date (yfinance EOD data lags by 1 day)
        latest = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
        if not latest:
            logger.warning("No price data in DB — skipping breadth and scanners")
            latest = str(date.today())
        today = latest
        logger.info("Step 3: computing breadth for %s …", today)
        row = breadth.compute_daily(today, conn)
        if row:
            breadth.upsert(row, conn)
            conn.commit()
            logger.info(
                "Breadth %s — %%>50DMA: %.1f  %%>200DMA: %.1f  NH/NL: %s/%s",
                today,
                row.get("pct_above_50dma") or 0,
                row.get("pct_above_200dma") or 0,
                row.get("new_highs_52w"),
                row.get("new_lows_52w"),
            )
        else:
            logger.warning("Breadth computation returned no data for %s", today)

        # --- Step 4: Run scanners ---
        logger.info("Step 4: loading prices for scanner …")
        universe_df = uni.load(conn)
        meta_map = (
            universe_df.set_index("ticker")[["gics_sector", "gics_industry"]]
            .to_dict("index")
        )

        price_dfs = loader.load_price_dfs(conn, universe_df["ticker"].tolist(), rows=400)
        logger.info("Loaded prices for %d tickers", len(price_dfs))

        # SPY from macro_series for RS computation
        spy_close = pd.read_sql(
            "SELECT date, value FROM macro_series WHERE series_id = 'SPY' ORDER BY date",
            conn,
        ).set_index("date")["value"]

        # Compute RS-3M raw score per ticker on today, then percentile-rank
        rs_raw: dict[str, float] = {}
        for ticker, df in price_dfs.items():
            if today not in df.index or len(df) < 65:
                continue
            try:
                df_rs = add_rs(df.copy(), spy_close.reindex(df.index))
                val = df_rs.loc[today, "rs_3m"]
                if pd.notna(val):
                    rs_raw[ticker] = float(val)
            except Exception:
                pass

        rs_ranks_s = (
            compute_rs_rank(pd.Series(rs_raw)) if rs_raw else pd.Series(dtype=float)
        )
        logger.info("RS rank computed for %d tickers", len(rs_ranks_s))

        # Run pullback scanner
        hits = pb.scan_universe(price_dfs, today)
        logger.info("Pullback scanner: %d hits on %s", len(hits), today)

        hit_rows = []
        for ticker in hits:
            df = price_dfs.get(ticker)
            if df is None:
                continue
            meta = dict(meta_map.get(ticker, {}))
            meta["rs_rank"] = float(rs_ranks_s.get(ticker, 50.0))
            meta["earnings_date"] = None
            try:
                hit_rows.append(pb.build_hit_row(ticker, df, today, meta))
            except Exception as exc:
                logger.warning("build_hit_row failed for %s: %s", ticker, exc)

        if hit_rows:
            conn.executemany(
                """INSERT OR REPLACE INTO scanner_hits
                   (date, ticker, scanner, gics_sector, gics_industry, rs_rank,
                    perf_1m, adr_pct, atr, avg_volume, dist_52w_high, earnings_date)
                   VALUES (:date, :ticker, :scanner, :gics_sector, :gics_industry, :rs_rank,
                           :perf_1m, :adr_pct, :atr, :avg_volume, :dist_52w_high, :earnings_date)""",
                hit_rows,
            )
            conn.commit()
        logger.info("Stored %d scanner hits for %s", len(hit_rows), today)

        # --- Step 5: Render HTML ---
        homepage.build(conn, today)
        logger.info("Homepage written to pages/index.html")

    logger.info("=== Daily build finished ===")


if __name__ == "__main__":
    main()
