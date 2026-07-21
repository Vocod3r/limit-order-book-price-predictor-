import ImbalanceGauge from './ImbalanceGauge'

function Ladder({ side, rows }) {
  const isBid = side === 'bid'
  const accent = isBid ? 'text-bid' : 'text-ask'
  const label = isBid ? 'Bids' : 'Asks'
  const nameKey = isBid ? 'buyer' : 'seller'

  return (
    <div className="flex-1">
      <div
        className={`text-[10px] uppercase tracking-widest font-ui mb-2 ${accent} ${
          isBid ? 'text-right' : 'text-left'
        }`}
      >
        {label}
      </div>
      <div className="flex flex-col gap-1">
        {rows.length === 0 && (
          <div className="text-text-muted text-xs font-ui py-4 text-center">No {label.toLowerCase()} yet</div>
        )}
        {rows.map((row) => (
          <div
            key={row.id}
            className={`flex items-center justify-between rounded-md px-3 py-2 bg-panel-raised border border-line ${
              isBid ? 'flex-row-reverse text-right' : 'text-left'
            }`}
          >
            <div className="flex flex-col">
              <span className="font-mono text-sm font-semibold tabular-nums">
                {row.price.toFixed(2)}
              </span>
              <span className="text-[11px] text-text-muted font-mono tabular-nums">
                {row.quantity} sh
              </span>
            </div>
            <span className="text-[11px] text-text-muted font-ui truncate max-w-[110px]">
              {row[nameKey]}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function BookPanel({ book, imbalance, loading }) {
  return (
    <div className="bg-panel border border-line rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-display text-lg font-semibold">Order Book</h2>
        {loading && <span className="text-[11px] text-text-muted font-ui">refreshing…</span>}
      </div>
      <div className="flex items-stretch gap-2">
        <Ladder side="bid" rows={book?.bids ?? []} />
        <ImbalanceGauge value={imbalance} />
        <Ladder side="ask" rows={book?.asks ?? []} />
      </div>
    </div>
  )
}
