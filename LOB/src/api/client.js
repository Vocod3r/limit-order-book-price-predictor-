// ---------------------------------------------------------------------------
// API client for the LOB auction pipeline's Flask backend.
//
// All endpoints below are now CONFIRMED against Test_bid_generation.py:
//   POST /bids, GET /bids            - submit / list resting bids
//   POST /asks, GET /asks            - submit / read current best ask
//   GET  /diagnostics/:stock_id      - latest LOB_ssm regime/imbalance/events
//   GET  /auction/:stock_id/winner   - live, read-only auction preview
//   POST /auction/:stock_id/end      - actually ends the auction: determines
//                                       winner(s), promotes them into Odoo
//                                       (crm.lead -> opportunity -> sale.order),
//                                       depletes the ask book by the fill
//
// DEPLOYING NON-LOCALLY:
//   Set VITE_API_BASE_URL in your hosting provider's env vars (Vercel,
//   Netlify, etc.) to your deployed Flask backend's public URL, e.g.
//   https://your-backend.onrender.com — NOT localhost. See DEPLOY.md.
// ---------------------------------------------------------------------------

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5051'

// Flip to false once real endpoints are confirmed and reachable.
export const MOCK_MODE = import.meta.env.VITE_MOCK_MODE !== 'false'

// Guard against the #1 "works on my machine" deploy bug: shipping a build
// that's still silently pointed at localhost because VITE_API_BASE_URL
// wasn't set in the hosting provider's dashboard.
if (
  !MOCK_MODE &&
  API_BASE.includes('localhost') &&
  typeof window !== 'undefined' &&
  !['localhost', '127.0.0.1'].includes(window.location.hostname)
) {
  console.warn(
    '[client.js] MOCK_MODE is off but VITE_API_BASE_URL is unset or still ' +
      'points at localhost, while the app itself is running on ' +
      `"${window.location.hostname}". Requests to the backend will fail. ` +
      'Set VITE_API_BASE_URL to your deployed backend URL — see DEPLOY.md.'
  )
}

const ENDPOINTS = {
  submitBid: '/bids',
  submitAsk: '/asks',
  listBids: (stockId) => `/bids?stock_id=${encodeURIComponent(stockId)}`,
  listAsks: (stockId) => `/asks?stock_id=${encodeURIComponent(stockId)}`,
  diagnostics: (stockId) => `/diagnostics/${encodeURIComponent(stockId)}`,
  auctionResult: (stockId) => `/auction/${encodeURIComponent(stockId)}/winner`,
  auctionEnd: (stockId) => `/auction/${encodeURIComponent(stockId)}/end`,
}

