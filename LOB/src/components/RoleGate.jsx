import { useEffect, useState } from 'react'

const VALID_ROLES = ['buyer', 'seller', 'both']
const STORAGE_KEY = 'lob_role'

export function useRole() {
  const [role, setRoleState] = useState(() => {
    const fromUrl = new URLSearchParams(window.location.search).get('role')
    if (VALID_ROLES.includes(fromUrl)) return fromUrl
    const fromStorage = sessionStorage.getItem(STORAGE_KEY)
    if (VALID_ROLES.includes(fromStorage)) return fromStorage
    return null
  })

  useEffect(() => {
    if (!role) return
    sessionStorage.setItem(STORAGE_KEY, role)
    const url = new URL(window.location.href)
    url.searchParams.set('role', role)
    window.history.replaceState({}, '', url)
  }, [role])

  function setRole(next) {
    if (!VALID_ROLES.includes(next)) return
    setRoleState(next)
  }

  function clearRole() {
    sessionStorage.removeItem(STORAGE_KEY)
    const url = new URL(window.location.href)
    url.searchParams.delete('role')
    window.history.replaceState({}, '', url)
    setRoleState(null)
  }

  return { role, setRole, clearRole }
}

const ROLE_COPY = {
  buyer: { title: "I'm buying", desc: 'Submit bids and watch the book from the buy side.' },
  seller: { title: "I'm selling", desc: 'Submit asks and watch the book from the sell side.' },
  both: { title: 'Both / observer', desc: 'Full control — submit either side. Useful for testing or admin.' },
}

export function RolePicker({ onSelect }) {
  return (
    <div className="flex h-screen items-center justify-center bg-ink text-text-primary">
      <div className="w-full max-w-md bg-panel border border-line rounded-xl p-6">
        <h1 className="font-display text-xl font-semibold mb-1">Auction Desk</h1>
        <p className="text-sm text-text-muted font-ui mb-6">How are you joining this session?</p>
        <div className="flex flex-col gap-3">
          {VALID_ROLES.map((r) => (
            <button
              key={r}
              onClick={() => onSelect(r)}
              className="text-left rounded-lg border border-line bg-panel-raised px-4 py-3 hover:border-text-muted transition-colors"
            >
              <div className="font-ui font-semibold text-sm">{ROLE_COPY[r].title}</div>
              <div className="text-xs text-text-muted font-ui mt-0.5">{ROLE_COPY[r].desc}</div>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}