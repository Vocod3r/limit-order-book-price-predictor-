"""
Real client bid intake - the actual replacement for Test_bid_generation.py.
A logged-in client's frontend/app calls POST /bids with their real bid;
this constructs a RawBid exactly like the test generator did, but from a
real HTTP request instead of random numbers.

Run:
    pip install flask --break-system-packages
    py bid_api.py <odoo_url> <odoo_db> <odoo_username> <odoo_api_key>

Client-side usage:
    POST /bids
    {
      "buyer": "Acme Capital",
      "buyer_email": "trading@acmecapital.com",
      "stock_id": "AAPL_STOCK",
      "price": 185.40,
      "quantity": 300
    }

buyer identity should really come from your own auth/session layer
(e.g. the logged-in user's name/id), not a free-text field a client can
fake - swap request.json["buyer"] for your session's real user identity
once you have real auth wired up.

---------------------------------------------------------------------------
Routes added to actually connect the React frontend end-to-end:

  GET  /bids?stock_id=...           list resting (unpromoted) bids
  GET  /asks?stock_id=...           current best ask (single level - see
                                     ask_book.py's own limitation, not
                                     re-implemented here)
  GET  /diagnostics/:stock_id       latest LOB_ssm regime/imbalance/events
  GET  /auction/:stock_id/winner    LIVE PREVIEW of who'd win right now,
                                     against the ask book's current price -
                                     read-only, does not touch Odoo. This is
                                     what the frontend polls continuously
                                     while bids are still being accepted.
  POST /auction/:stock_id/end       ENDS the auction: re-checks the ask book
                                     one more time (optionally verifying it
                                     matches what the caller last saw),
                                     determines the winner(s), promotes them
                                     into Odoo (crm.lead -> opportunity ->
                                     confirmed sale.order) via
                                     promote_winners.py, invoices +
                                     emails each winner automatically, and
                                     depletes the ask book by the filled
                                     quantity.
---------------------------------------------------------------------------
"""

import os
import re
import sys
import xmlrpc.client
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

from bid_ingestion import RawBid, validate_bids, BidIngestionClient, OdooConfig
from finnhub_market_data import BrokerMarketData, BrokerConfig
from ask_book import AskBook
from LOB_ssm import (
    fetch_bids_from_odoo,
    run_on_odoo_bids_and_asks,
    find_highest_bid_from_odoo,
)
from promote_winners import WinnerPromotionClient
from Accounting import AccountingClient

app = Flask(__name__)
CORS(app, origins=["https://lobbasedauctioningpipeline.vercel.app"])

FINNHUB_API_KEY = "d9biu5hr01qv2lms14bgd9biu5hr01qv2lms14c0"
STOCK_SYMBOL_MAP = {"AAPL_STOCK": "AAPL", "TSLA_STOCK": "TSLA", "MSFT_STOCK": "MSFT"}
bmd = BrokerMarketData(BrokerConfig(api_key=FINNHUB_API_KEY))
ask_book = AskBook()

# Fallbacks only used when a stock has never had an ask posted yet -
# mirrors run_pipeline.py's own FALLBACK_ASK_PRICE / TOTAL_COMMITTED so
# diagnostics don't hard-fail before a seller has set anything up.
FALLBACK_ASK_PRICE = 150.00
FALLBACK_ASK_QTY = 1000


@app.route("/")
def frontend():
    return send_from_directory(".", "trading_desk.html")


@app.route("/listings")
def listings():
    """One card per stock currently sitting in the ask book - the
    'browsable auctions' grid."""
    out = []
    for stock_id, entry in ask_book._data.items():
        out.append({
            "stock_id": stock_id,
            "ask_price": entry["price"],
            "ask_qty": entry["qty"],
            "highest_bid": _highest_registered_bid(stock_id),
        })
    return jsonify(out)


@app.route("/bids", methods=["GET", "POST"])
def bids_collection():
    if request.method == "GET":
        return _list_bids()
    return _submit_bid()


