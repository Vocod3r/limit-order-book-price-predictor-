import { useState, useEffect, useCallback } from 'react'
import Sidebar from './components/Sidebar'
import BookPanel from './components/BookPanel'
import OrderForm from './components/OrderForm'
import DiagnosticsPanel from './components/DiagnosticsPanel'
import AuctionPanel from './components/AuctionPanel'
import { useRole, RolePicker } from './components/RoleGate'
import { fetchBook, fetchDiagnostics, fetchAuctionResult } from './api/client'

const DEFAULT_STOCKS = ['EVENT_TEST_1', 'MSFT', 'GOOG', 'AAPL', 'AMZN', 'TSLA', 'META', 'NFLX', 'NVDA', 'INTC', 'AMD', 'IBM', 'ORCL', 'SAP', 'ADBE', 'CRM', 'UBER', 'LYFT', 'SNAP', 'TWTR']
const CUSTOM_STOCKS_KEY = 'lob_custom_stocks'
const POLL_INTERVAL_MS = 4000

function loadCustomStocks() {
  try {
    const raw = localStorage.getItem(CUSTOM_STOCKS_KEY)
    return raw ? JSON.parse(raw) : []
  } catch {
    return []
  }
}

function saveCustomStocks(list) {
  try {
    localStorage.setItem(CUSTOM_STOCKS_KEY, JSON.stringify(list))
  } catch {
    // ignore storage failures (private browsing, quota, etc.)
  }
}

function normalizeTicker(raw) {
  return raw.trim().toUpperCase().replace(/[^A-Z0-9_.-]/g, '')
}

export default function App() {
  const { role, setRole, clearRole } = useRole()

  const [customStocks, setCustomStocks] = useState(loadCustomStocks)

  const [activeStock, setActiveStock] = useState(() => {
    const fromUrl = normalizeTicker(new URLSearchParams(window.location.search).get('stock') || '')
    return fromUrl || DEFAULT_STOCKS[0]
  })

  // If the active stock isn't in the default list, treat it as a custom one
  // so it shows up in the sidebar (covers someone opening a shared
  // ?stock=NEWTICKER link before ever clicking "Add instrument" themselves).
  useEffect(() => {
    if (DEFAULT_STOCKS.includes(activeStock)) return
    setCustomStocks((prev) => {
      if (prev.includes(activeStock)) return prev
      const next = [...prev, activeStock]
      saveCustomStocks(next)
      return next
    })
  }, [activeStock])

  const stocks = [...DEFAULT_STOCKS, ...customStocks]

  function selectStock(stockId) {
    setActiveStock(stockId)
    const url = new URL(window.location.href)
    url.searchParams.set('stock', stockId)
    window.history.replaceState({}, '', url)
  }

  function addStock(rawTicker) {
    const ticker = normalizeTicker(rawTicker)
    if (!ticker) return
    if (!stocks.includes(ticker)) {
      const next = [...customStocks, ticker]
      setCustomStocks(next)
      saveCustomStocks(next)
    }
    selectStock(ticker) // jump straight to it so fresh bids start landing here
  }

  const [book, setBook] = useState(null)
  const [diagnostics, setDiagnostics] = useState(null)
  const [auctionResult, setAuctionResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [connected, setConnected] = useState(true)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [b, d, a] = await Promise.all([
        fetchBook(activeStock),
        fetchDiagnostics(activeStock),
        fetchAuctionResult(activeStock),
      ])
      setBook(b)
      setDiagnostics(d)
      setAuctionResult(a)
      setConnected(true)
    } catch (err) {
      console.error('Refresh failed:', err)
      setConnected(false)
    } finally {
      setLoading(false)
    }
  }, [activeStock])

  useEffect(() => {
    if (!role) return // no point polling until we know who's asking
    refresh()
    const id = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [role, refresh])

  if (!role) {
    return <RolePicker onSelect={setRole} />
  }

  return (
    <div className="flex h-screen bg-ink text-text-primary">
      <Sidebar
        stocks={stocks}
        activeStock={activeStock}
        onSelectStock={selectStock}
        onAddStock={addStock}
        connected={connected}
        role={role}
        onSwitchRole={clearRole}
      />

      <main className="flex-1 overflow-y-auto p-6">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h2 className="font-display text-2xl font-semibold tabular-nums">
              {activeStock}
            </h2>
            <p className="text-sm text-text-muted font-ui mt-0.5">
              Live auction pipeline monitor
            </p>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          <div className="lg:col-span-2 flex flex-col gap-5">
            <BookPanel book={book} imbalance={diagnostics?.queue_imbalance ?? 0} loading={loading} />
            <OrderForm stockId={activeStock} mode={role} onSubmitted={refresh} />
          </div>

          <div className="flex flex-col gap-5">
            <DiagnosticsPanel diagnostics={diagnostics} loading={loading} />
            <AuctionPanel
              result={auctionResult}
              loading={loading}
              stockId={activeStock}
              role={role}
              onEnded={refresh}
            />
          </div>
        </div>
      </main>
    </div>
  )
}