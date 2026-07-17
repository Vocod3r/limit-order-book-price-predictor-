"""
Orchestration: runs the full loop across all files, implementing Stage 8's
continuation logic - keep auctioning the same stock while shares remain,
otherwise move to the next stock in the queue.

    test_bid_generator.py  -->  bid_ingestion.py  -->  Odoo CRM
                                       ^
                              broker_market_data.py
                              (tick size, market hours)

    Odoo CRM  -->  lob_ssm.py  -->  SSM feature output
                        |
                        `-->  Stage 5 auction winner --> Stage 6 promotion
                                                              |
                                                              v
                                                   Stage 7/8 ledger update
                                                              |
                              remaining > 0 --------> loop back to Stage 1
                              (same stock)               (same stock)
                                                              |
                              remaining <= 0 -------> advance to next stock
                                                        in the queue

INVOICING IS MANUAL for now (Phase 5, accounting.py) - not called
automatically here. Real invoicing depends on your Odoo install's chart of
accounts/tax/journal configuration in ways that need hands-on setup before
automating; run settle_existing.py by hand once that's sorted out.

  Phase 1 - INGEST:  generate/receive bids, validate, write to Odoo.
  Phase 2 - ANALYZE: read the same bids back from Odoo, run the SSM.
  Phase 3 - DECIDE:  read the same bids again, determine the auction
                      winner against a given ask price/qty.
  Phase 4 - PROMOTE:  promote winners -> opportunities -> confirmed sales
                      orders. This also removes them from future Phase 3
                      queries automatically (fetch_bids_from_odoo only
                      reads type="lead"; promoted winners become type=
                      "opportunity" and drop out of the pool - so a
                      later cycle never re-awards an already-won bid).
  Phase 6 - LEDGER:   update the blanket order's committed/sold/remaining
                      shares (local ledger, not Odoo itself).
  Phase 8 - CONTINUE: shares_remaining > 0 -> loop back to Phase 1 for
                      the SAME stock. shares_remaining <= 0 -> the
                      blanket order is fulfilled, move to the next stock
                      in the queue.
"""

import base64
import xmlrpc.client
from datetime import datetime

from bid_ingestion import OdooConfig
from finnhub_market_data import BrokerMarketData, BrokerConfig
from LOB_ssm import (run_on_odoo_bids_and_asks, print_diagnostic_report,
                       print_event_conditioned_prediction,
                       find_highest_bid_from_odoo, OdooConfig as SSMOdooConfig)
from ask_book import AskBook
from promote_winners import WinnerPromotionClient
from Accounting import AccountingClient
from Blanket_order_ledger import BlanketOrderLedger


def phase2_analyze(stock_id: str, ssm_odoo_config: SSMOdooConfig,
                    initial_ask_price: float, initial_ask_qty: float):
    """
    The SSM's actual analysis: real bids from Odoo, real ask events from
    ask_book.py, merged and run through the IMM filter. Odoo plays NO
    role beyond being the source of the bid records - it doesn't gate or
    influence this analysis, and this function doesn't write anything
    back to Odoo. Odoo's role is purely to execute/record the auction
    outcome later (Stages 5-8) - the SSM's diagnostic judgment happens
    entirely here, independent of Odoo.
    """
    print(f"=== Phase 2: SSM diagnostic analysis for {stock_id} ===")

    ask_book = AskBook()
    if ask_book.get_current(stock_id) is None:
        print(f"No ask book entries yet for {stock_id} - post one with: "
              f"py ask_book.py {stock_id} <price> <qty>")
        print(f"Falling back to a flat placeholder ask ({initial_ask_price}/{initial_ask_qty}) for now.\n")
        ask_events = []
    else:
        ask_events = ask_book.get_events(stock_id)

    feature_rows = run_on_odoo_bids_and_asks(
        ssm_odoo_config, stock_id, ask_events,
        initial_ask_price=initial_ask_price, initial_ask_qty=initial_ask_qty,
    )
    print_diagnostic_report(stock_id, feature_rows)
    print_event_conditioned_prediction(stock_id, feature_rows)
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


