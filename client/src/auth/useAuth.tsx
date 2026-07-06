import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { getJSON, postEmpty } from '../lib/api'

export type Me = { id: string; email: string; name: string | null }

type AuthState =
  | { status: 'loading' }
  | { status: 'authed'; user: Me }
  | { status: 'anon' }

type Ctx = {
  state: AuthState
  refresh: () => Promise<void>
  recheckSession: () => Promise<void>
  signOut: () => Promise<void>
}

const AuthContext = createContext<Ctx | null>(null)

// Shared fetch used by both refresh() and recheckSession() so the two entry
// points can't drift on what "authed" means. Callers decide how to interpret
// a thrown error.
async function fetchMe(): Promise<Me> {
  return getJSON<Me>('/auth/me')
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: 'loading' })

  // Mount-time (and manual) refresh: any failure — network outage, 5xx,
  // timeout, or a real 401 — is treated as "session dead". This is correct
  // here because a failed initial load has no session to preserve.
  const refresh = useCallback(async () => {
    try {
      const me = await fetchMe()
      setState({ status: 'authed', user: me })
    } catch (e: any) {
      setState({ status: 'anon' })
    }
  }, [])

  // Escape-hatch re-check for an *already-authed* session that's seeing SSE
  // trouble (see AppShell's error-streak handler). Unlike refresh(), this
  // must not treat "the network/backend is unreachable" as "logged out" —
  // only a definitive 401 (getJSON's `{kind:'unauthorized'}`) means the
  // session itself is dead. Any other failure shape leaves state untouched;
  // the SSE backoff keeps retrying and a later successful recheck or
  // reconnect resolves it.
  const recheckSession = useCallback(async () => {
    try {
      const me = await fetchMe()
      setState({ status: 'authed', user: me })
    } catch (e: any) {
      if (e?.kind === 'unauthorized') {
        setState({ status: 'anon' })
      } else {
        console.warn('[auth] recheckSession failed (non-401), leaving session state as-is', e)
      }
    }
  }, [])

  const signOut = useCallback(async () => {
    await postEmpty('/auth/logout')
    setState({ status: 'anon' })
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  return (
    <AuthContext.Provider value={{ state, refresh, recheckSession, signOut }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): Ctx {
  const v = useContext(AuthContext)
  if (!v) throw new Error('useAuth outside AuthProvider')
  return v
}
