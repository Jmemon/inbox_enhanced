import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  createTask as apiCreateTask, deleteTask as apiDeleteTask, getTask, getTasks, patchTask as apiPatchTask,
  type Task, type TaskDetail,
} from '../lib/api'
import { subscribeSse } from '../lib/sse'

// Mounted once in AppShell (inside InboxProvider, for the life of the authed
// session) — same rationale as InboxProvider: the task list/detail cache and
// its one SSE subscription need to survive route navigation between the HUD
// and any future per-task views, instead of remounting from scratch (and
// re-subscribing to SSE) every time a route unmounts its component-local copy.

type Backfill = { scanned: number; matched: number; done: boolean }

type TasksStore = {
  tasks: Task[]
  byId: Record<string, Task>
  refresh: () => Promise<void>
  getDetail: (id: string) => TaskDetail | undefined
  loadDetail: (id: string) => Promise<TaskDetail | null>
  backfill: Record<string, Backfill>
  createTask: (body: Parameters<typeof apiCreateTask>[0]) => Promise<TaskDetail>
  patchTask: (id: string, body: Parameters<typeof apiPatchTask>[1]) => Promise<TaskDetail>
  deleteTask: (id: string) => Promise<void>
}

const TasksStoreContext = createContext<TasksStore | null>(null)

// TaskDetail = Task & { state_schema }. task_updated's "refetch detail+list
// item" contract (see loadDetail below) is satisfied from a single GET
// /api/tasks/{id} response by dropping state_schema back off — no second
// fetch needed to keep the `tasks` list item current.
function taskFromDetail(detail: TaskDetail): Task {
  const { state_schema: _state_schema, ...task } = detail
  return task
}