def phase5_settle_accounting(promotion_result: dict, ssm_odoo_config: SSMOdooConfig,
                              dry_run: bool = True, to_email: str = None):
    """
    Invoice + post + register payment for each sale order Phase 4 just
    promoted. Reads sale_order_id straight from Phase 4's own results -
    no re-querying Odoo to figure out which orders need settling.
    """
    print("=== Phase 5: settling accounting (invoice + payment) ===")
    if not promotion_result:
        print("No promotion result to settle against.\n")
        return []

    promoted = [r for r in promotion_result["results"] if r["status"] == "promoted"]
    if not promoted:
        print("Nothing was newly promoted this run - nothing to settle.\n")
        return []

    client = AccountingClient(ssm_odoo_config, dry_run=dry_run)
    settlements = []
    for r in promoted:
        settlement = client.settle_sale_order(r["sale_order_id"])
        settlements.append(settlement)
        print(f"  sale_order_id={r['sale_order_id']}: "
              f"invoice={settlement['invoice']['status']}, "
              f"payment={settlement['payment']['status'] if settlement['payment'] else 'skipped'}")
        if settlement["invoice"]["status"] == "posted" and to_email and not dry_run:
            send_invoice_pdf_email(ssm_odoo_config, settlement["invoice"]["invoice_id"], to_email)
        if settlement["invoice"]["status"] == "error":
            print(f"    invoice error: {settlement['invoice']['error']}")
        if settlement["payment"] and settlement["payment"]["status"] == "error":
            print(f"    payment error: {settlement['payment']['error']}")
    print()
    return settlements


def phase6_update_blanket_order(stock_id: str, winners: list, total_committed: float):
    """
    Updates the local blanket-order ledger with this round's fills.
    total_committed should be the blanket agreement's total size - pass
    the SAME value every time you call this for a given stock_id, since
    initialize() is idempotent (it won't reset an existing ledger).
    """
    print(f"=== Phase 6: updating blanket order ledger for {stock_id} ===")
    if not winners:
        print("No winners this round - ledger unchanged.\n")
        return None

    ledger = BlanketOrderLedger()
    if ledger.get_state(stock_id) is None:
        ledger.initialize(stock_id, total_committed=total_committed)
        print(f"Initialized blanket order: total_committed={total_committed}")

    state = None
    for w in winners:
        state = ledger.record_sale(stock_id, w.quantity)

    print(f"shares_sold={state.shares_sold:.0f}  "
          f"shares_remaining={state.shares_remaining:.0f}  "
          f"fulfilled={state.is_fulfilled}\n")
    return state


def phase8_inventory_decision(state) -> str:
    """
    Stage 8: shares_remaining > 0 -> keep auctioning the SAME stock
    (return to Stage 1). shares_remaining <= 0 -> blanket order is
    fulfilled, close it out and move to the next stock in the queue.
    """
    if state is None:
        print("=== Phase 8: no ledger state (no winners this cycle) - "
              "retrying same stock next cycle ===\n")
        return "continue_same_stock"

    if state.shares_remaining > 0:
        print(f"=== Phase 8: {state.shares_remaining:.0f} shares remaining for "
              f"{state.stock_id} - continuing same stock ===\n")
        return "continue_same_stock"
    else:
        print(f"=== Phase 8: blanket order for {state.stock_id} fully filled - "
              f"moving to next stock ===\n")
        return "move_to_next_stock"


def _fetch_invoice_pdf(config: OdooConfig, invoice_id: int) -> bytes:
    """Fetches the rendered invoice PDF over plain HTTP, using a real web
    session (cookie-based login) - ir.actions.report._render_qweb_pdf is a
    private method and Odoo 19 blocks calling it over XML-RPC, and there's
    no public XML-RPC equivalent, so the report controller is the only
    remaining route."""
    import json
    import urllib.request
    import http.cookiejar

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    auth_payload = json.dumps({
        "jsonrpc": "2.0", "method": "call",
        "params": {"db": config.db, "login": config.username, "password": config.api_key},
    }).encode()
    auth_req = urllib.request.Request(
        f"{config.url}/web/session/authenticate", data=auth_payload,
        headers={"Content-Type": "application/json"},
    )
    opener.open(auth_req).read()  # populates cookie_jar with the session cookie

    pdf_req = urllib.request.Request(f"{config.url}/report/pdf/account.report_invoice/{invoice_id}")
    return opener.open(pdf_req).read()


