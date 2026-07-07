"""
import_lobster_to_crm.py
────────────────────────
Reads a LOBSTER-format .txt file (z-score normalised features),
computes summary statistics, and logs them as a CRM Opportunity
in Odoo — optionally attaching the raw file.

File format detected:
  149 rows × 47,342 values per row
  Likely: (n_samples, n_features) where features = LOB levels × OHLCV-type

Usage:
    python import_lobster_to_crm.py --file Train_Dst_Auction_ZScore_CF_1.txt
"""

import numpy as np
import base64
import os
import argparse
import xmlrpc.client
import datetime
import traceback


# ── Odoo connection ──────────────────────────────────────
ODOO_URL      = "http://localhost:8069"
ODOO_DB       = "practice1"
ODOO_USER     = "vipindnerd07@gmail.com"
ODOO_PASSWORD = "#Teehee123"


# ─────────────────────────────────────────────────────────
# 1.  Parse the LOBSTER file
# ─────────────────────────────────────────────────────────

def load_lobster(filepath: str) -> np.ndarray:
    """
    Load a space-separated z-score LOB feature file.
    Returns shape (n_rows, n_cols) as float32.
    """
    rows = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append([float(x) for x in line.split()])
    return np.array(rows, dtype=np.float32)


def compute_stats(X: np.ndarray, filepath: str) -> dict:
    """Compute summary stats suitable for CRM description."""
    n_rows, n_cols = X.shape
    filename = os.path.basename(filepath)
    file_size_mb = os.path.getsize(filepath) / 1024 / 1024

    # Per-feature stats
    col_means  = X.mean(axis=0)
    col_stds   = X.std(axis=0)
    col_mins   = X.min(axis=0)
    col_maxs   = X.max(axis=0)

    # Global stats
    global_mean   = float(X.mean())
    global_std    = float(X.std())
    global_min    = float(X.min())
    global_max    = float(X.max())
    sparsity      = float((X == 0).mean())
    n_outliers    = int(((X - global_mean).abs() > 3 * global_std).sum()
                        if hasattr(X, 'abs')
                        else (np.abs(X - global_mean) > 3 * global_std).sum())

    # Detect likely feature structure
    # LOBSTER 10-level: 40 features per message type
    # FI-2010 format: 144 features
    feature_hint = "unknown"
    if n_cols % 40 == 0:
        feature_hint = f"{n_cols // 40} × 40 (10-level LOB × 4 fields)"
    elif n_cols % 144 == 0:
        feature_hint = f"{n_cols // 144} × 144 (FI-2010 style)"

    return {
        "filename":      filename,
        "file_size_mb":  round(file_size_mb, 2),
        "n_samples":     n_rows,
        "n_features":    n_cols,
        "feature_hint":  feature_hint,
        "global_mean":   round(global_mean, 4),
        "global_std":    round(global_std, 4),
        "global_min":    round(global_min, 4),
        "global_max":    round(global_max, 4),
        "sparsity_pct":  round(sparsity * 100, 2),
        "n_outliers_3s": n_outliers,
        "z_normalized":  True,  # filename contains 'ZScore'
    }


# ─────────────────────────────────────────────────────────
# 2.  Odoo helpers
# ─────────────────────────────────────────────────────────

class OdooClient:
    def __init__(self):
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        self.uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not self.uid:
            raise RuntimeError("Odoo authentication failed")
        self.models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        print(f"[Odoo] Connected uid={self.uid}")

    def call(self, model, method, args, kwargs=None):
        return self.models.execute_kw(
            ODOO_DB, self.uid, ODOO_PASSWORD,
            model, method, args, kwargs or {})

    def get_stage_id(self, name):
        r = self.call("crm.stage", "search_read",
                      [[["name", "=", name]]], {"fields": ["id"], "limit": 1})
        return r[0]["id"] if r else None

    def get_tag_id(self, name):
        r = self.call("crm.tag", "search_read",
                      [[["name", "=", name]]], {"fields": ["id"], "limit": 1})
        if r:
            return r[0]["id"]
        return self.call("crm.tag", "create", [{"name": name}])


