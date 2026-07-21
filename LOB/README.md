# Auction Desk

A React dashboard for the LOB auction pipeline: submit bids/asks, watch the
Kalman/IMM filter's regime diagnostics update, and see auction results — all
in one place instead of juggling curl commands and terminal output.

## Setup

```bash
npm install
npm run dev
```

Opens at `http://localhost:5173`.

## IMPORTANT: this ships in mock mode by default

The app currently runs against **fake, in-memory data** (`MOCK_MODE = true`
in `src/api/client.js`), not your real Flask server. This was deliberate —
only one backend route was ever confirmed in our conversation
(`POST /bids`), so the rest of the endpoints below are **reasonable guesses
at REST conventions, not verified routes**:

| Function | Assumed endpoint | Status |
|---|---|---|
| `submitBid` | `POST /bids` | **Confirmed** — matches the curl command from earlier |
| `submitAsk` | `POST /asks` | Guess — mirrors `/bids` by convention |
| `fetchBook` (bids) | `GET /bids?stock_id=...` | Guess |
| `fetchBook` (asks) | `GET /asks?stock_id=...` | Guess |
| `fetchDiagnostics` | `GET /diagnostics/<stock_id>` | Guess |
| `fetchAuctionResult` | `GET /auction/<stock_id>/winner` | Guess |

### To connect it to your real backend

1. Open your Flask app's route definitions (`@app.route(...)` decorators)
   and find the actual paths/methods for listing bids/asks, getting SSM
   diagnostics, and getting the auction winner.
2. Update the `ENDPOINTS` object in `src/api/client.js` to match exactly.
3. Check the response shapes: the mock functions in that file show exactly
   what shape each UI component expects. If your backend returns a
   different shape (e.g. different field names), either adjust your Flask
   response or add a small mapping step in `client.js` — don't change the
   components.
4. Set `VITE_MOCK_MODE=false` in a `.env` file (see `.env.example`), or
   flip the default in `client.js` directly.
5. If your Flask server runs somewhere other than `http://localhost:5051`,
   set `VITE_API_BASE_URL` in `.env` too.

Until step 4, the whole dashboard is fully interactive and demoable against
mock data — useful for confirming the UI/UX before wiring up the real
network calls.

## Design notes

Dark, data-dense "trading terminal" aesthetic — deliberately not a generic
SaaS dashboard look, since the subject (a real limit order book) has its
own visual vocabulary: monospace tabular numerals for prices/quantities
(so columns actually align), functional bid/ask color coding (green/red,
not decorative), and a signature imbalance gauge in the book panel that
directly visualizes `I(t)` — the actual queue imbalance statistic this
whole project is about — rather than a generic loading spinner or icon.

## Project structure

```
src/
  api/client.js           - all backend calls + mock data, isolated here
  components/
    Sidebar.jsx            - instrument selector, connection status
    BookPanel.jsx           - bid/ask ladders + imbalance gauge
    ImbalanceGauge.jsx      - the signature element, visualizes I(t)
    OrderForm.jsx           - submit a bid or ask
    DiagnosticsPanel.jsx    - IMM regime probabilities, event flags
    AuctionPanel.jsx        - auction winners, fill status
  App.jsx                  - layout, polling, state
```

Polling refreshes the book/diagnostics/auction result every 4 seconds
(`POLL_INTERVAL_MS` in `App.jsx`) — adjust or replace with WebSockets later
if you want truly live updates instead of polling.
