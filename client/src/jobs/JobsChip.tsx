import { useState } from 'react'
import { isJobTerminal, useJobsStore } from '../state/JobsProvider'
import { JobsPanel } from './JobsPanel'
import type { Job } from '../lib/api'

// Matches design.md's `[◌ Jobs N]` chip: a spinner while anything is still
// running, a small blue dot when anything needs a user decision, and a
// click toggles the slide-over panel. Owns its own open/closed state rather
// than lifting it into AppShell — the brief left "chip owns `open`, or a
// separate JobsIndicator wrapper" as this task's call; a wrapper felt like
// an extra file for one boolean, so the chip renders its own panel as a
// sibling instead. `onReview` just threads through to the panel (Task 6
// wires an actual handler; both this and JobsPanel default it to a no-op).
export function JobsChip({ onReview }: { onReview?: (job: Job) => void }) {
  const { jobs } = useJobsStore()
  const [open, setOpen] = useState(false)

  // JobsProvider's `jobs` is already the active-only fetch (getJobs()'s
  // default `active=true` — non-dismissed, non-stale-terminal per the
  // server's `list_jobs`), so zero here really does mean "nothing to show".
  if (jobs.length === 0) return null

  const anyRunning = jobs.some(j => !isJobTerminal(j))
  const anyNeedsUser = jobs.some(j => j.needs_user)

  return (
    <div style={{ position: 'relative' }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          display: 'flex', alignItems: 'center', gap: 6, fontSize: 13,
          padding: '4px 10px', borderRadius: 6, border: '1px solid #ddd',
          background: open ? '#eef2f7' : '#fff', cursor: 'pointer',
        }}
      >
        {anyRunning && <Spinner />}
        <span>Jobs {jobs.length}</span>
        {anyNeedsUser && <Dot />}
      </button>
      {open && <JobsPanel onClose={() => setOpen(false)} onReview={onReview} />}
    </div>
  )
}

// The app's blue-dot idiom (design.md §1.1/§2.2) for "needs a decision" —
// #3b82f6 is the spec's own named color for it.
function Dot() {
  return (
    <span style={{
      width: 8, height: 8, borderRadius: '50%', background: '#3b82f6', display: 'inline-block',
    }} />
  )
}

// No existing spinner/animation idiom anywhere else in client/src (checked:
// no @keyframes/animation in the codebase) — this defines its own scoped
// @keyframes rather than pulling in a dependency for one glyph.
function Spinner() {
  return (
    <>
      <style>{'@keyframes jobs-chip-spin { to { transform: rotate(360deg) } }'}</style>
      <span style={{
        display: 'inline-block', width: 10, height: 10, borderRadius: '50%',
        border: '2px solid #ccc', borderTopColor: '#666',
        animation: 'jobs-chip-spin 0.8s linear infinite',
      }} />
    </>
  )
}
