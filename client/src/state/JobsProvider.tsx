import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  confirmJob as apiConfirmJob, createJob, dismissJob as apiDismissJob, getJobs,
  type ConfirmJobBody, type Job, type TaskDetail,
} from '../lib/api'
import { subscribeSse } from '../lib/sse'

// Mounted once in AppShell (inside TasksProvider, for the life of the authed
// session) — same rationale as TasksProvider/InboxProvider: the jobs list
// and its SSE subscription need to survive route navigation instead of
// remounting from scratch (and re-subscribing to SSE) every time the header
// chip that reads it would otherwise unmount/remount.

const POLL_INTERVAL_MS = 15_000

// Spec §1.2 (specs/005_jobs_surface/design.md): a job is terminal once its
// stage is 'done'/'failed' OR it's been dismissed — the literal string
// 'dismissed' never appears in `stage` itself. Exported so JobsChip's own
// "is anything still running" spinner check uses the exact same definition
// the poll-gating effect below does, rather than two definitions drifting.
export function isJobTerminal(job: Job): boolean {
  return job.dismissed_at !== null || job.stage === 'done' || job.stage === 'failed'
}

type JobsStore = {
  jobs: Job[]
  refresh: () => Promise<void>
  startCreation: (goal: string, taskKind: 'tracker' | 'bucket') => Promise<Job>
  confirmJob: (id: string, body: ConfirmJobBody) => Promise<{ task: TaskDetail; job: Job }>
  dismissJob: (id: string) => Promise<void>
}

const JobsStoreContext = createContext<JobsStore | null>(null)

export function JobsProvider({ children }: { children: ReactNode }) {
  const [jobs, setJobs] = useState<Job[]>([])

  const refresh = useCallback(async () => {
    try {
      // active=true (the default) — the panel's normal, always-works poll
      // path (spec §1.4); dismissed/long-stale jobs are excluded server-side.
      const fetched = await getJobs()
      setJobs(fetched)
    } catch (e) {
      console.error('[JobsProvider] refresh failed', e)
    }
  }, [])

  // Initial catch-up fetch. Not redundant with the `_open` handler below —
  // see TasksProvider's identical comment: `_open` only fires for handlers
  // already subscribed before the EventSource connects, and by the time this
  // provider mounts (inside InboxProvider/TasksProvider) that first `_open`
  // is usually long consumed.
  useEffect(() => { void refresh() }, [refresh])

  useEffect(() => {
    return subscribeSse((e) => {
      if (e.event === '_open') {
        void refresh()
        return
      }
      // job_updated is a pure nudge (job_id only, never a row, per this
      // app's SSE convention) — refetch the whole list rather than trying
      // to patch a single job in place.
      if (e.event === 'job_updated') void refresh()
    })
  }, [refresh])

  // 15s poll — belt to SSE's suspenders (spec §2.1): a dropped connection
  // only delays updates until the next tick instead of stranding the panel.
  // Active ONLY while some job is non-terminal, and must start/stop as the
  // jobs list itself changes (not just on mount/unmount) — the load-bearing
  // property here is that this effect's own cleanup (clearInterval) runs
  // every time `hasActive` flips, so the moment the last job finishes there
  // is no interval left ticking in the background.
  const hasActive = jobs.some(j => !isJobTerminal(j))
  useEffect(() => {
    if (!hasActive) return
    const id = setInterval(() => { void refresh() }, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [hasActive, refresh])

  const startCreation = useCallback(async (goal: string, taskKind: 'tracker' | 'bucket'): Promise<Job> => {
    const job = await createJob(goal, taskKind)
    await refresh()
    return job
  }, [refresh])

  const confirmJobFn = useCallback(async (id: string, body: ConfirmJobBody) => {
    const result = await apiConfirmJob(id, body)
    await refresh()
    return result
  }, [refresh])

  const dismissJobFn = useCallback(async (id: string): Promise<void> => {
    await apiDismissJob(id)
    await refresh()
  }, [refresh])

  const value = useMemo(
    () => ({ jobs, refresh, startCreation, confirmJob: confirmJobFn, dismissJob: dismissJobFn }),
    [jobs, refresh, startCreation, confirmJobFn, dismissJobFn],
  )

  return <JobsStoreContext.Provider value={value}>{children}</JobsStoreContext.Provider>
}

export function useJobsStore(): JobsStore {
  const ctx = useContext(JobsStoreContext)
  if (!ctx) {
    throw new Error('useJobsStore must be used within a <JobsProvider> (mounted once in AppShell)')
  }
  return ctx
}