export function TasksProvider({ children }: { children: ReactNode }) {
  const [tasks, setTasks] = useState<Task[]>([])
  const [details, setDetails] = useState<Record<string, TaskDetail>>({})
  const [backfill, setBackfill] = useState<Record<string, Backfill>>({})

  const byId = useMemo(() => {
    const m: Record<string, Task> = {}
    for (const t of tasks) m[t.id] = t
    return m
  }, [tasks])

  // The SSE subscription below is mounted exactly once (see its effect) and
  // its handler closure is therefore fixed at whatever `byId` looked like on
  // mount (empty). Reading `byIdRef.current` instead of `byId` directly is
  // what lets that long-lived closure see the CURRENT task list on every
  // event — same stale-closure trap useInbox.tsx avoids via its
  // `lastInternalDate` ref rather than reading `displayLayer` inline.
  const byIdRef = useRef(byId)
  useEffect(() => { byIdRef.current = byId }, [byId])

  const refresh = useCallback(async () => {
    try {
      const { tasks: fetched } = await getTasks()
      setTasks(fetched)
    } catch (e) {
      console.error('[TasksProvider] refresh failed', e)
    }
  }, [])

  const loadDetail = useCallback(async (id: string): Promise<TaskDetail | null> => {
    try {
      const detail = await getTask(id)
      setDetails(prev => ({ ...prev, [id]: detail }))
      setTasks(prev => prev.map(t => (t.id === id ? taskFromDetail(detail) : t)))
      return detail
    } catch (e) {
      // api.ts's getJSON (which getTask uses) throws `{kind: 'unauthorized'}`
      // (a plain object, not an Error) for 401s, and a generic
      // `Error(\`${status} ${statusText}\`)` for every other non-ok response —
      // there is no structured shape carrying the status code for 404s
      // specifically. Detecting it is therefore a substring match on the
      // Error message (e.g. "404 Not Found") rather than a status check.
      // Anything else (network failure, 401, 5xx) falls through to the log
      // + rethrow below — only a 404 evicts, per the task_updated DELETE case.
      const is404 = e instanceof Error && e.message.includes('404')
      if (is404) {
        setDetails(prev => {
          const { [id]: _evicted, ...rest } = prev
          return rest
        })
        void refresh()
        return null
      }
      console.error('[TasksProvider] loadDetail failed', id, e)
      throw e
    }
  }, [refresh])

  const getDetail = useCallback((id: string) => details[id], [details])

  const createTask = useCallback(async (body: Parameters<typeof apiCreateTask>[0]): Promise<TaskDetail> => {
    const detail = await apiCreateTask(body)
    setDetails(prev => ({ ...prev, [detail.id]: detail }))
    await refresh()
    return detail
  }, [refresh])

  const patchTask = useCallback(async (id: string, body: Parameters<typeof apiPatchTask>[1]): Promise<TaskDetail> => {
    const detail = await apiPatchTask(id, body)
    setDetails(prev => ({ ...prev, [id]: detail }))
    await refresh()
    return detail
  }, [refresh])

  const deleteTask = useCallback(async (id: string): Promise<void> => {
    await apiDeleteTask(id)
    setDetails(prev => {
      const { [id]: _evicted, ...rest } = prev
      return rest
    })
    await refresh()
  }, [refresh])

  // Initial catch-up fetch. This is NOT redundant with the `_open` handler
  // below: `_open` only fires for handlers that were already subscribed
  // *before* the EventSource connects. TasksProvider mounts inside
  // InboxProvider, which has usually already opened (and consumed) the SSE
  // singleton's first `_open` via its own useInboxSse subscription by the
  // time this effect runs, so without this explicit fetch the task list
  // would stay empty until the next reconnect.
  useEffect(() => { void refresh() }, [refresh])

  useEffect(() => {
    // One persistent subscription for the store's lifetime. `refresh` and
    // `loadDetail` are both useCallback-stable (their own dep chains never
    // change identity), so listing them here is exhaustive-deps-correct
    // without ever causing a resubscribe — this effect body runs once.
    return subscribeSse((e) => {
      if (e.event === '_open') {
        // Reconnect catch-up: any task_updated events published while the
        // stream was down are otherwise lost, so pull the canonical list.
        void refresh()
        return
      }
      if (e.event === 'task_updated') {
        const known = byIdRef.current[e.task_id]
        if (!known) {
          // Unknown task_id — created from elsewhere (another tab, or a
          // draft-confirm flow that hasn't refreshed this store yet). A
          // single-item fetch has no list position to insert into, so fall
          // back to a full list refresh.
          void refresh()
          return
        }
        // 2A contract: reject / attach / detach and DELETE publishes do NOT
        // bump `version` (only edits to name/status/state_schema do), so a
        // version-only comparison would silently miss them. pending_count is
        // the load-bearing second signal here — it moves on every
        // review-queue mutation regardless of whether version changed.
        // loadDetail's own 404 handling covers the DELETE case (evict + list
        // refresh) for free.
        if (e.version > known.version || e.pending_count !== known.summary.pending_reviews) {
          void loadDetail(e.task_id)
        }
        return
      }
      if (e.event === 'task_backfill_progress') {
        setBackfill(prev => ({
          ...prev,
          [e.task_id]: { scanned: e.scanned, matched: e.matched, done: e.done },
        }))
        if (e.done) void loadDetail(e.task_id)
        return
      }
    })
  }, [refresh, loadDetail])

  const value = useMemo(
    () => ({ tasks, byId, refresh, getDetail, loadDetail, backfill, createTask, patchTask, deleteTask }),
    [tasks, byId, refresh, getDetail, loadDetail, backfill, createTask, patchTask, deleteTask],
  )

  return <TasksStoreContext.Provider value={value}>{children}</TasksStoreContext.Provider>
}

export function useTasksStore(): TasksStore {
  const ctx = useContext(TasksStoreContext)
  if (!ctx) {
    throw new Error('useTasksStore must be used within a <TasksProvider> (mounted once in AppShell)')
  }
  return ctx
}
