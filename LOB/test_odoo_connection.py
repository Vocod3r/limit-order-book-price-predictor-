"""
Standalone connectivity check for your real local Odoo instance.
Run this BEFORE run_pipeline.py, so any connection problem is isolated
here rather than buried inside the pipeline.

Usage:
    py test_odoo_connection.py http://localhost:8069 your_db your_username your_api_key
"""

import sys
import xmlrpc.client


def main():
    if len(sys.argv) != 5:
        print("Usage: py test_odoo_connection.py <url> <db> <username> <api_key>")
        sys.exit(1)

    url, db, username, api_key = sys.argv[1:5]

    print(f"1. Connecting to {url} ...")
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    try:
        version = common.version()
        print(f"   OK - Odoo server version: {version.get('server_version')}")
    except Exception as e:
        print(f"   FAILED to reach the server at all: {e}")
        print("   Check the URL and that Odoo is actually running.")
        sys.exit(1)

    print(f"2. Authenticating as {username} against db '{db}' ...")
    uid = common.authenticate(db, username, api_key, {})
    if not uid:
        print("   FAILED - authentication returned no uid.")
        print("   Check: db name is exact, username is correct, api_key is valid "
              "(not your login password), and the user isn't disabled.")
        sys.exit(1)
    print(f"   OK - authenticated as uid={uid}")

    print("3. Checking the CRM module (crm.lead) is installed and reachable ...")
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
    try:
        count = models.execute_kw(db, uid, api_key, "crm.lead", "search_count", [[]])
        print(f"   OK - crm.lead is reachable, {count} existing lead(s) in this database")
    except Exception as e:
        print(f"   FAILED to query crm.lead: {e}")
        print("   Check that the CRM app is installed (Apps > search 'CRM').")
        sys.exit(1)

    print("4. Checking write access (res.partner) ...")
    try:
        test_id = models.execute_kw(
            db, uid, api_key, "res.partner", "create",
            [{"name": "[CONNECTION TEST - safe to delete]"}]
        )
        print(f"   OK - created test partner id={test_id}")
        models.execute_kw(db, uid, api_key, "res.partner", "unlink", [[test_id]])
        print(f"   OK - cleaned up test partner")
    except Exception as e:
        print(f"   FAILED to write: {e}")
        print("   Check the user's access rights for res.partner (Settings > Users).")
        sys.exit(1)

    print("\nAll checks passed. This user/db/api_key combination is ready to use "
          "in bid_ingestion.py and lob_ssm.py.")


if __name__ == "__main__":
    main()