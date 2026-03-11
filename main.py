"""
Polymarket Arbitrage Bot — Main Entry Point

Runs the main scan-evaluate-trade loop every SCAN_INTERVAL_SECONDS.

Usage:
    python main.py              # paper trading (default)
    python main.py --live       # live trading (Phase 3+, requires .env keys)

See blueprint Section 9.8 for full pseudocode.
"""

import argparse
import asyncio
import logging
import signal
import sys

from dotenv import load_dotenv

from config.settings import SCAN_INTERVAL_SECONDS
from src.client import PolymarketClient
from src.db import Database
from src.evaluator import OpportunityEvaluator
from src.risk import RiskManager
from src.scanner.logical import LogicalScanner
from src.scanner.rebalance import RebalanceScanner
from src.trader.paper import PaperTrader
from dashboard.terminal import Dashboard

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_running = True


def _handle_shutdown(sig, frame):
    global _running
    logger.info("Shutdown signal received, stopping...")
    _running = False


async def main(live: bool = False):
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    client = PolymarketClient()
    db = Database()
    risk = RiskManager(db)
    evaluator = OpportunityEvaluator(client)
    trader = PaperTrader(db)          # TODO: swap for LiveTrader in Phase 3
    dashboard = Dashboard()
    scanners = [RebalanceScanner(client), LogicalScanner(client)]

    logger.info("Bot started — mode=%s", "LIVE" if live else "PAPER")

    while _running:
        try:
            # 1. Fetch latest market data
            events = client.get_all_active_events()

            # 2. Run all scanners
            opportunities = []
            for scanner in scanners:
                opps = scanner.scan(events)
                opportunities.extend(opps)

            # 3. Evaluate and filter
            for opp in opportunities:
                db.save_opportunity(opp)

                approved, reason = evaluator.evaluate(opp)
                if not approved:
                    continue

                can, reason = risk.can_trade(opp)
                if not can:
                    logger.info("Risk blocked: %s", reason)
                    continue

                size = risk.calculate_position_size(opp)
                trade = await trader.execute(opp, size)
                risk.record_trade_result(trade)

            # 4. Update dashboard
            dashboard.update(opportunities, risk.get_status())

        except Exception as exc:
            logger.exception("Error in main loop: %s", exc)

        # 5. Wait for next cycle
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    logger.info("Bot stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Arbitrage Bot")
    parser.add_argument("--live", action="store_true", help="Enable live trading (Phase 3+)")
    args = parser.parse_args()
    asyncio.run(main(live=args.live))
