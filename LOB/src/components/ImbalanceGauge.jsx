export default function ImbalanceGauge({ value = 0 }) {
  // value in [-1, 1]: negative = ask-heavy (selling pressure), positive = bid-heavy (buying pressure)
  const clamped = Math.max(-1, Math.min(1, value))
  const pct = ((clamped + 1) / 2) * 100 // 0-100 for the fill position

  return (
    <div className="flex flex-col items-center gap-2 px-3">
      <div className="text-[10px] uppercase tracking-widest text-text-muted font-ui">
        Imbalance I(t)
      </div>
      <div className="relative h-40 w-3 rounded-full bg-panel-raised border border-line overflow-hidden">
        <div
          className="absolute left-0 right-0 bottom-1/2 bg-bid transition-all duration-500 ease-out"
          style={{ height: clamped > 0 ? `${clamped * 50}%` : '0%' }}
        />
        <div
          className="absolute left-0 right-0 top-1/2 bg-ask transition-all duration-500 ease-out"
          style={{ height: clamped < 0 ? `${-clamped * 50}%` : '0%' }}
        />
        <div className="absolute left-0 right-0 top-1/2 h-px bg-line" />
      </div>
      <div
        className={`font-mono text-sm font-semibold tabular-nums ${
          clamped > 0.02 ? 'text-bid' : clamped < -0.02 ? 'text-ask' : 'text-text-muted'
        }`}
      >
        {clamped >= 0 ? '+' : ''}
        {clamped.toFixed(3)}
      </div>
    </div>
  )
}
