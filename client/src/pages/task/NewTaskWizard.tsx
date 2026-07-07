import { useEffect, useRef, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { subscribeSse, type PreviewExample } from '../../lib/sse'
import { postTaskDraft, getTaskDraft, type TaskStateSchema, type TaskDraftProposal } from '../../lib/api'
import { useTasksStore } from '../../state/TasksProvider'
import { SchemaEditor } from './SchemaEditor'

// Cloned from NewBucketModal.tsx's step machine (read that file first) — same
// appliedRef per-draft idempotency, same SSE-fast-path + 5s-poll-fallback
// dance, same Backdrop/modal styles (copied below rather than extracted to a
// shared module: the duplication is small and keeps this task's diff to the
// two new files the plan named, instead of also touching NewBucketModal.tsx).
//
// Differences from the bucket flow:
// - One extra step ('creating') for the post-create backfill wait.
// - The SSE payload (`task_draft_ready`) carries only the draft_id, not the
//   proposal — unlike `bucket_draft_preview`, which inlines positives/near_misses
//   directly. The fast path here does event -> getTaskDraft(id) -> apply.
// - No "more examples" affordance (the plan doesn't call for one here).

type Choice = 'positive' | 'near_miss' | 'rejected'
type ExampleState = PreviewExample & { initial: Exclude<Choice, 'rejected'>; choice: Choice }
type Step = 'form' | 'pending' | 'review' | 'creating'

const HINT = "We recommend confirming at least 2 positives + 2 near-misses before creating the task — but it's not required."

export function NewTaskWizard({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate()
  const { createTask, backfill } = useTasksStore()

  const [step, setStep] = useState<Step>('form')
  const [goal, setGoal] = useState('')
  const [draftId, setDraftId] = useState<string | null>(null)

  // Seeded from the draft's proposal once it lands; editable from there.
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [stateSchema, setStateSchema] = useState<TaskStateSchema | null>(null)
  const [keywordProbes, setKeywordProbes] = useState<string[]>([])
  const [examples, setExamples] = useState<ExampleState[]>([])

  const [submitting, setSubmitting] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [taskId, setTaskId] = useState<string | null>(null)

  // Idempotent apply: a result for a given draft_id can arrive via SSE OR via
  // the polling fallback (or both, in either order). appliedRef ensures we
  // only push the proposal + examples and flip step once per draft_id —
  // identical rationale to NewBucketModal's appliedRef.
  const appliedRef = useRef<Set<string>>(new Set())

  // Guards the fast-path's async getTaskDraft() fetch (kicked off from inside
  // an SSE handler, which has no cancellation closure of its own the way the
  // poll effect below does) from applying state after unmount.
  const unmountedRef = useRef(false)
  useEffect(() => () => { unmountedRef.current = true }, [])

  function applyDraft(forDraftId: string, proposal: TaskDraftProposal,
                       positives: PreviewExample[], nearMisses: PreviewExample[]) {
    if (appliedRef.current.has(forDraftId)) return
    appliedRef.current.add(forDraftId)
    setName(proposal.name)
    setDescription(proposal.description)
    setStateSchema(proposal.state_schema)
    setKeywordProbes(proposal.keyword_probes)
    setExamples([
      ...positives.map(ex => ({ ...ex, initial: 'positive' as const, choice: 'positive' as const })),
      ...nearMisses.map(ex => ({ ...ex, initial: 'near_miss' as const, choice: 'near_miss' as const })),
    ])
    setStep('review')
  }

  // Fast path: SSE push when the worker publishes. Unlike bucket_draft_preview,
  // task_draft_ready carries only the draft_id, so this still needs a fetch
  // before there's anything to apply.
  useEffect(() => {
    return subscribeSse((e) => {
      if (e.event !== 'task_draft_ready' || e.draft_id !== draftId) return
      if (appliedRef.current.has(e.draft_id)) return
      const forId = e.draft_id
      getTaskDraft(forId)
        .then(r => {
          if (unmountedRef.current || appliedRef.current.has(forId)) return
          if (r.status === 'ready') applyDraft(forId, r.proposal, r.positives, r.near_misses)
          // pending/gone here: leave it to the poll fallback below.
        })
        .catch(err => console.error('[task draft] fast-path fetch failed', err))
    })
  }, [draftId])

  // Safety net: poll the cache every 5s while pending. Same cancellation
  // semantics as NewBucketModal's preview poll — a `cancelled` flag closed
  // over per-effect-run plus clearTimeout on cleanup, checked at both async
  // boundaries. Stops on success, on draftId change, on step change, or on
  // unmount.
  useEffect(() => {
    if (step !== 'pending' || !draftId) return
    const localId = draftId
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null

    async function tick() {
      if (cancelled || appliedRef.current.has(localId)) return
      try {
        const r = await getTaskDraft(localId)
        if (cancelled) return
        if (r.status === 'ready') {
          applyDraft(localId, r.proposal, r.positives, r.near_misses)
          return
        }
        if (r.status === 'gone') {
          console.warn('[task draft] cache expired before result arrived')
          return  // give up; user can close and start a new draft
        }
      } catch (e) {
        console.error('[task draft] poll failed', e)
      }
      timer = setTimeout(tick, 5000)
    }

    // Start at 5s — let SSE win on the happy path, and only burn HTTP
    // requests if SSE doesn't deliver.
    timer = setTimeout(tick, 5000)

    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [draftId, step])

  // Once the task is created, watch its backfill progress for completion and
  // hand off to the task's own page.
  useEffect(() => {
    if (step !== 'creating' || !taskId) return
    if (backfill[taskId]?.done) {
      navigate(`/tasks/${taskId}`)
      onClose()
    }
  }, [step, taskId, backfill, navigate, onClose])

  async function startDraft() {
    const { draft_id } = await postTaskDraft(goal)
    setDraftId(draft_id)
    setStep('pending')
  }

  function setChoice(threadId: string, choice: Choice) {
    setExamples(prev => prev.map(ex => (ex.thread_id === threadId ? { ...ex, choice } : ex)))
  }

  async function submit() {
    if (!stateSchema || submitting) return
    setSubmitting(true)
    setCreateError(null)
    try {
      const positives = examples.filter(e => e.choice === 'positive').map(toExampleIn)
      const negatives = examples.filter(e => e.choice === 'near_miss').map(toExampleIn)
      const task = await createTask({
        name, goal, description, state_schema: stateSchema, keyword_probes: keywordProbes,
        confirmed_positives: positives, confirmed_negatives: negatives,
      })
      setTaskId(task.id)
      setStep('creating')
    } catch (e) {
      // 422 (schema invalid after edits) or any other create failure — the
      // store's createTask rethrows apiCreateTask's Error as-is (no
      // structured status code). Render its message inline and stay on the
      // review step so the form is still editable.
      setCreateError(e instanceof Error ? e.message : String(e))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Backdrop onClose={onClose}>
      <div style={modalStyle}>
        <h3 style={{ margin: 0 }}>new task</h3>

        {step === 'form' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16, marginTop: 16 }}>
            <label style={fieldLabelStyle}>
              <span>what do you want to track?</span>
              <textarea
                style={textareaStyle}
                rows={4}
                placeholder="I'm job hunting — track every company I'm in process with…"
                value={goal}
                onChange={e => setGoal(e.target.value)}
              />
            </label>
            <div style={{ display: 'flex', gap: 8 }}>
              <button disabled={!goal.trim()} onClick={() => void startDraft()}>start</button>
              <button onClick={onClose}>cancel</button>
            </div>
          </div>
        )}

        {step === 'pending' && (
          <div style={{ marginTop: 16 }}>Reading your goal and scanning your inbox…</div>
        )}

        {step === 'review' && stateSchema && (
          <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 20 }}>
            {createError && (
              <div style={{ color: '#8a1c25', fontSize: 13, background: '#fdf0f1', padding: 8, borderRadius: 4 }}>
                {createError}
              </div>
            )}
            <label style={fieldLabelStyle}>
              <span>name</span>
              <input style={inputStyle} value={name} onChange={e => setName(e.target.value)} />
            </label>
            <label style={fieldLabelStyle}>
              <span>description</span>
              <textarea
                style={textareaStyle} rows={3} value={description}
                onChange={e => setDescription(e.target.value)}
              />
            </label>

            <SchemaEditor value={stateSchema} onChange={setStateSchema} />

            <div>
              <div style={{ fontSize: 12, color: '#666', marginBottom: 12 }}>{HINT}</div>
              {examples.map(ex => (
                <ExampleRow key={ex.thread_id} ex={ex} onChoice={c => setChoice(ex.thread_id, c)} />
              ))}
            </div>

            <div style={{ display: 'flex', gap: 8 }}>
              <button disabled={submitting} onClick={() => void submit()}>create task</button>
              <button onClick={onClose}>cancel</button>
            </div>
          </div>
        )}

        {step === 'creating' && (
          <div style={{ marginTop: 16 }}>
            {taskId && backfill[taskId]
              ? `scanned ${backfill[taskId].scanned} · matched ${backfill[taskId].matched}`
              : 'starting backfill…'}
          </div>
        )}
      </div>
    </Backdrop>
  )
}


