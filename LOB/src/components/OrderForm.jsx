import { useState } from 'react'
import { submitBid, submitAsk } from '../api/client'

export default function OrderForm({ stockId, mode = 'both', onSubmitted }) {
  // mode: 'buyer' locks to bids, 'seller' locks to asks, 'both' shows the toggle
  const [side, setSide] = useState(mode === 'seller' ? 'ask' : 'bid')
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [price, setPrice] = useState('')
  const [quantity, setQuantity] = useState('')
  const [status, setStatus] = useState(null) // null | 'sending' | 'sent' | 'error'
  const [errorMsg, setErrorMsg] = useState('')

  const isBid = mode === 'buyer' ? true : mode === 'seller' ? false : side === 'bid'
  const canToggle = mode === 'both'

  async function handleSubmit(e) {
    e.preventDefault()
    if (!name || !price || !quantity) return

    setStatus('sending')
    setErrorMsg('')
    try {
      if (isBid) {
        await submitBid({ buyer: name, buyerEmail: email || undefined, stockId, price: Number(price), quantity: Number(quantity) })
      } else {
        await submitAsk({ seller: name, stockId, price: Number(price), quantity: Number(quantity) })
      }
      setStatus('sent')
      setPrice('')
      setQuantity('')
      onSubmitted?.()
      setTimeout(() => setStatus(null), 1500)
    } catch (err) {
      setStatus('error')
      setErrorMsg(err.message || 'Submission failed')
    }
  }

  return (
    <form onSubmit={handleSubmit} className="bg-panel border border-line rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-display text-lg font-semibold">
          {mode === 'buyer' ? 'Place bid' : mode === 'seller' ? 'Place ask' : 'Place order'}
        </h2>
        {canToggle && (
          <div className="flex rounded-lg border border-line overflow-hidden">
            <button
              type="button"
              onClick={() => setSide('bid')}
              className={`px-3 py-1.5 text-xs font-ui font-medium transition-colors ${
                isBid ? 'bg-bid-dim text-bid' : 'text-text-muted hover:text-text-primary'
              }`}
            >
              Buy
            </button>
            <button
              type="button"
              onClick={() => setSide('ask')}
              className={`px-3 py-1.5 text-xs font-ui font-medium transition-colors ${
                !isBid ? 'bg-ask-dim text-ask' : 'text-text-muted hover:text-text-primary'
              }`}
            >
              Sell
            </button>
          </div>
        )}
      </div>

      <div className="flex flex-col gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-[11px] uppercase tracking-wide text-text-muted font-ui">
            {isBid ? 'Buyer' : 'Seller'}
          </span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Firm name"
            className="bg-panel-raised border border-line rounded-md px-3 py-2 text-sm font-ui outline-none focus:border-text-muted"
          />
        </label>

        {isBid && (
          <label className="flex flex-col gap-1">
            <span className="text-[11px] uppercase tracking-wide text-text-muted font-ui">
              Email <span className="normal-case text-text-muted">(for invoicing if you win)</span>
            </span>
            <input
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              type="email"
              placeholder="you@company.com"
              className="bg-panel-raised border border-line rounded-md px-3 py-2 text-sm font-ui outline-none focus:border-text-muted"
            />
          </label>
        )}

        <div className="flex gap-3">
          <label className="flex flex-col gap-1 flex-1">
            <span className="text-[11px] uppercase tracking-wide text-text-muted font-ui">Price</span>
            <input
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              type="number"
              step="0.01"
              placeholder="0.00"
              className="bg-panel-raised border border-line rounded-md px-3 py-2 text-sm font-mono tabular-nums outline-none focus:border-text-muted"
            />
          </label>
          <label className="flex flex-col gap-1 flex-1">
            <span className="text-[11px] uppercase tracking-wide text-text-muted font-ui">Quantity</span>
            <input
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              type="number"
              placeholder="0"
              className="bg-panel-raised border border-line rounded-md px-3 py-2 text-sm font-mono tabular-nums outline-none focus:border-text-muted"
            />
          </label>
        </div>

        <button
          type="submit"
          disabled={status === 'sending'}
          className={`mt-1 rounded-md py-2 text-sm font-ui font-semibold transition-colors disabled:opacity-50 ${
            isBid ? 'bg-bid text-ink hover:opacity-90' : 'bg-ask text-ink hover:opacity-90'
          }`}
        >
          {status === 'sending' ? 'Submitting…' : isBid ? 'Submit bid' : 'Submit ask'}
        </button>

        {status === 'sent' && (
          <p className="text-xs text-bid font-ui">Order submitted.</p>
        )}
        {status === 'error' && (
          <p className="text-xs text-ask font-ui">Failed: {errorMsg}</p>
        )}
      </div>
    </form>
  )
}