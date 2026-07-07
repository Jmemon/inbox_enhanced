import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth/useAuth'
import Splash from './auth/Splash'
import Login from './auth/Login'
import { AppShell } from './AppShell'
import HudPage from './pages/hud/HudPage'
import InboxPage from './pages/inbox/InboxPage'
import TaskDetail from './pages/task/TaskDetail'

// Renamed from `Routes` — react-router-dom owns that name now.
function Gate() {
  const { state } = useAuth()
  if (state.status === 'loading') return <Splash />
  if (state.status === 'anon') return <Login />
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/" element={<HudPage />} />
          <Route path="/inbox" element={<InboxPage />} />
          <Route path="/tasks/:taskId" element={<TaskDetail />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  )
}
