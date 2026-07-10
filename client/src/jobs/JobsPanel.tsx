import { useMemo } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useJobsStore } from '../state/JobsProvider'
import type { Job } from '../lib/api'

const NEEDS_USER_COLOR = '#3b82f6'

// Fixed right slide-over (design.md §2.3): overlays content rather than
// pushing layout, and a backdrop click closes only the panel — it does not
// touch JobsChip's own `open` state beyond calling this `onClose`, and
// nothing else on the page is dimmed/blocked (the backdrop is transparent,
// purely a click-catcher — "overlay, not modal" per the spec wording).
export function JobsPanel({ onClose, onReview }: { onClose: () => void; onReview?: (job: Job) => void }) {
  const { jobs, dismissJob } = useJobsStore()

  // Newest-first by created_at (not updated_at) — sorting on updated_at
  // would reorder the list on every progress tick, which reads as the panel
  // shuffling itself under the user's cursor.
  const sorted = useMemo(
    () => [...jobs].sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [jobs],
  )

  return (
    <>
      <div onClick={onClose} style={backdropStyle} />
      <div style={panelStyle} onClick={e => e.stopPropagation()}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>Jobs</h3>
          <button onClick={onClose} style={{ fontSize: 13 }}>close</button>
        </div>
        {sorted.length === 0 && <div style={{ fontSize: 13, color: '#888' }}>No active jobs</div>}
        {sorted.map(job => (
          <JobCard
            key={job.id}
            job={job}
            onReview={onReview}
            onDismiss={() => dismissJob(job.id).catch(e => console.error('[JobsPanel] dismiss failed', e))}
          />
        ))}
      </div>
    </>
  )
}

function JobCard({ job, onReview, onDismiss }: {
  job: Job; onReview?: (job: Job) => void; onDismiss: () => void
}) {
  // delete_retriage jobs carry no payload/name — goal is also empty for them
  // (goal is a 'creation'-only field per the jobs table, spec §1.1), so they
  // get their own fixed title rather than falling through to an empty string.
  const title = job.kind === 'delete_retriage'
    ? 'Reclassifying orphaned threads'
    : (job.payload?.proposal?.name ?? job.goal)

  return (
    <div style={cardStyle}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 600, fontSize: 13 }}>
        {job.needs_user && <Dot />}
        <span>{title}</span>
      </div>
      <JobStageBody job={job} />
      <JobActions job={job} onReview={onReview} onDismiss={onDismiss} />
    </div>
  )
}

function JobStageBody({ job }: { job: Job }) {
  if (job.kind === 'creation') {
    switch (job.stage) {
      case 'proposing': return <StageLine>drafting proposal…</StageLine>
      case 'draft_ready': return <StageLine>ready for review</StageLine>
      case 'backfilling': return <ProgressLine scanned={job.scanned} total={job.total} matched={job.matched} />
      case 'done': return <StageLine>{job.task_kind === 'tracker' ? 'tracker created' : 'bucket live'}</StageLine>
      case 'failed': return <ErrorLine>{job.error ?? 'failed'}</ErrorLine>
      default: return null
    }
  }
  // delete_retriage: running -> done | failed (spec §1.2)
  switch (job.stage) {
    case 'running': return <ProgressLine scanned={job.scanned} total={job.total} matched={job.matched} />
    case 'done': return <StageLine>{job.matched} threads reclassified</StageLine>
    case 'failed': return <ErrorLine>{job.error ?? 'failed'}</ErrorLine>
    default: return null
  }
}

function JobActions({ job, onReview, onDismiss }: {
  job: Job; onReview?: (job: Job) => void; onDismiss: () => void
}) {
  if (job.kind === 'creation') {
    if (job.stage === 'draft_ready') {
      return (
        <div style={actionsRowStyle}>
          <button onClick={() => onReview?.(job)} style={{ fontSize: 12 }}>Review</button>
        </div>
      )
    }
    if (job.stage === 'done') {
      if (job.task_kind === 'tracker' && job.task_id) {
        return (
          <div style={actionsRowStyle}>
            <Link to={`/tasks/${job.task_id}`} style={{ fontSize: 12 }}>Open task</Link>
          </div>
        )
      }
      // done + bucket: no per-bucket detail page to link to (spec §2.3).
      return <div style={actionsRowStyle}><button onClick={onDismiss} style={{ fontSize: 12 }}>dismiss</button></div>
    }
    if (job.stage === 'failed') {
      return <div style={actionsRowStyle}><button onClick={onDismiss} style={{ fontSize: 12 }}>dismiss</button></div>
    }
    return null
  }
  // delete_retriage: only done/failed carry a dismiss action; 'running' has none.
  if (job.stage === 'done' || job.stage === 'failed') {
    return <div style={actionsRowStyle}><button onClick={onDismiss} style={{ fontSize: 12 }}>dismiss</button></div>
  }
  return null
}

function ProgressLine({ scanned, total, matched }: { scanned: number; total: number; matched: number }) {
  const pct = total > 0 ? Math.min(100, Math.round((scanned / total) * 100)) : 0
  return (
    <div style={{ marginTop: 6 }}>
      <div style={{ height: 6, borderRadius: 3, background: '#eee', overflow: 'hidden' }}>
        <div style={{ height: '100%', width: `${pct}%`, background: NEEDS_USER_COLOR }} />
      </div>
      <div style={{ fontSize: 11, color: '#666', marginTop: 4 }}>
        {scanned}/{total} scanned · matched {matched}
      </div>
    </div>
  )
}

function StageLine({ children }: { children: ReactNode }) {
  return <div style={{ color: '#666', fontSize: 12, marginTop: 4 }}>{children}</div>
}

function ErrorLine({ children }: { children: ReactNode }) {
  return <div style={{ color: '#8a1c25', fontSize: 12, marginTop: 4 }}>{children}</div>
}

function Dot() {
  return (
    <span style={{
      width: 8, height: 8, borderRadius: '50%', background: NEEDS_USER_COLOR, display: 'inline-block',
    }} />
  )
}

const backdropStyle: CSSProperties = {
  position: 'fixed', inset: 0, zIndex: 200,
}

const panelStyle: CSSProperties = {
  position: 'fixed', top: 0, right: 0, bottom: 0, width: 360, zIndex: 201,
  background: '#fff', borderLeft: '1px solid #ddd', boxShadow: '-4px 0 16px rgba(0,0,0,0.12)',
  overflowY: 'auto', padding: 16, boxSizing: 'border-box',
  display: 'flex', flexDirection: 'column', gap: 12,
}

const cardStyle: CSSProperties = {
  border: '1px solid #eee', borderRadius: 6, padding: 10,
}

const actionsRowStyle: CSSProperties = {
  marginTop: 8,
}
