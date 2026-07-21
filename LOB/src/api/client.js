// ---------------------------------------------------------------------------
// API client for the LOB auction pipeline's Flask backend.
//
// IMPORTANT: the endpoint paths below are BEST-GUESS, based only on the one
// confirmed route from earlier in the project (`POST /bids`). Everything
// else (asks, listing, diagnostics, auction result) is a reasonable guess
// at REST conventions, not a confirmed route. Before treating this as
// wired-up-and-done:
//
//   1. Check your actual Flask app's route definitions (the @app.route(...)
//      decorators in whatever file runs the server on port 5051).
//   2. Update ENDPOINTS below to match exactly.
//   3. Toggle MOCK_MODE to false once confirmed.
//
// Everything in the UI is built against the shapes returned by the mock
// functions below, so as long as your real backend returns matching shapes
// (or you adjust the `map*` functions), the rest of the app doesn't change.
// ---------------------------------------------------------------------------

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5051'

// Flip to false once real endpoints are confirmed and reachable.
export const MOCK_MODE = import.meta.env.VITE_MOCK_MODE !== 'false'

const ENDPOINTS = {
  submitBid: '/bids', // CONFIRMED - POST {buyer, stock_id, price, quantity}
  submitAsk: '/asks', // GUESS - not confirmed, mirrors /bids by convention
  listBids: (stockId) => `/bids?stock_id=${encodeURIComponent(stockId)}`, // GUESS
  listAsks: (stockId) => `/asks?stock_id=${encodeURIComponent(stockId)}`, // GUESS
  diagnostics: (stockId) => `/diagnostics/${encodeURIComponent(stockId)}`, // GUESS - SSM regime output
  auctionResult: (stockId) => `/auction/${encodeURIComponent(stockId)}/winner`, // GUESS
}

async function request(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}${text ? `: ${text}` : ''}`)
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
