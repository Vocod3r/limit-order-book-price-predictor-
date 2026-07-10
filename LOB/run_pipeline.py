"""
Orchestration: runs the full loop across all four files.

    test_bid_generator.py  -->  bid_ingestion.py  -->  Odoo CRM
                                       ^
                              broker_market_data.py
                              (tick size, market hours)

    Odoo CRM  -->  lob_ssm.py  -->  SSM feature output
                        |
                        `-->  Stage 5 auction winner

Three phases, run in sequence but each independently useful (matching the
earlier decision that the SSM must not gate what reaches Odoo):

  Phase 1 - INGEST:  generate/receive bids, validate, write to Odoo.
  Phase 2 - ANALYZE: read the same bids back from Odoo, run the SSM.
  Phase 3 - DECIDE:  read the same bids again, determine the auction
                      winner against a given ask price/qty. Read-only -
                      does not write a "winner" back to Odoo yet (that's
                      Stage 6, a separate future step).

In production, Phase 1 runs continuously (real client bids arriving), and
Phases 2/3 run on their own schedule (e.g. every N seconds, or on demand)
against whatever's accumulated in Odoo so far - neither needs Phase 1 to
have "just run".
"""

from datetime import datetime
import unittest.mock as mock

from Broker_Data import BrokerMarketData, BrokerConfig
from bid_ingestion import validate_bids, BidIngestionClient, OdooConfig
from Test_bid_generation import generate_random_bids  # dev/test only
from LOB_ssm import run_on_odoo_bids, find_highest_bid_from_odoo, OdooConfig as SSMOdooConfig
from promote_winners import WinnerPromotionClient


def phase1_ingest(stock_id: str, odoo_config: OdooConfig, dry_run: bool = True):
    """Bids -> validated -> written to Odoo CRM."""
    print(f"=== Phase 1: ingesting bids for {stock_id} ===")

    # In production, replace this with real bids arriving from your
    # platform's API/UI. Using the test generator here since we're not
    # connected to real client traffic in this run.
    # Fixed start_time so re-running with the same seed reproduces the
    # SAME bids (same bid_refs) every time - lets Odoo's dedup check
    # actually catch repeats instead of appending 60 new leads per run.
    # Remove this once real client bids replace the test generator.
    raw_bids = generate_random_bids(
        stock_id, n_bids=60, seed=5, start_time=datetime(2026, 7, 8, 5, 0, 0)
    )

    # Real tick size + market-hours check, from the broker adapter.
    # Mocked here since we don't have live broker credentials in this
    # environment - in production this hits the real API.
    with mock.patch("requests.get") as fake_get:
        fake_get.return_value.raise_for_status = lambda: None
        fake_get.return_value.json = lambda: {
            "NSE:ACME": {"tick_size": 0.05, "instrument_token": 1, "lot_size": 1}
        }
        bmd = BrokerMarketData(BrokerConfig(api_key="x", access_token="y"))
        tick_size = bmd.get_instrument_meta("NSE:ACME")["tick_size"]

    market_open = BrokerMarketData.is_market_open(datetime(2026, 7, 8, 11, 0))
    valid_bids = validate_bids(raw_bids, tick_size=tick_size, market_open=market_open)
    print(f"{len(raw_bids)} generated -> {len(valid_bids)} passed validation "
          f"(tick_size={tick_size}, market_open={market_open})")

    client = BidIngestionClient(odoo_config, dry_run=dry_run)
    result = client.register_batch(valid_bids)
    print(f"Odoo ingestion summary: {result['summary']}\n")
    return result


def phase2_analyze(stock_id: str, ssm_odoo_config: SSMOdooConfig,
                    initial_ask_price: float, initial_ask_qty: float):
    """Odoo CRM -> SSM features, independent of Phase 1's run."""
    print(f"=== Phase 2: running SSM on bids already in Odoo for {stock_id} ===")
    feature_rows = run_on_odoo_bids(
        ssm_odoo_config, stock_id,
        initial_ask_price=initial_ask_price, initial_ask_qty=initial_ask_qty,
    )
    print(f"SSM produced {len(feature_rows)} feature rows from the Odoo-sourced bid stream\n")
    for i, row in enumerate(feature_rows[:5], start=1):
        print(f"  tick {i}: mid={row['mid_price']:.3f}  imbalance={row['queue_imbalance']:.3f}  "
              f"P(bid_depleting)={row['p_bid_depleting']:.3f}")
    if len(feature_rows) > 5:
        print(f"  ... ({len(feature_rows) - 5} more)")
    return feature_rows