def _list_bids():
    """Resting bids for a stock - i.e. still crm.lead type='lead', not yet
    promoted to an opportunity by a previous auction. Powers BookPanel's
    bid ladder."""
    stock_id = request.args.get("stock_id")
    if not stock_id:
        return jsonify({"status": "error", "error": "stock_id query param required"}), 400

    try:
        raw_bids = fetch_bids_from_odoo(odoo_config, stock_id)
    except RuntimeError as e:
        return jsonify({"status": "error", "error": str(e)}), 502

    rows = [
        {"id": f"bid-{b.lead_id}", "buyer": b.buyer, "price": b.price, "quantity": b.quantity}
        for b in raw_bids
    ]
    return jsonify(rows)


def _submit_bid():
    data = request.get_json(force=True)
    try:
        raw_email = data.get("buyer_email")
        bid = RawBid(
            buyer=str(data["buyer"]),
            stock_id=str(data["stock_id"]),
            price=float(data["price"]),
            quantity=float(data["quantity"]),
            timestamp=datetime.now(),
            buyer_email=str(raw_email).strip() or None if raw_email else None,
        )
    except (KeyError, ValueError) as e:
        return jsonify({"status": "error", "error": f"invalid bid payload: {e}"}), 400

    symbol = STOCK_SYMBOL_MAP.get(bid.stock_id, "AAPL")
    tick_size = bmd.get_instrument_meta(symbol)["tick_size"]
    market_open = True  # TESTING ONLY - bypasses real Finnhub market-hours check

    valid_bids = validate_bids([bid], tick_size=tick_size, market_open=market_open)
    if not valid_bids:
        reason = "market is closed" if not market_open else "failed tick-size validation"
        return jsonify({"status": "rejected", "reason": reason}), 422

    result = ingestion_client.register_bid(valid_bids[0])

    # tell the buyer where they stand right now, relative to other
    # currently-registered bids for this stock (not the final auction
    # result - that only resolves when the auction is ended)
    current_high = _highest_registered_bid(bid.stock_id)
    outbid = current_high is not None and current_high > bid.price
    result["outbid"] = outbid
    result["current_highest_bid"] = current_high
    return jsonify(result), (201 if result["status"] == "created" else 200)


@app.route("/asks", methods=["GET", "POST"])
def asks_collection():
    if request.method == "GET":
        return _list_asks()
    return _submit_ask()


def _list_asks():
    """AskBook only tracks a single best-level ask per stock (see
    ask_book.py's own docstring) - so this is always at most one row, not
    a seller-by-seller ladder. Kept honest rather than faked."""
    stock_id = request.args.get("stock_id")
    if not stock_id:
        return jsonify({"status": "error", "error": "stock_id query param required"}), 400

    current = ask_book.get_current(stock_id)
    if current is None:
        return jsonify([])

    price, qty = current
    return jsonify([{"id": f"ask-{stock_id}", "seller": "Resting ask", "price": price, "quantity": qty}])


def _submit_ask():
    """Seller side: post/lower an ask price for a stock. Reuses the same
    AskBook Event 4 logic ask_book.py's CLI already drives."""
    data = request.get_json(force=True)
    try:
        stock_id = str(data["stock_id"])
        price = float(data["price"])
        quantity = float(data["quantity"])
    except (KeyError, ValueError) as e:
        return jsonify({"status": "error", "error": f"invalid ask payload: {e}"}), 400

    entry = ask_book.post_new_ask(stock_id, price, quantity)
    return jsonify({"status": "posted", "stock_id": stock_id,
                     "resulting_best_ask": entry["price"], "resulting_qty": entry["qty"]}), 201


