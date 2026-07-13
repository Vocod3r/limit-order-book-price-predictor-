"""
Stage 7: Accounting & Settlement.

Takes a confirmed sale.order (from promote_winners.py) and:
  1. Generates a draft customer invoice (account.move, move_type=out_invoice)
  2. Posts it (draft -> posted, the actual "invoice generated" state)
  3. Registers a payment against it via Odoo's account.payment.register wizard
  4. (Journal entries are created automatically by Odoo when the payment
     posts - there is no separate manual step for that part; Odoo's
     accounting engine handles double-entry bookkeeping internally)

IMPORTANT CAVEAT: invoicing/payment flows are the part of Odoo most
sensitive to configuration - required taxes, fiscal positions, chart of
accounts, default payment journals all vary by install. This code makes
minimal assumptions (no tax, default journal) and is more likely than the
earlier CRM/Sales code to need adjustment for your specific Odoo setup.
Test with dry_run=True first, and expect to iterate on error messages
against your real instance rather than assuming this works unmodified.
"""

import xmlrpc.client
from dataclasses import dataclass
from typing import Optional

from LOB_ssm import OdooConfig


class AccountingClient:
    def __init__(self, config: OdooConfig, dry_run: bool = True):
        self.config = config
        self.dry_run = dry_run
        self._uid = None
        self._models = None

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

    def _execute(self, model: str, method: str, *args, **kwargs):
        return self._models.execute_kw(
            self.config.db, self._uid, self.config.api_key, model, method,
            list(args), kwargs
        )

    def create_and_post_invoice(self, sale_order_id: int) -> dict:
        """
        Generates a customer invoice from a confirmed sale order and posts
        it. Returns {"status": ..., "invoice_id": ...} or an error dict.
        """
        try:
            if self.dry_run:
                return {"status": "dry_run", "sale_order_id": sale_order_id,
                         "would_do": "create + post invoice for this sale order"}

            # _create_invoices() is the method Odoo's own "Create Invoice"
            # button calls internally. Despite the leading underscore
            # (Odoo's convention for "not meant to be a UI button target
            # directly"), it's a normal model method and callable via
            # execute_kw - this is how it's driven programmatically.
            invoice_ids = self._execute("sale.order", "_create_invoices", [sale_order_id])
            if not invoice_ids:
                return {"status": "error", "sale_order_id": sale_order_id,
                         "error": "_create_invoices returned no invoice - order may "
                                   "already be fully invoiced, or have no invoiceable lines"}

            invoice_id = invoice_ids[0] if isinstance(invoice_ids, list) else invoice_ids

            # Post the invoice (draft -> posted). This is what actually
            # generates the journal entries in Odoo's accounting.
            self._execute("account.move", "action_post", [invoice_id])

            return {"status": "posted", "sale_order_id": sale_order_id, "invoice_id": invoice_id}

        except Exception as e:
            return {"status": "error", "sale_order_id": sale_order_id, "error": str(e)}

    def register_payment(self, invoice_id: int, amount: Optional[float] = None) -> dict:
        """
        Registers a payment against a posted invoice using Odoo's
        account.payment.register wizard - the same mechanism the "Register
        Payment" button in the UI uses. Assumes full payment (invoice's
        amount_total) if `amount` isn't given, and Odoo's default payment
        journal/method for the company - override via wizard vals below
        if your setup needs a specific journal.
        """
        try:
            if self.dry_run:
                return {"status": "dry_run", "invoice_id": invoice_id,
                         "would_do": f"register payment (amount={amount or 'full'})"}

            wizard_vals = {
                "active_model": "account.move",
                "active_ids": [invoice_id],
            }
            if amount is not None:
                wizard_vals["amount"] = amount

            wizard_id = self._execute(
                "account.payment.register", "create", wizard_vals,
                context={"active_model": "account.move", "active_ids": [invoice_id]},
            )
            result = self._execute("account.payment.register", "action_create_payments", [wizard_id])

            return {"status": "paid", "invoice_id": invoice_id, "wizard_result": result}

        except Exception as e:
            return {"status": "error", "invoice_id": invoice_id, "error": str(e)}

    def settle_sale_order(self, sale_order_id: int) -> dict:
        """Convenience: invoice + post + pay, in one call."""
        invoice_result = self.create_and_post_invoice(sale_order_id)
        if invoice_result["status"] not in ("posted", "dry_run"):
            return {"invoice": invoice_result, "payment": None}

        if self.dry_run:
            payment_result = self.register_payment(None)  # dry_run short-circuits before using it
        else:
            payment_result = self.register_payment(invoice_result["invoice_id"])

        return {"invoice": invoice_result, "payment": payment_result}