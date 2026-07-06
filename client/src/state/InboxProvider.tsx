import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'
import { useBuckets } from '../pages/buckets/useBuckets'
import { useInbox } from '../pages/inbox/useInbox'
import { useInboxSse } from '../pages/inbox/useInboxSse'

// Mounted once in AppShell for the life of the authed session — inbox state
// (buckets/idLayer/displayLayer/pagination/filter) survives route navigation
// between `/` (HUD) and `/inbox` instead of each route holding a
// component-local copy that remounts from scratch. Its permanent
// `useInboxSse` subscription keeps the SSE singleton open across navigation
// (superseding the old AppShell no-op pin) and keeps the inbox current even
// while the user is sitting on the HUD, not the inbox list.

type InboxStore = {
  buckets: ReturnType<typeof useBuckets>
  inbox: ReturnType<typeof useInbox>
  filterSelection: Set<string> | null
  setFilterSelection: (next: Set<string> | null) => void
}

const InboxStoreContext = createContext<InboxStore | null>(null)

export function InboxProvider({ children }: { children: ReactNode }) {
  const buckets = useBuckets()
  const [filterSelection, setFilterSelection] = useState<Set<string> | null>(null)
  const inbox = useInbox({ buckets: buckets.buckets, filterSelection })
  useInboxSse({ onApply: inbox.applyThreadUpdates, snapshot: inbox.snapshot })

  const value = useMemo(
    () => ({ buckets, inbox, filterSelection, setFilterSelection }),
    [buckets, inbox, filterSelection],
  )

  return <InboxStoreContext.Provider value={value}>{children}</InboxStoreContext.Provider>
}

export function useInboxStore(): InboxStore {
  const ctx = useContext(InboxStoreContext)
  if (!ctx) {
    throw new Error('useInboxStore must be used within an <InboxProvider> (mounted once in AppShell)')
  }
  return ctx
}