def send_invoice_pdf_email(config: OdooConfig, invoice_id: int, to_email: str):
    """Renders the actual invoice PDF via Odoo's report engine and emails
    it as an attachment - not just a text notification."""
    common = xmlrpc.client.ServerProxy(f"{config.url}/xmlrpc/2/common")
    uid = common.authenticate(config.db, config.username, config.api_key, {})
    models = xmlrpc.client.ServerProxy(f"{config.url}/xmlrpc/2/object")

    pdf_bytes = _fetch_invoice_pdf(config, invoice_id)

    attachment_id = models.execute_kw(config.db, uid, config.api_key, "ir.attachment", "create", [{
        "name": f"Invoice_{invoice_id}.pdf",
        "type": "binary",
        "datas": base64.b64encode(pdf_bytes).decode(),
        "res_model": "account.move",
        "res_id": invoice_id,
        "mimetype": "application/pdf",
    }])

    mail_id = models.execute_kw(config.db, uid, config.api_key, "mail.mail", "create", [{
        "subject": f"Invoice #{invoice_id}",
        "body_html": "Please find your invoice attached.",
        "email_to": to_email,
        "attachment_ids": [(6, 0, [attachment_id])],
    }])
    try:
        models.execute_kw(config.db, uid, config.api_key, "mail.mail", "send", [[mail_id]])
    except xmlrpc.client.Fault as e:
        if "cannot marshal None" not in str(e):
            raise
    print(f"  Invoice PDF for invoice_id={invoice_id} emailed to {to_email}.")


