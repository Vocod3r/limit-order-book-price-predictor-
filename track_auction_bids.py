"""
track_auction_bids.py
─────────────────────
Reads a LOBSTER z-scored LOB file and maps the DOUBLE AUCTION
bid series structure onto Odoo CRM opportunities.

File structure (confirmed):
  Rows  = 149 feature channels
  Cols  = 47,342 LOB event timesteps

  Row layout (alternating smooth/noisy = price/volume):
    Even rows (0, 2, 4, ...): BID PRICE levels  (very smooth — step functions)
    Odd rows  (1, 3, 5, ...): BID VOLUME levels (noisy)

  Double auction mapping:
    Rows 0, 2, 4, 6, 8  → Bid prices  L1–L5  (best → deep)
    Rows 1, 3, 5, 7, 9  → Bid volumes L1–L5
    Rows 10,12,14,16,18 → Ask prices  L1–L5
    Rows 11,13,15,17,19 → Ask volumes L1–L5
    Rows 20+            → Additional feature channels

CRM mapping:
  Each BID SERIES (one price-level channel) → one CRM Opportunity
  Pipeline stages track the auction progression state.
"""

import numpy as np
import xmlrpc.client
import argparse
import datetime
import os

# ── Config ────────────────────────────────────────────────
ODOO_URL      = "http://localhost:8069"
ODOO_DB       = "practice1"
ODOO_USER     = "vipindnerd07@gmail.com"
ODOO_PASSWORD = "#Teehee123"

# ── Double auction pipeline stages ────────────────────────
# These stages track WHERE in the auction process a bid series is
AUCTION_STAGES = {
    "opening":          "New",           # Auction just opened
    "active_bidding":   "Qualified",     # Active price discovery
    "near_match":       "Proposition",   # Bid close to ask (tight spread)
    "matched":          "Won",           # Bid crossed ask → trade
    "withdrawn":        "Lost",          # Bid pulled / price reverted
}

# ─────────────────────────────────────────────────────────
# 1. Parse file
# ─────────────────────────────────────────────────────────

def load_file(filepath: str) -> np.ndarray:
    rows = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append([float(x) for x in line.split()])
    return np.array(rows, dtype=np.float32)


# ─────────────────────────────────────────────────────────
# 2. Analyse each bid series
# ─────────────────────────────────────────────────────────

def analyse_bid_series(series: np.ndarray, level: int,
                       matching_ask: np.ndarray) -> dict:
    """
    Compute double-auction metrics for one bid price series.

    Args:
        series:       (T,) z-scored bid prices over time
        level:        LOB depth level (1=best bid)
        matching_ask: (T,) z-scored ask prices at same level
    """
    T = len(series)
    changes = np.diff(series)

    up_ticks   = int((changes > 0).sum())
    down_ticks = int((changes < 0).sum())
    flat_ticks = int((changes == 0).sum())

    # Spread between bid and ask at this level
    spread = matching_ask - series           # positive = no match yet
    min_spread = float(spread.min())
    mean_spread = float(spread.mean())

    # "Near match" = spread < 10th percentile
    tight_threshold = float(np.percentile(spread, 10))
    n_near_match = int((spread < tight_threshold).sum())

    # "Crossed" = bid >= ask (trade would occur)
    n_crossed = int((series >= matching_ask).sum())

    # Price trend via linear regression slope
    t = np.arange(T)
    slope = float(np.polyfit(t, series, 1)[0])

    # Max drawdown (peak → trough)
    running_max = np.maximum.accumulate(series)
    drawdowns   = series - running_max
    max_drawdown = float(drawdowns.min())

    # Bid aggressiveness: how often bid price was revised upward
    bid_revisions_up   = up_ticks
    bid_revisions_down = down_ticks

    # Classify auction state
    if n_crossed > T * 0.05:
        stage = "matched"
    elif min_spread < 0.05:
        stage = "near_match"
    elif slope > 0 and up_ticks > down_ticks:
        stage = "active_bidding"
    elif slope < -0.0001:
        stage = "withdrawn"
    else:
        stage = "opening"

    # Revenue proxy: higher = more interesting bid series
    # Use (aggressiveness × tightness) as a score
    score = round((bid_revisions_up / T) * 100 * max(0.01, 1 - mean_spread), 2)

    return {
        "level":               level,
        "n_ticks":             T,
        "start_price":         round(float(series[0]),  4),
        "end_price":           round(float(series[-1]), 4),
        "net_change":          round(float(series[-1] - series[0]), 4),
        "slope":               round(slope * 1000, 6),   # per 1000 ticks
        "volatility":          round(float(series.std()), 4),
        "max_drawdown":        round(max_drawdown, 4),
        "up_ticks":            up_ticks,
        "flat_ticks":          flat_ticks,
        "down_ticks":          down_ticks,
        "mean_spread":         round(mean_spread, 4),
        "min_spread":          round(min_spread, 4),
        "n_near_match_ticks":  n_near_match,
        "n_crossed_ticks":     n_crossed,
        "bid_aggressiveness":  round(up_ticks / max(1, up_ticks + down_ticks), 4),
        "auction_stage":       stage,
        "score":               score,
    }


# ─────────────────────────────────────────────────────────
# 3. Build CRM description
# ─────────────────────────────────────────────────────────