def phase3_find_winner(stock_id: str, ssm_odoo_config: SSMOdooConfig,
                        ask_price: float, ask_qty: float):
    """
    Odoo CRM -> auction winner, independent of Phase 2's SSM run - this
    reads the bid stream a second time and asks a different question
    (who wins?) than the SSM does (what regime is the market in?).

    ask_price/ask_qty are still placeholders here - see the note printed
    below. Once broker_market_data.py has real credentials, these should
    come from a live quote instead of being hardcoded.
    """
    print(f"=== Phase 3: determining auction winner for {stock_id} ===")
    print(f"(NOTE: ask_price={ask_price}, ask_qty={ask_qty} are still placeholders, "
          f"not a live broker quote - see broker_market_data.py)")

    result = find_highest_bid_from_odoo(ssm_odoo_config, stock_id, ask_price, ask_qty)

    print(f"Total bids seen: {result['total_bids_seen']}")
    print(f"Highest bid overall: {result['highest_bid_price']:.2f} "
          f"by {result['highest_bid_buyer']} at {result['highest_bid_timestamp']}")
    if result["winners"]:
        print(f"{len(result['winners'])} winner(s), "
              f"{result['total_qty_filled']:.0f} of {result['ask_qty']:.0f} shares filled:")
        for w in result["winners"]:
            print(f"  {w.buyer:<20} won {w.quantity:.0f} @ {w.price:.2f}  ({w.timestamp})")
    else:
        print("No bids cleared the ask price - no winner this round.")
    print()
    return result


def phase4_promote_winners(stock_id: str, ssm_odoo_config: SSMOdooConfig,
                            winners: list, dry_run: bool = True):
    """Promote each Phase 3 winner's lead -> opportunity -> confirmed sale.order."""
    print(f"=== Phase 4: promoting {len(winners)} winner(s) to Opportunities/Sales Orders ===")
    if not winners:
        print("No winners to promote.\n")
        return None

    client = WinnerPromotionClient(ssm_odoo_config, dry_run=dry_run)
    result = client.promote_all(winners, stock_id)
    print(f"Promotion summary: {result['summary']}\n")
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 5:
        print("Usage: python3 run_pipeline.py <odoo_url> <odoo_db> <odoo_username> <odoo_api_key> [stock_id]")
        print("Example (against mock_odoo_server.py): "
              "python3 run_pipeline.py http://localhost:8069 fakedb fakeuser fakekey MOCK_STOCK")
        sys.exit(1)

    url, db, username, api_key = sys.argv[1:5]
    stock_id = sys.argv[5] if len(sys.argv) > 5 else "MOCK_STOCK"

    odoo_config = OdooConfig(url=url, db=db, username=username, api_key=api_key)
    ssm_odoo_config = SSMOdooConfig(url=url, db=db, username=username, api_key=api_key)

    # Phase 1: real ingestion (dry_run=False actually writes to Odoo)
    phase1_ingest(stock_id, odoo_config, dry_run=False)

    # Phase 2: independent read-back + SSM analysis
    phase2_analyze(stock_id, ssm_odoo_config, initial_ask_price=45.02, initial_ask_qty=1000)

    # Phase 3: independent read-back + auction winner determination
    result = phase3_find_winner(stock_id, ssm_odoo_config, ask_price=45.02, ask_qty=1000)

    # Phase 4: promote winners -> opportunities -> confirmed sales orders
    # (dry_run=False here means it WILL write; flip to True to just preview)
    phase4_promote_winners(stock_id, ssm_odoo_config, result["winners"], dry_run=False)