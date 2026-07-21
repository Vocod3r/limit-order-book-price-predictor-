import { useState, useEffect, useCallback } from 'react'
import Sidebar from './components/Sidebar'
import BookPanel from './components/BookPanel'
import OrderForm from './components/OrderForm'
import DiagnosticsPanel from './components/DiagnosticsPanel'
import AuctionPanel from './components/AuctionPanel'
import { fetchBook, fetchDiagnostics, fetchAuctionResult } from './api/client'

const STOCKS = ['EVENT_TEST_1', 'MSFT', 'GOOG', 'AAPL']
const POLL_INTERVAL_MS = 4000

export default function App() {
  const [activeStock, setActiveStock] = useState(STOCKS[0])
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
    refresh()
    const id = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [refresh])

  return (
    <div className="flex h-screen bg-ink text-text-primary">
      <Sidebar
        stocks={STOCKS}
        activeStock={activeStock}
        onSelectStock={setActiveStock}
        connected={connected}
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
            <OrderForm stockId={activeStock} onSubmitted={refresh} />
          </div>

          <div className="flex flex-col gap-5">
            <DiagnosticsPanel diagnostics={diagnostics} loading={loading} />
            <AuctionPanel result={auctionResult} loading={loading} />
          </div>
        </div>
      </main>
    </div>
  )
}
