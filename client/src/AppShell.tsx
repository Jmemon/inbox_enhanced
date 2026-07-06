import { useEffect, useRef } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from './auth/useAuth'
import { InboxProvider } from './state/InboxProvider'
import { subscribeSse } from './lib/sse'

// Auth-death escape (see lib/sse.ts backoff): an expired session cookie
// makes every SSE reconnect fail with a fresh 401 forever, since nothing
// about the stream itself tells the app the *session* — not just the
// connection — is dead. Count consecutive '_error' events (reset by any
// '_open'); once 3 land in a row, re-validate via useAuth().recheckSession().
// Connection-dead is NOT session-dead: a backend outage fails both the SSE
// stream and this re-check's /auth/me call for reasons that have nothing to
// do with the cookie, so recheckSession() only flips auth state to 'anon' on
// a definitive 401 — any other failure leaves state alone and the SSE
// backoff keeps retrying. Only on a real 401 does state flip to 'anon',
// which unmounts <AppShell> (and with it InboxProvider's SSE subscription),
// ending the error loop and landing the user back on the login screen.
// Throttled to at most once per 30s so a merely-flaky connection can't
// hammer /auth/me either.
const AUTH_RECHECK_THRESHOLD = 3
const AUTH_RECHECK_THROTTLE_MS = 30_000

const navStyle = ({ isActive }: { isActive: boolean }) => ({
  fontSize: 13, padding: '4px 10px', borderRadius: 6, textDecoration: 'none',
  color: isActive ? '#111' : '#666', background: isActive ? '#eef2f7' : 'transparent',
  fontWeight: isActive ? 600 : 400,
})

export function AppShell() {
  const { state, signOut, recheckSession } = useAuth()

  const consecutiveErrorsRef = useRef(0)
  const lastRecheckAtRef = useRef(0)

  useEffect(() => subscribeSse((e) => {
    if (e.event === '_open') {
      consecutiveErrorsRef.current = 0
      return
    }
    if (e.event !== '_error') return
    consecutiveErrorsRef.current += 1
    if (consecutiveErrorsRef.current < AUTH_RECHECK_THRESHOLD) return
    const now = Date.now()
    if (now - lastRecheckAtRef.current < AUTH_RECHECK_THROTTLE_MS) return
    lastRecheckAtRef.current = now
    void recheckSession()
  }), [recheckSession])

  if (state.status !== 'authed') return null
  return (
    <div style={{ fontFamily: 'system-ui, sans-serif', minHeight: '100vh' }}>
      <header style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '12px 24px', borderBottom: '1px solid #eee',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
          <div style={{ fontWeight: 600 }}>inbox concierge</div>
          <nav style={{ display: 'flex', gap: 6 }}>
            <NavLink to="/" end style={navStyle}>HUD</NavLink>
            <NavLink to="/inbox" style={navStyle}>Inbox</NavLink>
          </nav>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 14, color: '#444' }}>{state.user.name ?? state.user.email}</span>
          <button onClick={signOut} style={{ fontSize: 13, padding: '6px 10px' }}>sign out</button>
        </div>
      </header>
      <InboxProvider>
        <Outlet />
      </InboxProvider>
    </div>
  )
}