def send_completion_email(config: OdooConfig, to_email: str, invoiced_count: int, stock_queue: list):
    """Sends a plain confirmation email via Odoo's own outgoing mail server
    (mail.mail) once the whole pipeline run is done."""
    common = xmlrpc.client.ServerProxy(f"{config.url}/xmlrpc/2/common")
    uid = common.authenticate(config.db, config.username, config.api_key, {})
    models = xmlrpc.client.ServerProxy(f"{config.url}/xmlrpc/2/object")
    body = (f"Pipeline run complete.<br/>Stocks processed: {', '.join(stock_queue)}<br/>"
            f"Invoices settled this run: {invoiced_count}")
    mail_id = models.execute_kw(config.db, uid, config.api_key, "mail.mail", "create", [{
        "subject": "Auction pipeline run complete",
        "body_html": body,
        "email_to": to_email,
    }])
    try:
        models.execute_kw(config.db, uid, config.api_key, "mail.mail", "send", [[mail_id]])
    except xmlrpc.client.Fault as e:
        if "cannot marshal None" not in str(e):
            raise
        # send() succeeded server-side; only the RPC response (None) failed
        # to serialize back - safe to ignore.
    print(f"\nCompletion email sent to {to_email}.")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 5:
        print("Usage: python3 run_pipeline.py <odoo_url> <odoo_db> <odoo_username> <odoo_api_key> "
              "[--to recipient@example.com] <stock_id_1> [stock_id_2] ...")
        print("Example (against mock_odoo_server.py): "
              "python3 run_pipeline.py http://localhost:8069 fakedb fakeuser fakekey STOCK_A STOCK_B")
        sys.exit(1)

    url, db, username, api_key = sys.argv[1:5]
    remaining_args = sys.argv[5:]
    RECIPIENT_EMAIL = "your.gmail@gmail.com"  # default - overridden by --to below
    if remaining_args[:1] == ["--to"]:
        RECIPIENT_EMAIL = remaining_args[1]
        remaining_args = remaining_args[2:]
    stock_queue = remaining_args if remaining_args else ["MOCK_STOCK"]

    odoo_config = OdooConfig(url=url, db=db, username=username, api_key=api_key)
    ssm_odoo_config = SSMOdooConfig(url=url, db=db, username=username, api_key=api_key)

    FINNHUB_API_KEY = "d9biu5hr01qv2lms14bgd9biu5hr01qv2lms14c0"
    STOCK_SYMBOL_MAP = {"AAPL_STOCK": "AAPL", "TSLA_STOCK": "TSLA", "MSFT_STOCK": "MSFT"}
    bmd = BrokerMarketData(BrokerConfig(api_key=FINNHUB_API_KEY))
    FALLBACK_ASK_PRICE = 150.00  # used only if the week-average call fails
    TOTAL_COMMITTED = 1000     # blanket order size per stock, for this demo
    MAX_CYCLES_PER_STOCK = 5   # safety cap so a stock with no clearing bids
                               # doesn't loop forever

    ledger = BlanketOrderLedger()
    invoiced_count = 0

    for stock_id in stock_queue:
        print(f"\n{'#'*70}\n# STARTING STOCK: {stock_id}\n{'#'*70}\n")

        symbol = STOCK_SYMBOL_MAP.get(stock_id, "AAPL")
        try:
            ASK_PRICE = bmd.get_week_average_price(symbol)
            print(f"Ask price for {stock_id}: {ASK_PRICE:.2f} "
                  f"(7-day average close for {symbol})")
        except Exception as e:
            ASK_PRICE = FALLBACK_ASK_PRICE
            print(f"Could not fetch week-average price for {symbol} ({e}) - "
                  f"falling back to placeholder {ASK_PRICE}")

        for cycle in range(1, MAX_CYCLES_PER_STOCK + 1):
            print(f"--- cycle {cycle} for {stock_id} ---")

            # Stage 1 (ingestion) now happens externally via bid_api.py -
            # real clients submit bids there and they land straight in
            # Odoo CRM. This loop starts from Stage 2, reading whatever's
            # already been ingested for this stock_id so far.

            # Stage 2: SSM analysis (informational - doesn't affect the
            # winner decision below)
            phase2_analyze(stock_id, ssm_odoo_config,
                            initial_ask_price=ASK_PRICE, initial_ask_qty=TOTAL_COMMITTED)

            # How many shares are left on this stock's blanket order?
            existing_state = ledger.get_state(stock_id)
            ask_qty_this_round = (
                existing_state.shares_remaining if existing_state is not None
                else TOTAL_COMMITTED
            )

            # Use the REAL ask book price if one's been posted (matches
            # Phase 2's data source) - falls back to the placeholder only
            # if no ask has ever been posted for this stock.
            ask_book = AskBook()
            current_ask = ask_book.get_current(stock_id)
            ask_price_this_round = current_ask[0] if current_ask else ASK_PRICE

            # Stage 3: find this cycle's winner(s) against the remaining ask qty
            result = phase3_find_winner(stock_id, ssm_odoo_config,
                                         ask_price=ask_price_this_round, ask_qty=ask_qty_this_round)

            if not result["winners"]:
                print(f"No bids cleared the ask this cycle for {stock_id}.\n")
                state = ledger.get_state(stock_id)  # unchanged
                decision = phase8_inventory_decision(state)
                if decision == "move_to_next_stock":
                    break
                continue  # try another cycle, same stock

            # Stage 4/6: promote winners, update the ledger
            promotion_result = phase4_promote_winners(
                stock_id, ssm_odoo_config, result["winners"], dry_run=False
            )
            phase5_settle_accounting(promotion_result, ssm_odoo_config, dry_run=False, to_email=RECIPIENT_EMAIL)
            invoiced_count += sum(1 for r in promotion_result["results"] if r["status"] == "promoted")
            state = phase6_update_blanket_order(
                stock_id, result["winners"], total_committed=TOTAL_COMMITTED
            )

            # Stage 8: decide whether to keep going on this stock or move on
            decision = phase8_inventory_decision(state)
            if decision == "move_to_next_stock":
                break
        else:
            print(f"Hit MAX_CYCLES_PER_STOCK={MAX_CYCLES_PER_STOCK} for {stock_id} "
                  f"without fully filling the blanket order - moving on anyway.\n")

    print(f"\n{'#'*70}\n# ALL STOCKS IN QUEUE PROCESSED\n{'#'*70}")
    send_completion_email(odoo_config, to_email=RECIPIENT_EMAIL,
                           invoiced_count=invoiced_count, stock_queue=stock_queue)