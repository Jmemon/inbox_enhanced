import { useEffect } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from './auth/useAuth'
import { subscribeSse } from './lib/sse'

const navStyle = ({ isActive }: { isActive: boolean }) => ({
  fontSize: 13, padding: '4px 10px', borderRadius: 6, textDecoration: 'none',
  color: isActive ? '#111' : '#666', background: isActive ? '#eef2f7' : 'transparent',
  fontWeight: isActive ? 600 : 400,
})

export function AppShell() {
  const { state, signOut } = useAuth()

  // Pin the SSE singleton open for the life of the authed shell. lib/sse.ts
  // closes the EventSource when its LAST handler unsubscribes; without this,
  // navigating to a route that mounts no inbox hooks would close the stream,
  // deregister the user from active_users, and stop beat polling.
  useEffect(() => subscribeSse(() => {}), [])

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
      <Outlet />
    </div>
  )
}