@app.route("/diagnostics/<stock_id>")
def diagnostics(stock_id):
    """Latest LOB_ssm regime/imbalance/event read for this stock. Returns
    a zeroed-out shape (not an error) when there's no data yet, so the
    frontend doesn't need special-case handling for a brand-new stock."""
    current = ask_book.get_current(stock_id)
    ask_price = current[0] if current else FALLBACK_ASK_PRICE
    ask_qty = current[1] if current else FALLBACK_ASK_QTY
    ask_events = ask_book.get_events(stock_id)

    try:
        feature_rows = run_on_odoo_bids_and_asks(
            odoo_config, stock_id, ask_events,
            initial_ask_price=ask_price, initial_ask_qty=ask_qty,
        )
    except ValueError:
        feature_rows = []  # no bids registered yet for this stock

    if not feature_rows:
        return jsonify({
            "regime": {"stable": 0, "bid_depleting": 0, "ask_depleting": 0},
            "queue_imbalance": 0,
            "events": {
                "ask_depleted": False, "new_higher_bid": False,
                "bid_depleted": False, "new_lower_ask": False,
            },
            "mid_price": None,
            "spread": None,
        })

    latest = feature_rows[-1]
    return jsonify({
        "regime": {
            "stable": latest["p_stable"],
            "bid_depleting": latest["p_bid_depleting"],
            "ask_depleting": latest["p_ask_depleting"],
        },
        "queue_imbalance": latest["queue_imbalance"],
        "events": {
            "ask_depleted": bool(latest.get("event_ask_depleted", False)),
            "new_higher_bid": bool(latest.get("event_new_higher_bid", False)),
            "bid_depleted": bool(latest.get("event_bid_depleted", False)),
            "new_lower_ask": bool(latest.get("event_new_lower_ask", False)),
        },
        "mid_price": latest["mid_price"],
        "spread": latest["spread"],
    })


def _serialize_winner(w):
    return {
        "buyer": w.buyer,
        "price": w.price,
        "quantity": w.quantity,
        "timestamp": w.timestamp.isoformat(),
        "lead_id": w.lead_id,
    }


def _send_invoice_link_email(invoice_id: int, to_email: str):
    """Emails a direct link to the posted invoice in Odoo - same approach
    as run_pipeline.py's send_invoice_pdf_email, just reusing this
    module's already-authenticated models_client/uid instead of opening a
    fresh xmlrpc connection."""
    invoice_url = f"{odoo_config.url}/web#id={invoice_id}&model=account.move&view_type=form"
    mail_id = models_client.execute_kw(odoo_config.db, uid, odoo_config.api_key, "mail.mail", "create", [{
        "subject": f"Invoice #{invoice_id}",
        "body_html": f'Your invoice is ready: <a href="{invoice_url}">{invoice_url}</a>',
        "email_to": to_email,
    }])
    try:
        models_client.execute_kw(odoo_config.db, uid, odoo_config.api_key, "mail.mail", "send", [[mail_id]])
    except xmlrpc.client.Fault as e:
        if "cannot marshal None" not in str(e):
            raise


def _sale_order_for_lead(lead_id: int):
    """Finds the sale.order for a lead that was ALREADY promoted on a
    previous attempt (status == 'already_promoted' from promote_winners.py,
    which - unlike a fresh 'promoted' result - doesn't carry sale_order_id
    at all). Without this, retrying "End auction" on a stock that was
    already promoted once silently skips invoicing for that winner, since
    there'd be no sale_order_id to invoice against."""
    orders = models_client.execute_kw(
        odoo_config.db, uid, odoo_config.api_key, "sale.order", "search",
        [[("opportunity_id", "=", lead_id)]],
    )
    return orders[0] if orders else None


def _lead_partner_email(lead_id: int):
    """The winner's real email, if one's on file - i.e. res.partner.email,
    which bid_ingestion.py's ensure_partner() now sets from buyer_email at
    bid time. Returns None if the bidder never supplied one."""
    lead = models_client.execute_kw(
        odoo_config.db, uid, odoo_config.api_key, "crm.lead", "read", [lead_id], {"fields": ["partner_id"]},
    )
    if not lead or not lead[0].get("partner_id"):
        return None
    partner_id = lead[0]["partner_id"][0]
    partner = models_client.execute_kw(
        odoo_config.db, uid, odoo_config.api_key, "res.partner", "read", [partner_id], {"fields": ["email"]},
    )
    return partner[0].get("email") if partner else None


