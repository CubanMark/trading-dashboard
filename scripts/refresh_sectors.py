"""
Monthly sector/industry refresh.

Fetches current sector + industry names from yfinance.info for all active
universe tickers and overwrites stale GICS-style names in the DB.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.db import connect, init_schema
from data.universe import refresh_sector_industry
from data.quality import log_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("=== Monthly sector refresh started ===")
    init_schema()

    with connect() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM universe WHERE active=1"
        ).fetchone()[0]

        if active == 0:
            logger.error("Universe is empty — run main.py first to seed the universe")
            sys.exit(1)

        logger.info("Refreshing sector/industry for %d active tickers …", active)
        stats = refresh_sector_industry(conn)
        log_run(
            conn,
            "monthly_sector_refresh",
            "ok",
            f"updated={stats['updated']} skipped={stats['skipped']} errors={stats['errors']}",
        )
        logger.info(
            "Sector refresh complete — updated: %d  skipped: %d  errors: %d",
            stats["updated"], stats["skipped"], stats["errors"],
        )

    logger.info("=== Monthly sector refresh finished ===")


if __name__ == "__main__":
    main()