def build_description(stats: dict) -> str:
    return f"""═══ LOB Dataset: {stats['filename']} ═══

── File info ──
  File size     : {stats['file_size_mb']} MB
  Samples (rows): {stats['n_samples']:,}
  Features (cols): {stats['n_features']:,}
  Feature layout: {stats['feature_hint']}
  Z-normalised  : {stats['z_normalized']}

── Global statistics ──
  Mean          : {stats['global_mean']}
  Std dev       : {stats['global_std']}
  Min / Max     : {stats['global_min']} / {stats['global_max']}
  Sparsity      : {stats['sparsity_pct']}%
  Outliers (>3σ): {stats['n_outliers_3s']:,}

── Notes ──
Imported via import_lobster_to_crm.py on {datetime.date.today()}"""


def attach_file(odoo: OdooClient, lead_id: int, filepath: str):
    """Attach the raw data file to the CRM opportunity."""
    filename = os.path.basename(filepath)
    with open(filepath, "rb") as f:
        data_b64 = base64.b64encode(f.read()).decode()

    odoo.call("ir.attachment", "create", [{
        "name":      filename,
        "datas":     data_b64,
        "res_model": "crm.lead",
        "res_id":    lead_id,
        "mimetype":  "text/plain",
    }])
    print(f"[Odoo] Attached '{filename}' to opportunity id={lead_id}")


# ─────────────────────────────────────────────────────────
# 3.  Main
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",   required=True, help="Path to LOBSTER .txt file")
    parser.add_argument("--attach", action="store_true",
                        help="Attach the raw file to the Odoo opportunity")
    parser.add_argument("--tag",    default="LOBSTER",
                        help="CRM tag to apply (default: LOBSTER)")
    parser.add_argument("--stage",  default="New",
                        help="Pipeline stage name (default: New)")
    args = parser.parse_args()
    
    print("Current working directory:", os.getcwd())
    print("File argument:", repr(args.file))

    # ── Load & analyse ──────────────────────────────────
    print(f"[1/3] Loading {args.file} …")
    X     = load_lobster(args.file)
    stats = compute_stats(X, args.file)
    print(f"      Shape: {X.shape}  |  mean={stats['global_mean']}  "
          f"std={stats['global_std']}")

    # ── Connect to Odoo ─────────────────────────────────
    print("[2/3] Connecting to Odoo …")
    try:
        odoo = OdooClient()
    except Exception as e:
        print(f"      ✗ Cannot reach Odoo: {e}")
        print("      Printing stats to console instead:\n")
        print(build_description(stats))
        return

    # ── Create opportunity ──────────────────────────────
    print("[3/3] Creating CRM opportunity …")
    stage_id = odoo.get_stage_id(args.stage)
    tag_ids  = [odoo.get_tag_id(t) for t in [args.tag, "Synthetic"]]

    vals = {
        "name":        f"[Dataset] {stats['filename']}",
        "description": build_description(stats),
        # Use n_samples/1000 as a proxy revenue metric (sortable)
        "expected_revenue": round(stats["n_samples"] / 1000, 1),
    }
    if stage_id:
        vals["stage_id"] = stage_id
    if tag_ids:
        vals["tag_ids"] = [(6, 0, tag_ids)]

    lead_id = odoo.call("crm.lead", "create", [vals])
    print(f"      ✓ Created Opportunity id={lead_id}")

    # ── Optionally attach file ───────────────────────────
    if args.attach:
        attach_file(odoo, lead_id, args.file)

    print(f"\n[Done] Dataset logged to CRM opportunity id={lead_id}")
    print(f"       View at: {ODOO_URL}/web#action=crm.crm_lead_all_leads&id={lead_id}")


if __name__ == "__main__":
    main()