@app.route("/auction/<stock_id>/winner")
def auction_winner_preview(stock_id):
    """Read-only, real-time preview of who'd win right now against the
    CURRENT ask book - safe to poll continuously while bids are still
    open. Never writes to Odoo, never touches the ask book. This is
    distinct from POST /auction/:id/end, which actually executes it."""
    current = ask_book.get_current(stock_id)
    if current is None:
        return jsonify({"status": "no_ask", "winners": [], "total_qty_filled": 0, "qty_unfilled": 0})

    ask_price, ask_qty = current
    try:
        result = find_highest_bid_from_odoo(odoo_config, stock_id, ask_price, ask_qty)
    except ValueError:
        return jsonify({
            "status": "no_bids", "ask_price": ask_price, "ask_qty": ask_qty,
            "winners": [], "total_qty_filled": 0, "qty_unfilled": ask_qty,
        })

    return jsonify({
        "status": "preview",
        "ask_price": result["ask_price"],
        "ask_qty": result["ask_qty"],
        "highest_bid_price": result["highest_bid_price"],
        "highest_bid_buyer": result["highest_bid_buyer"],
        "winners": [_serialize_winner(w) for w in result["winners"]],
        "total_qty_filled": result["total_qty_filled"],
        "qty_unfilled": result["qty_unfilled"],
    })


@app.route("/auction/<stock_id>/end", methods=["POST"])
def auction_end(stock_id):
    """
    Actually ends the auction for stock_id:
      1. Re-reads the ask book RIGHT NOW (the "latest ask" check).
      2. If the caller passed expected_ask_price (from the last preview
         they saw), verifies it still matches - rejects with 409 if a
         seller changed the ask out from under a running auction, rather
         than silently awarding shares at a price the buyer never saw.
      3. Determines the winner(s) at that ask price (LOB_ssm, read-only).
      4. Promotes winner(s) into Odoo: lead -> opportunity -> confirmed
         sale.order (promote_winners.py - already-working sales flow).
      5. Depletes the ask book by the filled quantity, so the book
         reflects the trade and diagnostics see a real Event 1 if it's
         now empty.
    Invoicing (Accounting.py) now runs automatically for each promoted
    winner, right here - creates + posts the invoice, registers payment,
    and emails a link to the invoice using that winner's real email
    (captured at bid time via buyer_email, see bid_ingestion.py). If a
    winner never supplied an email, their invoice is still created and
    posted, just not emailed - check the "invoicing" list in the response.
    Also correctly handles a RETRY of a stock whose winner was already
    promoted on a previous attempt (see _sale_order_for_lead below) -
    without that, invoicing would silently be skipped on any retry.
    """
    current = ask_book.get_current(stock_id)
    if current is None:
        return jsonify({"status": "error", "error": f"No ask has been posted for {stock_id!r} yet."}), 400

    current_ask_price, current_ask_qty = current

    data = request.get_json(silent=True) or {}
    expected_ask_price = data.get("expected_ask_price")
    if expected_ask_price is not None and abs(float(expected_ask_price) - current_ask_price) > 1e-9:
        return jsonify({
            "status": "ask_price_changed",
            "error": "The ask price changed since you last checked - refresh and try again.",
            "expected_ask_price": float(expected_ask_price),
            "current_ask_price": current_ask_price,
        }), 409

    try:
        result = find_highest_bid_from_odoo(odoo_config, stock_id, current_ask_price, current_ask_qty)
    except ValueError as e:
        return jsonify({"status": "no_bids", "error": str(e)}), 200

    winners = result["winners"]
    if not winners:
        return jsonify({
            "status": "no_winners",
            "ask_price": current_ask_price, "ask_qty": current_ask_qty,
            "winners": [], "total_qty_filled": 0, "qty_unfilled": current_ask_qty,
        })

    promotion_client = WinnerPromotionClient(odoo_config, dry_run=False)
    promotion = promotion_client.promote_all(winners, stock_id)

    # Invoice + email each winner. Handles both a fresh promotion this
    # round AND a winner who was already promoted on an earlier attempt
    # (e.g. you clicked "End auction" before, and are retrying) - in the
    # latter case promote_winners.py doesn't hand back sale_order_id, so
    # we look it up ourselves rather than silently skipping invoicing.
    accounting_client = AccountingClient(odoo_config, dry_run=False)
    invoicing = []
    for r in promotion["results"]:
        if r["status"] == "promoted":
            sale_order_id = r["sale_order_id"]
        elif r["status"] == "already_promoted":
            sale_order_id = _sale_order_for_lead(r["lead_id"])
            if sale_order_id is None:
                invoicing.append({"lead_id": r["lead_id"], "error": "already promoted, but no sale.order found for it"})
                continue
        else:
            continue  # status == "error" - nothing to invoice

        entry = {"lead_id": r["lead_id"], "sale_order_id": sale_order_id}
        try:
            settlement = accounting_client.settle_sale_order(sale_order_id)
            entry["invoice_status"] = settlement["invoice"]["status"]
            entry["payment_status"] = settlement["payment"]["status"] if settlement["payment"] else "skipped"

            if settlement["invoice"]["status"] == "posted":
                invoice_id = settlement["invoice"]["invoice_id"]
                entry["invoice_id"] = invoice_id
                email = _lead_partner_email(r["lead_id"])
                if email:
                    _send_invoice_link_email(invoice_id, email)
                    entry["emailed_to"] = email
                else:
                    entry["email_skipped"] = "no email on file for this bidder"
            elif settlement["invoice"]["status"] == "error":
                entry["invoice_error"] = settlement["invoice"]["error"]
        except Exception as e:
            entry["error"] = str(e)
        invoicing.append(entry)

    filled = result["total_qty_filled"]
    if filled > 0:
        ask_book.deplete(stock_id, filled)

    return jsonify({
        "status": "executed",
        "ask_price": current_ask_price,
        "winners": [_serialize_winner(w) for w in winners],
        "total_qty_filled": filled,
        "qty_unfilled": result["qty_unfilled"],
        "promotion_summary": promotion["summary"],
        "invoicing": invoicing,
    })