function ExampleRow({ ex, onChoice }: { ex: ExampleState; onChoice: (c: Choice) => void }) {
  return (
    <div style={{ borderBottom: '1px solid #eee', padding: '12px 0' }}>
      <div style={{ fontSize: 13 }}>
        <strong>{ex.subject}</strong> from <span style={{ color: '#666' }}>{ex.sender}</span>
      </div>
      <blockquote style={{ borderLeft: '3px solid #ccc', margin: '8px 0', padding: '0 8px',
                            color: '#444', fontSize: 13 }}>
        {ex.snippet}
      </blockquote>
      <div style={{ fontSize: 12, color: '#666', fontStyle: 'italic' }}>why: {ex.rationale}</div>
      <div style={{ marginTop: 8, display: 'flex', gap: 12, fontSize: 13 }}>
        {(['positive', 'near_miss', 'rejected'] as Choice[]).map(c => (
          <label key={c} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <input type="radio" name={ex.thread_id} checked={ex.choice === c}
                   onChange={() => onChoice(c)} />
            {c === 'near_miss' ? 'near-miss' : c}
            {ex.initial === c && <span style={{ color: '#999' }}> (suggested)</span>}
          </label>
        ))}
      </div>
    </div>
  )
}


function toExampleIn(ex: ExampleState) {
  return { sender: ex.sender, subject: ex.subject, snippet: ex.snippet, rationale: ex.rationale }
}


const modalStyle: CSSProperties = {
  background: '#fff', padding: 24, borderRadius: 8, maxWidth: 720, width: '90%',
  maxHeight: '80vh', overflowY: 'auto',
}

// Form field label: label text sits above its input on its own line so inputs
// don't get squeezed inline next to the prompt.
const fieldLabelStyle: CSSProperties = {
  display: 'flex', flexDirection: 'column', gap: 6, fontSize: 14,
}

const inputStyle: CSSProperties = {
  width: '100%', boxSizing: 'border-box', padding: '6px 8px', fontSize: 14,
  borderRadius: 4, border: '1px solid #ccc',
}

const textareaStyle: CSSProperties = {
  ...inputStyle, fontFamily: 'inherit', resize: 'vertical',
}

function Backdrop({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
    }}>
      <div onClick={e => e.stopPropagation()}>{children}</div>
    </div>
  )
}
