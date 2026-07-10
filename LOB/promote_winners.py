"""
Stage 6: promote auction winners from crm.lead (type="lead") into real
Opportunities, then create and confirm a Sales Order for each - the actual
"Odoo needs to realize these are the bids we move forward" step.

This is a WRITE path, separate from lob_ssm.py's read-only functions.
It takes the AuctionWinner list that lob_ssm.determine_auction_winner()
already produced (each one carries the exact lead_id it came from - no
re-searching or guessing which record it was) and:

  1. Updates that crm.lead's type from "lead" to "opportunity"
  2. Ensures a product exists for the stock (creates one if needed)
  3. Creates a sale.order linked to that opportunity
  4. Confirms it - Odoo's action_confirm turns the quotation into the
     official Sales Order, matching Stage 6 -> Stage 7 in the pipeline doc

Losing bids are left untouched - they remain as unconverted leads, a
permanent record that they existed and didn't clear this round.
"""

import xmlrpc.client
from dataclasses import dataclass
from typing import List

from LOB_ssm import AuctionWinner, OdooConfig


class WinnerPromotionClient:
    def __init__(self, config: OdooConfig, dry_run: bool = True):
        self.config = config
        self.dry_run = dry_run
        self._uid = None
        self._models = None
        self._product_cache = {}

        if not dry_run:
            self._connect()

    def _connect(self):
        common = xmlrpc.client.ServerProxy(f"{self.config.url}/xmlrpc/2/common")
        self._uid = common.authenticate(
            self.config.db, self.config.username, self.config.api_key, {}
        )
        if not self._uid:
            raise RuntimeError("Odoo authentication failed - check credentials.")
        self._models = xmlrpc.client.ServerProxy(f"{self.config.url}/xmlrpc/2/object")

    def _execute(self, model: str, method: str, *args):
        return self._models.execute_kw(
            self.config.db, self._uid, self.config.api_key, model, method, list(args)
        )

    def ensure_product(self, stock_id: str) -> int:
        name = f"Auction stock - {stock_id}"
        if name in self._product_cache:
            return self._product_cache[name]
        existing = self._execute("product.product", "search", [("name", "=", name)])
        pid = existing[0] if existing else self._execute("product.product", "create", {
            "name": name, "type": "service", "sale_ok": True,
        })
        self._product_cache[name] = pid
        return pid

    def promote_winner(self, winner: AuctionWinner, stock_id: str) -> dict:
        """
        Promotes exactly one winner's lead by its known lead_id - no
        re-searching. Returns a result dict; never raises, so a batch
        loop can continue past one failure.
        """
        try:
            if self.dry_run:
                return {
                    "lead_id": winner.lead_id, "status": "dry_run",
                    "would_do": f"promote lead {winner.lead_id} -> opportunity, "
                                f"create+confirm sale.order for {winner.buyer} "
                                f"({winner.quantity:.0f} @ {winner.price:.2f})",
                }

            # 1. Fetch the lead's partner_id (need it for the sale order)
            lead = self._execute("crm.lead", "read", [winner.lead_id], ["partner_id", "type"])
            if not lead:
                return {"lead_id": winner.lead_id, "status": "error",
                         "error": "lead not found - was it deleted?"}
            lead = lead[0]

            if lead["type"] == "opportunity":
                return {"lead_id": winner.lead_id, "status": "already_promoted"}

            partner_id = lead["partner_id"][0] if lead.get("partner_id") else False
            if not partner_id:
                return {"lead_id": winner.lead_id, "status": "error",
                         "error": "lead has no partner_id - cannot create a sale order without one"}

            # 2. Promote lead -> opportunity
            self._execute("crm.lead", "write", [winner.lead_id], {"type": "opportunity"})

            # 3. Ensure a product exists, create the sale order
            product_id = self.ensure_product(stock_id)
            order_id = self._execute("sale.order", "create", {
                "partner_id": partner_id,
                "opportunity_id": winner.lead_id,
                "order_line": [(0, 0, {
                    "product_id": product_id,
                    "product_uom_qty": winner.quantity,
                    "price_unit": winner.price,
                })],
            })

            # 4. Confirm - this is what turns it into the official Sales Order
            self._execute("sale.order", "action_confirm", [order_id])

            return {
                "lead_id": winner.lead_id, "status": "promoted",
                "sale_order_id": order_id,
            }

        except Exception as e:
            return {"lead_id": winner.lead_id, "status": "error", "error": str(e)}

    def promote_all(self, winners: List[AuctionWinner], stock_id: str) -> dict:
        results = [self.promote_winner(w, stock_id) for w in winners]
        summary = {
            "total": len(results),
            "promoted": sum(1 for r in results if r["status"] == "promoted"),
            "already_promoted": sum(1 for r in results if r["status"] == "already_promoted"),
            "dry_run": sum(1 for r in results if r["status"] == "dry_run"),
            "errors": [r for r in results if r["status"] == "error"],
        }
        return {"results": results, "summary": summary}