def _highest_registered_bid(stock_id: str):
    """Highest price among bids already registered in Odoo CRM for this
    stock - used to tell a buyer if they've been outbid so far. Prices
    live inside crm.lead's free-text description field (bid_ingestion.py
    doesn't store price as its own field), so this parses them back out."""
    leads = models_client.execute_kw(
        odoo_config.db, uid, odoo_config.api_key, "crm.lead", "search_read",
        [[("name", "like", f"Bid - {stock_id} @")]], {"fields": ["description"]},
    )
    prices = []
    for lead in leads:
        m = re.search(r"price=([\d.]+)", lead.get("description") or "")
        if m:
            prices.append(float(m.group(1)))
    return max(prices) if prices else None


# ---------------------------------------------------------------------------
# Odoo connection setup - runs at IMPORT time (not just inside __main__) so
# gunicorn (`gunicorn Test_bid_generation:app`) can find `app` fully wired
# up without ever calling __main__. Prefers env vars (what real hosts give
# you); falls back to the original CLI-args usage for local dev so
# `py Test_bid_generation.py <url> <db> <user> <key>` still works unchanged.
# ---------------------------------------------------------------------------
if len(sys.argv) == 5:
    url, db, username, api_key = sys.argv[1:5]
elif all(os.environ.get(k) for k in ("ODOO_URL", "ODOO_DB", "ODOO_USERNAME", "ODOO_API_KEY")):
    url = os.environ["ODOO_URL"]
    db = os.environ["ODOO_DB"]
    username = os.environ["ODOO_USERNAME"]
    api_key = os.environ["ODOO_API_KEY"]
else:
    print("Missing Odoo credentials. Either run:")
    print("  py Test_bid_generation.py <odoo_url> <odoo_db> <odoo_username> <odoo_api_key>")
    print("or set ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_API_KEY as environment variables.")
    sys.exit(1)

odoo_config = OdooConfig(url=url, db=db, username=username, api_key=api_key)
ingestion_client = BidIngestionClient(odoo_config, dry_run=False)

_common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
uid = _common.authenticate(db, username, api_key, {})
models_client = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

if __name__ == "__main__":
    # Local dev only - production hosts run this via gunicorn instead,
    # which imports `app` directly and never executes this block.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5051)))