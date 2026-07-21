function RegimeBar({ label, value, color }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between text-[11px] font-ui">
        <span className="text-text-muted">{label}</span>
        <span className="font-mono tabular-nums text-text-primary">{(value * 100).toFixed(0)}%</span>
      </div>
      <div className="h-1.5 rounded-full bg-panel-raised overflow-hidden">
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{ width: `${value * 100}%`, backgroundColor: color }}
        />
      </div>
    </div>
  )
}

function EventChip({ label, active }) {
  return (
    <div
      className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-ui border transition-colors ${
        active
          ? 'bg-signal/10 border-signal text-signal'
          : 'bg-panel-raised border-line text-text-muted'
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${active ? 'bg-signal' : 'bg-line'}`} />
      {label}
    </div>
  )
}

export default function DiagnosticsPanel({ diagnostics, loading }) {
  const regime = diagnostics?.regime ?? { stable: 0, bid_depleting: 0, ask_depleting: 0 }
  const events = diagnostics?.events ?? {}

  return (
    <div className="bg-panel border border-line rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-display text-lg font-semibold">Filter diagnostics</h2>
        {loading && <span className="text-[11px] text-text-muted font-ui">refreshing…</span>}
      </div>

      <div className="flex flex-col gap-3 mb-5">
        <RegimeBar label="Stable" value={regime.stable} color="var(--color-text-muted)" />
        <RegimeBar label="Bid depleting" value={regime.bid_depleting} color="var(--color-ask)" />
        <RegimeBar label="Ask depleting" value={regime.ask_depleting} color="var(--color-bid)" />
      </div>

      <div className="flex flex-col gap-2 mb-5 text-sm font-ui">
        <div className="flex justify-between">
          <span className="text-text-muted">Mid price</span>
          <span className="font-mono tabular-nums">{diagnostics?.mid_price?.toFixed(2) ?? '—'}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-text-muted">Spread</span>
          <span className="font-mono tabular-nums">{diagnostics?.spread?.toFixed(2) ?? '—'}</span>
        </div>
      </div>

      <div className="text-[10px] uppercase tracking-widest text-text-muted font-ui mb-2">
        Events
      </div>
      <div className="flex flex-wrap gap-2">
        <EventChip label="Ask depleted" active={events.ask_depleted} />
        <EventChip label="New higher bid" active={events.new_higher_bid} />
        <EventChip label="Bid depleted" active={events.bid_depleted} />
        <EventChip label="New lower ask" active={events.new_lower_ask} />
      </div>
    </div>
  )
}
