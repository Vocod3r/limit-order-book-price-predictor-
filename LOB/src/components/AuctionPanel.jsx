import { useState } from 'react'
import { endAuction } from '../api/client'

export default function AuctionPanel({ result, loading, stockId, role, onEnded }) {
  const winners = result?.winners ?? []
  const canEndAuction = role === 'seller' || role === 'both'

  const [ending, setEnding] = useState(false)
  const [endMessage, setEndMessage] = useState(null) // { type: 'success'|'error', text }

  async function handleEndAuction() {
    setEnding(true)
    setEndMessage(null)
    try {
      const outcome = await endAuction(stockId, result?.ask_price)

      if (outcome.status === 'ask_price_changed') {
        setEndMessage({
          type: 'error',
          text: 'The ask price changed since your last check — refresh and try again.',
        })
      } else if (outcome.status === 'no_bids' || outcome.status === 'no_winners') {
        setEndMessage({ type: 'error', text: 'No bids cleared the ask price — nothing to award.' })
      } else if (outcome.status === 'executed') {
        const n = outcome.promotion_summary?.promoted ?? outcome.winners.length
        setEndMessage({
          type: 'success',
          text: `Auction ended: ${outcome.total_qty_filled} sh filled, ${n} winner(s) promoted to Odoo sales orders.`,
        })
        onEnded?.()
      } else {
        setEndMessage({ type: 'error', text: 'Unexpected response ending the auction.' })
      }
    } catch (err) {
      setEndMessage({ type: 'error', text: err.message || 'Failed to end auction.' })
    } finally {
      setEnding(false)
    }
  }

  return (
    <div className="bg-panel border border-line rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-display text-lg font-semibold">Auction result</h2>
        {loading && <span className="text-[11px] text-text-muted font-ui">refreshing…</span>}
      </div>

      {winners.length === 0 ? (
        <p className="text-text-muted text-sm font-ui py-3">No cleared trades yet.</p>
      ) : (
        <div className="flex flex-col gap-2 mb-4">
          {winners.map((w, i) => (
            <div
              key={i}
              className="flex items-center justify-between rounded-md px-3 py-2 bg-panel-raised border border-line"
            >
              <span className="text-sm font-ui">{w.buyer}</span>
              <span className="font-mono text-sm tabular-nums text-bid">
                {w.quantity} @ {w.price.toFixed(2)}
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="flex justify-between text-xs font-ui pt-3 border-t border-line">
        <span className="text-text-muted">Filled</span>
        <span className="font-mono tabular-nums">{result?.total_qty_filled ?? 0} sh</span>
      </div>
      <div className="flex justify-between text-xs font-ui mt-1">
        <span className="text-text-muted">Unfilled</span>
        <span className="font-mono tabular-nums">{result?.qty_unfilled ?? 0} sh</span>
      </div>
      {result?.ask_price != null && (
        <div className="flex justify-between text-xs font-ui mt-1">
          <span className="text-text-muted">Current ask</span>
          <span className="font-mono tabular-nums">{result.ask_price.toFixed(2)}</span>
        </div>
      )}

      {canEndAuction && (
        <div className="mt-4 pt-4 border-t border-line">
          <button
            onClick={handleEndAuction}
            disabled={ending || result?.ask_price == null}
            title={result?.ask_price == null ? 'Post an ask before ending the auction' : undefined}
            className="w-full rounded-md py-2 text-sm font-ui font-semibold bg-signal text-ink hover:opacity-90 transition-colors disabled:opacity-50"
          >
            {ending ? 'Ending auction…' : 'End auction'}
          </button>
          <p className="text-[11px] text-text-muted font-ui mt-2">
            Stops accepting new bids for this instrument, awards the current
            winner(s) at the ask price, and pushes them into Odoo.
          </p>
          {endMessage && (
            <p className={`text-xs font-ui mt-2 ${endMessage.type === 'success' ? 'text-bid' : 'text-ask'}`}>
              {endMessage.text}
            </p>
          )}
        </div>
      )}
    </div>
  )
}