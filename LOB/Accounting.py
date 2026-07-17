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

        Uses the sale.advance.payment.inv wizard - the same public model
        the "Create Invoice" button in the UI drives - because Odoo 17+
        blocks calling private methods like sale.order._create_invoices
        directly over XML-RPC ("Private methods cannot be called
        remotely"). advance_payment_method="delivered" means "invoice the
        full order", matching the old _create_invoices behavior.
        """
        try:
            if self.dry_run:
                return {"status": "dry_run", "sale_order_id": sale_order_id,
                         "would_do": "create + post invoice for this sale order"}

            order = self._execute("sale.order", "read", [sale_order_id], ["invoice_ids", "invoice_status"])
            existing_invoice_ids = order[0]["invoice_ids"] if order else []
            if existing_invoice_ids:
                invoice_id = existing_invoice_ids[0]
                state = self._execute("account.move", "read", [invoice_id], ["state"])[0]["state"]
                if state != "posted":
                    self._execute("account.move", "action_post", [invoice_id])
                return {"status": "posted", "sale_order_id": sale_order_id, "invoice_id": invoice_id}

            context = {"active_model": "sale.order", "active_ids": [sale_order_id],
                       "active_id": sale_order_id}
            wizard_id = self._execute(
                "sale.advance.payment.inv", "create",
                {"advance_payment_method": "delivered"}, context=context,
            )
            try:
                self._execute("sale.advance.payment.inv", "create_invoices", [wizard_id], context=context)
            except xmlrpc.client.Fault as e:
                if "cannot marshal None" not in str(e):
                    raise
                # create_invoices() succeeded server-side; only its action
                # dict (used to redirect the UI, not needed here) failed
                # to serialize back - safe to ignore.

            order = self._execute("sale.order", "read", [sale_order_id], ["invoice_ids"])
            invoice_ids = order[0]["invoice_ids"] if order else []
            if not invoice_ids:
                return {"status": "error", "sale_order_id": sale_order_id,
                         "error": "invoice wizard ran but no invoice_ids found on the order - "
                                   "order may already be fully invoiced, or have no invoiceable lines"}

            invoice_id = invoice_ids[0]

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

            wizard_vals = {}
            if amount is not None:
                wizard_vals["amount"] = amount

            context = {"active_model": "account.move", "active_ids": [invoice_id]}
            wizard_id = self._execute(
                "account.payment.register", "create", wizard_vals, context=context,
            )
            try:
                result = self._execute("account.payment.register", "action_create_payments",
                                        [wizard_id], context=context)
            except xmlrpc.client.Fault as e:
                if "cannot marshal None" not in str(e):
                    raise
                result = None  # succeeded server-side; only the action-dict response failed to serialize

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