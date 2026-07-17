"""
Prints the real sale_order_id for every order still in "to invoice"
status - no clicking through the UI needed.

Usage:
    py list_orders_to_invoice.py <url> <db> <username> <api_key>
"""

import sys
import xmlrpc.client


def main():
    if len(sys.argv) != 5:
        print("Usage: py list_orders_to_invoice.py <url> <db> <username> <api_key>")
        sys.exit(1)

    url, db, username, api_key = sys.argv[1:5]
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, username, api_key, {})
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    orders = models.execute_kw(db, uid, api_key, "sale.order", "search_read",
        [[("invoice_status", "=", "to invoice")]],
        {"fields": ["id", "name", "amount_total", "order_line"]})

    if not orders:
        print("Nothing to invoice.")
        return

    for o in orders:
        lines = models.execute_kw(db, uid, api_key, "sale.order.line", "read",
            [o["order_line"]], {"fields": ["product_id"]})
        products = ", ".join(l["product_id"][1] for l in lines)
        print(f"id={o['id']}  name={o['name']}  total={o['amount_total']}  product(s)={products}")

    print("\nRun settle_existing.py with these ids: " + " ".join(str(o["id"]) for o in orders))


if __name__ == "__main__":
    main()