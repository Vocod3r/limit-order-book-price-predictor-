"""
Standalone: settle specific sale_order_ids (invoice + payment) and print
the FULL error text, not just the status. Use this to diagnose Phase 5
failures without re-running the whole pipeline (which would generate 60
new duplicate bids just to get back to this point).

Usage:
    py settle_existing.py <url> <db> <username> <api_key> <sale_order_id> [<sale_order_id> ...]
"""

import sys
from LOB_ssm import OdooConfig
from Accounting import AccountingClient


def main():
    if len(sys.argv) < 6:
        print("Usage: py settle_existing.py <url> <db> <username> <api_key> <sale_order_id> [more ids...]")
        sys.exit(1)

    url, db, username, api_key = sys.argv[1:5]
    sale_order_ids = [int(x) for x in sys.argv[5:]]

    config = OdooConfig(url=url, db=db, username=username, api_key=api_key)
    client = AccountingClient(config, dry_run=False)

    for sale_order_id in sale_order_ids:
        print(f"\n=== Settling sale_order_id={sale_order_id} ===")
        result = client.settle_sale_order(sale_order_id)

        invoice = result["invoice"]
        print(f"Invoice status: {invoice['status']}")
        if invoice["status"] == "error":
            print(f"Invoice error: {invoice['error']}")
            continue  # payment was skipped since invoice failed

        payment = result["payment"]
        print(f"Payment status: {payment['status']}")
        if payment["status"] == "error":
            print(f"Payment error: {payment['error']}")


if __name__ == "__main__":
    main()