def build_description(m: dict, filename: str) -> str:
    direction = "↑ upward" if m["net_change"] > 0 else "↓ downward"
    aggr_pct = round(m["bid_aggressiveness"] * 100, 1)
    return f"""═══ Double Auction Bid Series: Level {m['level']} ═══
Source   : {filename}
Logged   : {datetime.date.today()}

── Auction Structure ──
  LOB depth level  : L{m['level']} ({'best bid' if m['level'] == 1 else f'depth {m["level"]}'})
  Total ticks      : {m['n_ticks']:,}
  Auction stage    : {m['auction_stage'].upper()}

── Bid Price Series ──
  Entry / Exit     : {m['start_price']} → {m['end_price']}
  Net change       : {m['net_change']} z-score ({direction})
  Trend (slope)    : {m['slope']} per 1K ticks
  Volatility       : {m['volatility']}
  Max drawdown     : {m['max_drawdown']}

── Tick Activity ──
  Up ticks         : {m['up_ticks']:,}
  Flat ticks       : {m['flat_ticks']:,}
  Down ticks       : {m['down_ticks']:,}
  Aggressiveness   : {aggr_pct}% upward revisions

── Spread & Match ──
  Mean spread (bid-ask): {m['mean_spread']}
  Min spread           : {m['min_spread']}
  Near-match ticks     : {m['n_near_match_ticks']:,}
  Crossed (trade) ticks: {m['n_crossed_ticks']:,}"""


# ─────────────────────────────────────────────────────────
# 4. Odoo client
# ─────────────────────────────────────────────────────────

class OdooClient:
    def __init__(self):
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        self.uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not self.uid:
            raise RuntimeError("Odoo auth failed")
        self.models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    def call(self, model, method, args, kw=None):
        return self.models.execute_kw(
            ODOO_DB, self.uid, ODOO_PASSWORD,
            model, method, args, kw or {})

    def stage_id(self, name):
        r = self.call("crm.stage", "search_read",
                      [[["name", "=", name]]], {"fields": ["id"], "limit": 1})
        return r[0]["id"] if r else None

    def tag_id(self, name):
        r = self.call("crm.tag", "search_read",
                      [[["name", "=", name]]], {"fields": ["id"], "limit": 1})
        return r[0]["id"] if r else self.call("crm.tag", "create", [{"name": name}])


def log_to_odoo(odoo: OdooClient, m: dict,
                filename: str, dataset_tag: str) -> int:
    stage_name = AUCTION_STAGES.get(m["auction_stage"], "New")
    stage      = odoo.stage_id(stage_name)

    tags = [odoo.tag_id(t) for t in [
        "LOBSTER", dataset_tag,
        f"L{m['level']}",
        m["auction_stage"].replace("_", "-"),
    ]]

    vals = {
        "name":             f"[Bid L{m['level']}] {os.path.basename(filename)}",
        "description":      build_description(m, filename),
        "expected_revenue": m["score"],   # aggressiveness × tightness score
        "probability":      min(99, int(m["bid_aggressiveness"] * 100)),
    }
    if stage:
        vals["stage_id"] = stage
    vals["tag_ids"] = [(6, 0, tags)]

    lead_id = odoo.call("crm.lead", "create", [vals])
    return lead_id


# ─────────────────────────────────────────────────────────
# 5. Main
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",   required=True)
    parser.add_argument("--levels", type=int, default=5,
                        help="How many LOB depth levels to track (default 5)")
    parser.add_argument("--tag",    default="Auction",
                        help="Dataset tag to apply in CRM")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print analysis without posting to Odoo")
    args = parser.parse_args()

    print(f"[1/3] Loading {args.file} …")
    X = load_file(args.file)
    n_rows, n_ticks = X.shape
    print(f"      {n_rows} channels × {n_ticks:,} ticks")

    # Row layout: 0,2,4,6,8=bid prices; 10,12,14,16,18=ask prices
    # (confirmed via smoothness analysis)
    bid_price_rows = [2 * i       for i in range(args.levels)]   # 0,2,4,6,8
    ask_price_rows = [10 + 2 * i  for i in range(args.levels)]   # 10,12,14,16,18

    print(f"[2/3] Analysing {args.levels} bid levels …")
    results = []
    for i in range(args.levels):
        br = bid_price_rows[i]
        ar = ask_price_rows[i]
        bid_series = X[br]
        ask_series = X[ar] if ar < n_rows else np.zeros_like(bid_series)

        m = analyse_bid_series(bid_series, level=i + 1, matching_ask=ask_series)
        results.append(m)

        print(f"  L{i+1}: stage={m['auction_stage']:15s}  "
              f"spread={m['mean_spread']:.4f}  "
              f"aggr={m['bid_aggressiveness']:.3f}  "
              f"score={m['score']:.2f}")

    if args.dry_run:
        print("\n[Dry run] Sample description for L1:")
        print(build_description(results[0], args.file))
        return

    print(f"[3/3] Posting to Odoo …")
    try:
        odoo = OdooClient()
    except Exception as e:
        print(f"  ✗ Odoo unreachable: {e}")
        return

    for m in results:
        lead_id = log_to_odoo(odoo, m, args.file, args.tag)
        print(f"  ✓ L{m['level']} → Opportunity id={lead_id}  "
              f"stage={AUCTION_STAGES[m['auction_stage']]}")

    print(f"\n[Done] {args.levels} bid series logged to CRM")


if __name__ == "__main__":
    main()