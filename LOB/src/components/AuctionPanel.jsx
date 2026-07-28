import { useState } from 'react'
import { endAuction } from '../api/client'

export default function AuctionPanel({ result, loading, stockId, role, onEnded }) {
  const winners = result?.winners ?? []
  const canEndAuction = role === 'seller' || role === 'both'

  const [ending, setEnding] = useState(false)
  const [endMessage, setEndMessage] = useState(null) // { type: 'success'|'error', text }
  const [invoicing, setInvoicing] = useState(null) // per-winner invoice/payment/email results from the last "End auction" call

  async function handleEndAuction() {
    setEnding(true)
    setEndMessage(null)
    setInvoicing(null)
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
        // The promotion succeeding does NOT mean invoicing/payment/emailing
        // succeeded - those are separate steps that can fail independently
        // (see Accounting.py's own caveat about Odoo tax/journal config).
        // Surface them explicitly instead of letting a promoted-but-not-
        // invoiced winner look identical to a fully-settled one.
        setInvoicing(outcome.invoicing ?? [])
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

  function invoicingSummary(entry) {
    if (entry.error) return { ok: false, text: `Failed: ${entry.error}` }
    if (entry.invoice_error) return { ok: false, text: `Invoice error: ${entry.invoice_error}` }
    if (entry.invoice_status && entry.invoice_status !== 'posted') {
      return { ok: false, text: `Invoice not posted (status: ${entry.invoice_status})` }
    }
    const parts = []
    if (entry.invoice_id) parts.push(`Invoice #${entry.invoice_id} posted`)
    if (entry.payment_status) parts.push(`payment: ${entry.payment_status}`)
    if (entry.emailed_to) parts.push(`emailed ${entry.emailed_to}`)
    else if (entry.email_skipped) parts.push('email skipped (no address on file)')
    return { ok: true, text: parts.join(', ') || 'Settled' }
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

          {invoicing && invoicing.length > 0 && (
            <div className="mt-3 pt-3 border-t border-line flex flex-col gap-1.5">
              <span className="text-[11px] text-text-muted font-ui uppercase tracking-wide">
                Invoicing
              </span>
              {invoicing.map((entry, i) => {
                const { ok, text } = invoicingSummary(entry)
                return (
                  <div key={entry.lead_id ?? i} className="text-xs font-ui flex flex-col">
                    <span className={ok ? 'text-bid' : 'text-ask'}>
                      {ok ? '✓' : '✗'} lead {entry.lead_id}: {text}
                    </span>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}