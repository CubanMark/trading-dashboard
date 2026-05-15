import logging
import os
from datetime import date
from pathlib import Path

import pandas as pd

from data.db import connect, init_schema
from data import universe as uni
from data.universe import seed_sp400_sp600, needs_sector_refresh, refresh_sector_industry
from data import loader
from data.quality import log_run
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
        # --- Step 0: one-time migration — deactivate old dot-notation tickers ---
        dot_count = conn.execute(
            "SELECT COUNT(*) FROM universe WHERE ticker LIKE '%.%' AND active=1"
        ).fetchone()[0]
        if dot_count > 0:
            conn.execute("UPDATE universe SET active=0 WHERE ticker LIKE '%.%'")
            conn.commit()
            logger.info("Step 0: deactivated %d dot-notation tickers", dot_count)
            log_run(conn, "step0_migration", "ok",
                    f"Deactivated {dot_count} dot-notation tickers")

        # --- Step 0a: one-time backfill — set scanner_label where NULL ---
        null_labels = conn.execute(
            "SELECT COUNT(*) FROM scanner_hits WHERE scanner_label IS NULL OR scanner_label = ''"
        ).fetchone()[0]
        if null_labels > 0:
            conn.execute("""
                UPDATE scanner_hits SET scanner_label = CASE scanner
                    WHEN 'pullback_ma20' THEN 'MA20 Pullback'
                    WHEN 'pullback_ma10' THEN 'MA10 Pullback'
                    WHEN 'pullback_3d'   THEN '3D Pullback'
                    ELSE scanner
                END
                WHERE scanner_label IS NULL OR scanner_label = ''
            """)
            conn.commit()
            logger.info("Step 0a: backfilled scanner_label for %d rows", null_labels)
            log_run(conn, "step0a_label_backfill", "ok",
                    f"Backfilled scanner_label for {null_labels} rows")

        # --- Step 0b: one-time sector refresh — replace GICS names with Yahoo Finance names ---
        if needs_sector_refresh(conn):
            logger.info("Step 0b: GICS-style sector names detected — refreshing from yfinance.info …")
            stats = refresh_sector_industry(conn)
            log_run(conn, "step0b_sector_refresh", "ok",
                    f"Sector refresh: updated={stats['updated']} skipped={stats['skipped']} errors={stats['errors']}")

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
        log_run(conn, "step1_prices", "ok", "Prices and macro updated")

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
        log_run(conn, "step3_breadth", "ok", f"Breadth computed for {today}")

        # --- Step 4: Run scanners ---
        logger.info("Step 4: loading prices for scanner …")
        universe_df = uni.load(conn)
        meta_map = (
            universe_df.set_index("ticker")[["gics_sector", "gics_industry", "gics_sub_industry"]]
            .to_dict("index")
        )

        price_dfs = loader.load_price_dfs(conn, universe_df["ticker"].tolist(), rows=400)
        logger.info("Loaded prices for %d tickers", len(price_dfs))

        # SPY from macro_series for RS computation + regime check
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

        # Run all 3 pullback scanner variants with regime filter
        scan_result = pb.scan_universe(
            price_dfs, today, spy_close=spy_close, meta_map=meta_map
        )

        # Clear all previous hits so only today's results remain in the table.
        deleted = conn.execute("DELETE FROM scanner_hits").rowcount
        conn.commit()
        if deleted:
            logger.info("Cleared %d stale scanner hits", deleted)

        hit_rows: list[dict] = []
        if scan_result["regime"] == "bear":
            logger.info("Pullback scanner suspended — bear regime (SPY < SMA200)")
            log_run(conn, "step4_scanner", "ok", "Bear regime — scanner suspended")
        else:
            scanner_specs = [
                ("pullback_ma20", "MA20 Pullback"),
                ("pullback_ma10", "MA10 Pullback"),
                ("pullback_3d",   "3D Pullback"),
            ]
            for scanner_id, scanner_label in scanner_specs:
                for ticker in scan_result["hits"].get(scanner_id, []):
                    df = price_dfs.get(ticker)
                    if df is None:
                        continue
                    meta = dict(meta_map.get(ticker, {}))
                    meta["rs_rank"]       = float(rs_ranks_s.get(ticker, 50.0))
                    meta["earnings_date"] = None
                    meta["scanner"]       = scanner_id
                    meta["scanner_label"] = scanner_label
                    meta["warning"]       = pb._WARNING
                    try:
                        hit_rows.append(pb.build_hit_row(ticker, df, today, meta))
                    except Exception as exc:
                        logger.warning("build_hit_row failed for %s: %s", ticker, exc)

            pb._annotate_overlaps(hit_rows)

            if hit_rows:
                conn.executemany(
                    """INSERT OR REPLACE INTO scanner_hits
                       (date, ticker, scanner, gics_sector, gics_industry, rs_rank,
                        perf_1m, adr_pct, atr, avg_volume, dist_52w_high, earnings_date,
                        scanner_label, also_in, warning, dist_ma_atr, dist_local_high_atr)
                       VALUES (:date, :ticker, :scanner, :gics_sector, :gics_industry, :rs_rank,
                               :perf_1m, :adr_pct, :atr, :avg_volume, :dist_52w_high, :earnings_date,
                               :scanner_label, :also_in, :warning, :dist_ma_atr, :dist_local_high_atr)""",
                    hit_rows,
                )
                conn.commit()
            total_by_variant = {k: len(v) for k, v in scan_result["hits"].items()}
            logger.info("Pullback scanner: %s on %s", total_by_variant, today)
            log_run(conn, "step4_scanner", "ok",
                    f"{len(hit_rows)} scanner hit rows for {today} ({total_by_variant})")

        # --- Step 5: Render HTML ---
        homepage.build(conn, today)
        logger.info("Homepage written to pages/index.html")
        log_run(conn, "step5_render", "ok", "Homepage built")

    logger.info("=== Daily build finished ===")


if __name__ == "__main__":
    main()
