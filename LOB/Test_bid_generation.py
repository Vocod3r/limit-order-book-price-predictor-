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
      "stock_id": "AAPL_STOCK",
      "price": 185.40,
      "quantity": 300
    }

buyer identity should really come from your own auth/session layer
(e.g. the logged-in user's name/id), not a free-text field a client can
fake - swap request.json["buyer"] for your session's real user identity
once you have real auth wired up.
"""

import re
import sys
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory

from bid_ingestion import RawBid, validate_bids, BidIngestionClient, OdooConfig
from finnhub_market_data import BrokerMarketData, BrokerConfig
from ask_book import AskBook

app = Flask(__name__)

FINNHUB_API_KEY = "d9biu5hr01qv2lms14bgd9biu5hr01qv2lms14c0"
STOCK_SYMBOL_MAP = {"AAPL_STOCK": "AAPL", "TSLA_STOCK": "TSLA", "MSFT_STOCK": "MSFT"}
bmd = BrokerMarketData(BrokerConfig(api_key=FINNHUB_API_KEY))
ask_book = AskBook()


@app.route("/")
def frontend():
    return send_from_directory(".", "trading_desk.html")


@app.route("/bids", methods=["POST"])
def submit_bid():
    data = request.get_json(force=True)
    try:
        bid = RawBid(
            buyer=str(data["buyer"]),
            stock_id=str(data["stock_id"]),
            price=float(data["price"]),
            quantity=float(data["quantity"]),
            timestamp=datetime.now(),
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
    # result - that only resolves when run_pipeline.py next runs)
    current_high = _highest_registered_bid(bid.stock_id)
    outbid = current_high is not None and current_high > bid.price
    result["outbid"] = outbid
    result["current_highest_bid"] = current_high
    return jsonify(result), (201 if result["status"] == "created" else 200)


@app.route("/asks", methods=["POST"])
def submit_ask():
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


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("Usage: py Test_bid_generation.py <odoo_url> <odoo_db> <odoo_username> <odoo_api_key>")
        sys.exit(1)

    url, db, username, api_key = sys.argv[1:5]
    odoo_config = OdooConfig(url=url, db=db, username=username, api_key=api_key)
    ingestion_client = BidIngestionClient(odoo_config, dry_run=False)

    import xmlrpc.client
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, username, api_key, {})
    models_client = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    app.run(host="0.0.0.0", port=5001)