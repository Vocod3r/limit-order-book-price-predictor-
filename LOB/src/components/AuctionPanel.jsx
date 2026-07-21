export default function AuctionPanel({ result, loading }) {
  const winners = result?.winners ?? []

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
    </div>
  )
}