async function request(path, options = {}) {
  let res
  try {
    res = await fetch(`${API_BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    })
  } catch (err) {
    // fetch() throws a bare "Failed to fetch" / TypeError for network errors
    // AND for CORS rejections, which is the most common non-local deploy
    // failure. Make that failure mode legible instead of a cryptic error.
    throw new Error(
      `Could not reach ${API_BASE}${path}. If this just started happening ` +
        'after deploying, check: (1) VITE_API_BASE_URL is set correctly, ' +
        '(2) the backend is actually running/reachable, (3) the backend\'s ' +
        `CORS config allows requests from ${typeof window !== 'undefined' ? window.location.origin : 'this origin'}. ` +
        `Original error: ${err.message}`
    )
  }
  if (!res.ok) {
    // 409 (ask price changed) and other structured error bodies still carry
    // useful JSON - surface it instead of just the status text.
    const body = await res.json().catch(() => null)
    const message = body?.error || res.statusText
    const error = new Error(`${res.status}: ${message}`)
    error.status = res.status
    error.body = body
    throw error
  }
  return res.json()
}

// --- Mock data, used when MOCK_MODE is true --------------------------------

function mockBook() {
  return {
    bids: [
      { id: 'b1', buyer: 'Ironwood Capital', price: 258.5, quantity: 200 },
      { id: 'b2', buyer: 'Kestrel Partners', price: 258.25, quantity: 150 },
      { id: 'b3', buyer: 'Marlin Trading', price: 258.0, quantity: 300 },
    ],
    asks: [
      { id: 'a1', seller: 'Basalt Securities', price: 259.0, quantity: 180 },
      { id: 'a2', seller: 'Onyx Capital', price: 259.25, quantity: 220 },
      { id: 'a3', seller: 'Cedarline LLC', price: 259.5, quantity: 140 },
    ],
  }
}

function mockDiagnostics() {
  return {
    regime: { stable: 0.62, bid_depleting: 0.21, ask_depleting: 0.17 },
    queue_imbalance: 0.184,
    events: {
      ask_depleted: false,
      new_higher_bid: true,
      bid_depleted: false,
      new_lower_ask: false,
    },
    mid_price: 258.75,
    spread: 0.5,
  }
}

function mockAuctionResult() {
  return {
    status: 'preview',
    winners: [{ buyer: 'Ironwood Capital', price: 259.0, quantity: 200 }],
    total_qty_filled: 200,
    qty_unfilled: 0,
    ask_price: 259.0,
  }
}

// --- Public API --------------------------------------------------------

export async function submitBid({ buyer, stockId, price, quantity }) {
  if (MOCK_MODE) {
    await new Promise((r) => setTimeout(r, 250))
    return { ok: true, id: `mock-${Date.now()}` }
  }
  return request(ENDPOINTS.submitBid, {
    method: 'POST',
    body: JSON.stringify({ buyer, stock_id: stockId, price, quantity }),
  })
}

export async function submitAsk({ seller, stockId, price, quantity }) {
  if (MOCK_MODE) {
    await new Promise((r) => setTimeout(r, 250))
    return { ok: true, id: `mock-${Date.now()}` }
  }
  // NOTE: the backend's AskBook only tracks a single best-level ask per
  // stock (no seller identity persisted) - `seller` is accepted here for
  // a consistent call shape but isn't stored server-side yet.
  return request(ENDPOINTS.submitAsk, {
    method: 'POST',
    body: JSON.stringify({ seller, stock_id: stockId, price, quantity }),
  })
}

export async function fetchBook(stockId) {
  if (MOCK_MODE) {
    await new Promise((r) => setTimeout(r, 200))
    return mockBook()
  }
  const [bids, asks] = await Promise.all([
    request(ENDPOINTS.listBids(stockId)),
    request(ENDPOINTS.listAsks(stockId)),
  ])
  return { bids, asks }
}

export async function fetchDiagnostics(stockId) {
  if (MOCK_MODE) {
    await new Promise((r) => setTimeout(r, 200))
    return mockDiagnostics()
  }
  return request(ENDPOINTS.diagnostics(stockId))
}

export async function fetchAuctionResult(stockId) {
  if (MOCK_MODE) {
    await new Promise((r) => setTimeout(r, 200))
    return mockAuctionResult()
  }
  return request(ENDPOINTS.auctionResult(stockId))
}

// Ends the auction: determines the winner(s) against the CURRENT ask and
// promotes them into Odoo. expectedAskPrice is optional - pass the
// ask_price you last saw from fetchAuctionResult() so the backend can
// reject (409) if the ask changed underneath you since then.
export async function endAuction(stockId, expectedAskPrice) {
  if (MOCK_MODE) {
    await new Promise((r) => setTimeout(r, 300))
    return {
      status: 'executed',
      ask_price: expectedAskPrice ?? 259.0,
      winners: [{ buyer: 'Ironwood Capital', price: expectedAskPrice ?? 259.0, quantity: 200 }],
      total_qty_filled: 200,
      qty_unfilled: 0,
      promotion_summary: { total: 1, promoted: 1, already_promoted: 0, dry_run: 0, errors: [] },
    }
  }
  return request(ENDPOINTS.auctionEnd(stockId), {
    method: 'POST',
    body: JSON.stringify(
      expectedAskPrice != null ? { expected_ask_price: expectedAskPrice } : {}
    ),
  })
}