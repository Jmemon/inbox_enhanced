import { useEffect, useRef, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import type { PreviewExample } from '../../lib/sse'
import type { TaskStateSchema } from '../../lib/api'
import { useTasksStore } from '../../state/TasksProvider'
import { SchemaEditor } from './SchemaEditor'

// Step machine: goal form -> draft pending -> review (name/description/
// schema/examples, seeded from the draft) -> create -> 'creating'
// (post-create backfill wait).
//
// Phase 4.5 Task 5: the goal->draft backend this wizard used to drive
// (POST/GET /api/tasks/draft + the `task_draft_ready` SSE event) is deleted
// — jobs own that flow now (`api.ts`'s createJob/getJobs + confirmJob,
// state/JobsProvider.tsx). `startDraft` below is stubbed to an inline error
// instead of restoring the deleted request — same pattern Phase 4 Task 4
// used on the now-deleted NewBucketModal (see git history) when ITS backend
// was cut out from under it a task early. The 'pending'/'review' steps past
// it are therefore unreachable dead code, kept only because Phase 4.5 Task 6
// restructures this wizard onto startCreation()/confirmJob and a review step
// seeded from a job's `payload` instead (specs/005_jobs_surface/design.md
// §2.4) rather than reintroducing the deleted draft-poll path.
//
// Phase 4 Task 5: `kind` gives this wizard a second mode ('bucket', default
// 'tracker') so bucket creation goes through the same goal->draft->review
// flow instead of a separate modal. Bucket mode diverges only where the two
// domains actually differ: no EPS state_schema (the review step skips
// <SchemaEditor> and always sends `state_schema: null`), and no per-task
// detail page to hand off to on completion (`onCreated` instead of
// `navigate`). Every other line here is unchanged, byte-identical tracker
// behavior — search for `kind ===` to see every divergence point.

type Choice = 'positive' | 'near_miss' | 'rejected'
type ExampleState = PreviewExample & { initial: Exclude<Choice, 'rejected'>; choice: Choice }
type Step = 'form' | 'pending' | 'review' | 'creating'

const HINT = "We recommend confirming at least 2 positives + 2 near-misses before creating the task — but it's not required."

export function NewTaskWizard({ onClose, kind = 'tracker', onCreated }: {
  onClose: () => void
  // 'bucket' skips the EPS schema step entirely — buckets carry criteria only,
  // no pipeline/entity state (see task_engine/repo.create_task).
  kind?: 'tracker' | 'bucket'
  // Bucket mode's completion hook — there's no per-task detail page for a
  // bucket to navigate to, so the caller (InboxPage) refreshes its own bucket
  // list instead. Unused in tracker mode.
  onCreated?: (taskId: string) => void
}) {
  const navigate = useNavigate()
  const { createTask, backfill } = useTasksStore()

  const [step, setStep] = useState<Step>('form')
  const [goal, setGoal] = useState('')

  // Seeded from the draft's proposal once it lands; editable from there.
  // Nothing sets these anymore now that startDraft() is stubbed below — see
  // this file's top-of-file note — but the review step (Task 6 rewires its
  // entry point) still reads/edits them, so they stay.
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [stateSchema, setStateSchema] = useState<TaskStateSchema | null>(null)
  // No setter destructured — nothing seeds this anymore now that applyDraft
  // is gone (see top-of-file note); submit() below still reads it.
  const [keywordProbes] = useState<string[]>([])
  const [examples, setExamples] = useState<ExampleState[]>([])

  const [submitting, setSubmitting] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [taskId, setTaskId] = useState<string | null>(null)

  // Rendered inline on the form step. Was startDraft's POST /api/tasks/draft
  // failure banner; now just carries the stubbed "unavailable" message (see
  // startDraft below) since that request no longer exists to fail.
  const [startError, setStartError] = useState<string | null>(null)

  // Guards the done-navigate effect below so it fires at most once — without
  // this, a stray backfill progress update after the first navigate (e.g. a
  // late SSE frame re-rendering `backfill`) would re-run navigate()/onClose().
  const navigatedRef = useRef(false)

  // Once the task is created, watch its backfill progress for completion and
  // hand off — to the task's own page in tracker mode, or back to the caller
  // in bucket mode (there is no per-bucket detail page to navigate to).
  // Keyed on this task's own progress entry (not the whole `backfill` record)
  // so unrelated tasks' progress updates don't re-run this effect, and
  // guarded by navigatedRef so it fires at most once even if `progress`
  // changes again after done=true.
  //
  // This relies on TasksProvider's task_backfill_progress SSE handler
  // recording `backfill[taskId]` unconditionally by task_id, with no check
  // against its (tracker-only) task list — confirmed by reading
  // state/TasksProvider.tsx: `setBackfill(prev => ({ ...prev, [e.task_id]:
  // ... }))` never consults `byId`/`known`. So a bucket task id — never
  // present in that list — still gets its progress recorded here, and this
  // effect's `backfill[taskId]` read works unchanged for bucket mode. (The
  // handler's `if (e.done) void loadDetail(e.task_id)` follow-up also
  // resolves fine for a bucket id — GET /api/tasks/{id} has no kind filter —
  // but is a no-op for the tracker list since `setTasks`'s `.map` only
  // transforms existing entries, never inserts new ones.)
  const progress = taskId ? backfill[taskId] : undefined
  useEffect(() => {
    if (step !== 'creating' || !taskId || navigatedRef.current) return
    if (progress?.done) {
      navigatedRef.current = true
      if (kind === 'bucket') {
        onCreated?.(taskId)
      } else {
        navigate(`/tasks/${taskId}`)
      }
      onClose()
    }
  }, [step, taskId, progress?.done, navigate, onClose, kind, onCreated])

  // Stubbed — see this file's top-of-file note. Never advances past 'form';
  // Task 6 rewires this onto startCreation()/the jobs flow.
  function startDraft() {
    setStartError('Task creation is moving to the jobs panel — coming in the next update.')
  }

  function setChoice(threadId: string, choice: Choice) {
    setExamples(prev => prev.map(ex => (ex.thread_id === threadId ? { ...ex, choice } : ex)))
  }

  async function submit() {
    if (submitting) return
    // Tracker mode requires a schema (SchemaEditor keeps it non-null past the
    // review step); bucket mode never has one.
    if (kind === 'tracker' && !stateSchema) return
    setSubmitting(true)
    setCreateError(null)
    try {
      const positives = examples.filter(e => e.choice === 'positive').map(toExampleIn)
      const negatives = examples.filter(e => e.choice === 'near_miss').map(toExampleIn)
      const task = await createTask({
        name, goal: goal.trim(), description, kind,
        state_schema: stateSchema ? trimStateSchema(stateSchema) : null,
        keyword_probes: keywordProbes, confirmed_positives: positives, confirmed_negatives: negatives,
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
        <h3 style={{ margin: 0 }}>{kind === 'bucket' ? 'New bucket' : 'new task'}</h3>

        {step === 'form' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16, marginTop: 16 }}>
            {startError && <div style={errorBannerStyle}>{startError}</div>}
            <label style={fieldLabelStyle}>
              <span>{kind === 'bucket' ? 'What should this bucket catch?' : 'what do you want to track?'}</span>
              <textarea
                style={textareaStyle}
                rows={4}
                placeholder={kind === 'bucket' ? undefined
                  : "I'm job hunting — track every company I'm in process with…"}
                value={goal}
                onChange={e => setGoal(e.target.value)}
              />
            </label>
            <div style={{ display: 'flex', gap: 8 }}>
              <button disabled={!goal.trim()} onClick={startDraft}>start</button>
              <button onClick={onClose}>cancel</button>
            </div>
          </div>
        )}

        {/* Unreachable now that startDraft() (above) never leaves 'form' —
            kept only because Task 6 restructures this step onto the jobs
            flow rather than deleting it outright. */}
        {step === 'pending' && (
          <div style={{ marginTop: 16 }}>Reading your goal and scanning your inbox…</div>
        )}

        {/* Tracker mode's gate: stateSchema is only non-null once something
            seeds it (Task 6's job-review wiring), so this also holds the
            step on 'review' until that's happened. Bucket mode never sets
            stateSchema, so it gates on the step alone. Unreachable for now
            like the 'pending' step above — nothing currently sets step to
            'review' either. */}
        {step === 'review' && (kind === 'bucket' || stateSchema) && (
          <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 20 }}>
            {createError && <div style={errorBannerStyle}>{createError}</div>}
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

            {/* Bucket mode skips the EPS schema step entirely — a bucket has
                no pipeline/entity state, only criteria. */}
            {kind === 'tracker' && stateSchema && <SchemaEditor value={stateSchema} onChange={setStateSchema} />}

            <div>
              <div style={{ fontSize: 12, color: '#666', marginBottom: 12 }}>{HINT}</div>
              {examples.map(ex => (
                <ExampleRow key={ex.thread_id} ex={ex} onChoice={c => setChoice(ex.thread_id, c)} />
              ))}
            </div>

            <div style={{ display: 'flex', gap: 8 }}>
              <button disabled={submitting} onClick={() => void submit()}>
                {kind === 'bucket' ? 'create bucket' : 'create task'}
              </button>
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

// Trims freeform text the SchemaEditor collects before it hits the wire:
// pipeline stage/terminal chip names and entity noun/identity_hint/attribute
// keys. Chips and attributes that are empty after trimming are dropped
// rather than sent as blank strings — the server's EPS schema validator
// (task_engine/schema.py) rejects those outright, so this avoids a
// round-trip 422 for whitespace the user never meant to submit.
function trimStateSchema(schema: TaskStateSchema): TaskStateSchema {
  const stages = schema.pipeline.stages.map(s => s.trim()).filter(Boolean)
  const terminal = schema.pipeline.terminal.map(s => s.trim()).filter(Boolean)
  const entity = schema.entity
    ? {
        noun: schema.entity.noun.trim(),
        identity_hint: schema.entity.identity_hint.trim(),
        attributes: schema.entity.attributes
          .map(a => ({ ...a, key: a.key.trim() }))
          .filter(a => a.key !== ''),
      }
    : null
  return { ...schema, pipeline: { stages, terminal }, entity }
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

// Inline error banner — was inline-only on the review step (createError);
// pulled into a shared constant so the form step's startError renders
// identically rather than drifting.
const errorBannerStyle: CSSProperties = {
  color: '#8a1c25', fontSize: 13, background: '#fdf0f1', padding: 8, borderRadius: 4,
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
