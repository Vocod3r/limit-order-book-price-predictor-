import { MOCK_MODE } from '../api/client'

const ROLE_LABEL = { buyer: 'Buyer', seller: 'Seller', both: 'Buyer & seller' }

export default function Sidebar({ stocks, activeStock, onSelectStock, connected, role, onSwitchRole }) {
  return (
    <aside className="w-56 shrink-0 border-r border-line bg-panel flex flex-col">
      <div className="p-5 border-b border-line">
        <h1 className="font-display text-base font-semibold leading-tight">
          Auction Desk
        </h1>
        <p className="text-[11px] text-text-muted font-ui mt-1">LOB pipeline monitor</p>
      </div>

      {role && (
        <div className="px-5 py-3 border-b border-line flex items-center justify-between">
          <span className="text-[11px] font-ui text-text-muted">
            Viewing as <span className="text-text-primary font-medium">{ROLE_LABEL[role]}</span>
          </span>
          {onSwitchRole && (
            <button
              onClick={onSwitchRole}
              className="text-[11px] font-ui text-text-muted hover:text-text-primary underline underline-offset-2"
            >
              Switch
            </button>
          )}
        </div>
      )}

      <div className="p-3 flex flex-col gap-1 flex-1 overflow-y-auto">
        <div className="text-[10px] uppercase tracking-widest text-text-muted font-ui px-2 py-2">
          Instruments
        </div>
        {stocks.map((s) => (
          <button
            key={s}
            onClick={() => onSelectStock(s)}
            className={`text-left px-3 py-2 rounded-md text-sm font-mono transition-colors ${
              s === activeStock
                ? 'bg-panel-raised text-text-primary border border-line'
                : 'text-text-muted hover:text-text-primary'
            }`}
          >
            {s}
          </button>
        ))}
      </div>

      <div className="p-4 border-t border-line flex items-center gap-2">
        <span
          className={`h-2 w-2 rounded-full ${
            MOCK_MODE ? 'bg-signal' : connected ? 'bg-bid' : 'bg-ask'
          }`}
        />
        <span className="text-[11px] font-ui text-text-muted">
          {MOCK_MODE ? 'Mock data mode' : connected ? 'Connected' : 'Disconnected'}
        </span>
      </div>
    </aside>
  )
}