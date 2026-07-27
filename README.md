# LOB Auction Pipeline

A limit-order-book auction system with two distinct layers that share the
same underlying state-space model:

1. **Academic / diagnostic layer** — validates the IMM regime-switching
   Kalman filter (`LOB_ssm.py`) against the FI2010 benchmark dataset, so
   there's a known-good reference for how queue imbalance, regime
   probabilities, and event detection *should* behave on real exchange
   data.
2. **Live pipeline** — the same filter running against real, ongoing bids
   and asks from a working web frontend, feeding into Odoo CRM/Sales for
   actual auction execution and order promotion.

Read this before assuming a number "should" match the other layer —
they're built from structurally different data, on purpose (see
[Known limitations](#known-limitations-by-design) below).

---

## 1. Academic diagnostic layer (FI2010)

Validates `LOB_ssm.py`'s IMM filter against the public FI2010 limit order
book benchmark dataset — real, continuously-refreshed exchange order flow,
used as ground truth for whether the filter's regime detection and
queue-imbalance calculation behave sensibly before trusting them on live
data.

| File | Purpose |
|---|---|
| `Fi2010_batch.py` | Runs the filter across FI2010 batches |
| `Fi2010_compare.py` | Compares filter output against FI2010 ground truth |
| `Fi2010_diagnostics.py` | Regime/event diagnostics on FI2010 data |
| `Fi2010_evaluate.py` | Scoring/evaluation metrics |
| `fi2010_visualize.ipynb` | Notebook: plots, boxplots, consistency checks |
| `NoAuction/` | FI2010 dataset splits (Zscore / MinMax / DecPre normalizations, train/test) |
| `boxplot_*.png`, `consistency_*.png`, `auc_comparison_*.png` | Generated diagnostic plots per normalization method |

**Reference range**: on FI2010 data, queue imbalance settles around
**0.53–0.58** — real order flow has continuous replenishment/cancellation
on both sides of the book, keeping bid and ask depth roughly balanced in
magnitude. This is the number to sanity-check the filter's math against,
*not* something the live pipeline (§2) should be expected to match, since
live test data doesn't have equivalent two-sided replenishment yet.

## 2. Live pipeline (frontend → Odoo)

```
 Buyer/Seller (React frontend, role-gated)
        |
        v
 Flask backend (Test_bid_generation.py)
        |
        +- POST /bids  --------> bid_ingestion.py --> Odoo CRM (crm.lead)
        +- POST /asks  --------> ask_book.py (local JSON, single best level)
        +- GET  /diagnostics/:id --> LOB_ssm.py (IMM filter, live ticks)
        +- GET  /auction/:id/winner --> LOB_ssm.py (read-only preview)
        +- POST /auction/:id/end
                |
                +- LOB_ssm.find_highest_bid_from_odoo (determine winner(s))
                +- promote_winners.py --> Odoo (lead -> opportunity -> confirmed sale.order)
                +- ask_book.deplete() (reflect the fill)
```

Invoicing (`Accounting.py`, `Settle_existing.py`) is a **deliberately
separate, manual step** — not auto-triggered when an auction ends. Odoo's
tax/journal/fiscal-position config varies too much per install to safely
automate blind (see `Accounting.py`'s own docstring).

### Frontend

React + Vite, in `src/`. Key pieces:

- `RoleGate.jsx` — resolves buyer/seller/both from a `?role=` URL param or
  session storage; shows a picker if neither is set. Per-tab, not shared —
  two people (or two tabs) can hold different roles at once.
- `OrderForm.jsx` — locks to bid-only or ask-only based on role.
- `AuctionPanel.jsx` — live auction preview (polled) + "End auction"
  action, visible to seller/both roles only.
- `App.jsx` — polls `/bids`, `/asks`, `/diagnostics`, `/auction/:id/winner`
  every 4s (`POLL_INTERVAL_MS`) once a role is chosen; also handles
  dynamic instrument allocation (`?stock=` param + sidebar "+" control).
- `api/client.js` — `MOCK_MODE` env flag switches between canned mock
  data and the real Flask endpoints above.

### Backend routes (`Test_bid_generation.py`)

| Route | Method | Does |
|---|---|---|
| `/bids` | GET / POST | list resting (unpromoted) bids / submit a bid |
| `/asks` | GET / POST | current best ask (single level) / post or lower an ask |
| `/diagnostics/:stock_id` | GET | latest IMM regime + queue imbalance + event flags |
| `/auction/:stock_id/winner` | GET | **read-only preview** — safe to poll continuously |
| `/auction/:stock_id/end` | POST | **executes**: re-checks the live ask, optionally verifies it against `expected_ask_price` (409 if it moved), determines winner(s), promotes into Odoo, depletes the ask book |

### Odoo connection

Credentials come from environment variables (`ODOO_URL`, `ODOO_DB`,
`ODOO_USERNAME`, `ODOO_API_KEY`), read at **import time** so the app works
under both local dev (`py Test_bid_generation.py <url> <db> <user> <key>`
still works as a fallback) and a production WSGI server like gunicorn.

---

## Running locally

**Backend:**
```powershell
cd LOB
py Test_bid_generation.py <odoo_url> <odoo_db> <odoo_username> <odoo_api_key>
# or, with env vars set instead:
py Test_bid_generation.py
```

**Frontend:**
```powershell
cd LOB
npm run dev
```
(must be run from `LOB`, not `LOB/src` — Vite needs `index.html` and
`vite.config.js` at that level)

Set `VITE_MOCK_MODE=false` and `VITE_API_BASE_URL=http://localhost:5051`
in `.env.local` to talk to the real local backend instead of mock data.

## Deploying

See `DEPLOY.md` for the full walkthrough. Short version:
- **Frontend** -> Vercel/Netlify (static, free)
- **Backend** -> Render (needs a real persistent process — not serverless).
  Uses `Procfile` (`gunicorn --workers 1 --threads 8 --timeout 120
  Test_bid_generation:app`) and `requirements.txt`.
- **Odoo**, if running locally rather than hosted, needs a tunnel (e.g.
  ngrok) so Render can reach it — this is a fragile link for anything
  beyond testing/demos; a hosted Odoo instance (Odoo.sh or a VPS) removes
  the dependency on your laptop staying on.
- CORS must be configured in `Test_bid_generation.py`
  (`CORS(app, origins=[...])`) with your real deployed frontend URL.

## Known limitations (by design, not oversights)

- **Ask book is single-level.** `AskBook` tracks one best price/quantity
  per stock, no seller identity, no depth beyond the top level. `GET
  /asks` always returns at most one row.
- **Bid queue depth is a rolling snapshot, not lifetime.** `LOB_ssm.py`'s
  `best_bid_qty` only counts bids within a trailing
  `BID_RESTING_WINDOW_SECONDS` (default 10 min) — without this, depth
  only ever accumulates (nothing removes a bid from the sum except it
  winning an auction), which drags queue imbalance toward an extreme
  instead of the balanced range FI2010 shows. Tune the window if your
  actual bidding cadence differs.
- **Invoicing is manual.** Ending an auction promotes winners to a
  confirmed Odoo sale order; it does not invoice or collect payment.
  Run `Settle_existing.py` (or `Accounting.py` directly) separately.
- **No seller ownership of a listing.** Anyone can post a better ask and
  effectively take over as "the" seller for an instrument — there's no
  listing-ownership concept, unlike e.g. eBay.
- **Tick size / market hours are placeholder-real.** `finnhub_market_data.py`
  uses Finnhub's free tier: tick size is hardcoded (`0.01`), and while
  market-open status is a real API call, quote depth (`best_bid_qty`/
  `best_ask_qty` from `get_reference_quote`) is synthetic, not real order
  book depth.
