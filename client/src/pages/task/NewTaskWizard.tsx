import { useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import type { PreviewExample } from '../../lib/sse'
import type { Job, TaskStateSchema } from '../../lib/api'
import { useJobsStore } from '../../state/JobsProvider'
import { SchemaEditor } from './SchemaEditor'

// Phase 4.5 Task 6 (specs/005_jobs_surface/design.md §2.4): the wizard is
// now exactly two user-initiated moments, never anything that opens or
// persists on its own —
//
//   1. start: the goal form -> startCreation(goal, kind) -> onClose()
//      immediately. There is no in-modal wait for the draft; the header
//      chip starts spinning and the panel (JobsPanel) carries all progress
//      from here.
//   2. review: opened DIRECTLY at the review step by passing `reviewJob` —
//      a draft_ready job the user picked via JobsPanel's [Review] action
//      (wired in AppShell). Seeded from `reviewJob.payload`. Confirm ->
//      confirmJob(reviewJob.id, body) -> onClose() immediately; backfill
//      progress and the eventual "open task"/"bucket live" affordance live
//      entirely in the panel now — this modal never watches backfill and
//      never navigates.
//
// This retires the old goal->draft->review->creating step machine
// (`startDraft`'s inline-stub, the draftId/pending wait, the 'creating' step
// + navigatedRef backfill-watch effect) that Task 5 already cut the backend
// out from under — see git history for that intermediate stub.
//
// `kind`/`reviewJob` together decide the wizard's effective domain:
// `effectiveKind` is `reviewJob.task_kind` when reviewing (the job already
// carries its own kind — trusting the prop over it would let a caller typo
// desync the two), else the plain `kind` prop for start mode. Bucket mode
// diverges from tracker mode only where the two domains actually differ: no
// EPS state_schema (skip <SchemaEditor>, always send `state_schema: null`)
// and no per-task detail page (no navigate — panel owns that for trackers
// too now). Every other line is shared — search `effectiveKind ===` for
// every divergence point.

type Choice = 'positive' | 'near_miss' | 'rejected'
type ExampleState = PreviewExample & { initial: Exclude<Choice, 'rejected'>; choice: Choice }
type Step = 'form' | 'review'

const HINT = "We recommend confirming at least 2 positives + 2 near-misses before creating the task — but it's not required."

function toExampleState(ex: PreviewExample, initial: Exclude<Choice, 'rejected'>): ExampleState {
  return { ...ex, initial, choice: initial }
}

export function NewTaskWizard({ onClose, kind = 'tracker', reviewJob }: {
  onClose: () => void
  // Start-mode domain. Ignored once `reviewJob` is set — see effectiveKind.
  kind?: 'tracker' | 'bucket'
  // When set, the wizard skips the goal form and opens directly at the
  // review step, seeded from this job's payload (design.md §2.4). Only ever
  // passed for a draft_ready 'creation' job (JobsPanel's [Review] action, so
  // `payload` is expected non-null; guarded below anyway with a graceful
  // inline error rather than a crash, since a job's payload is nullable in
  // its type and dismiss/backend races are cheap to imagine).
  reviewJob?: Job
}) {
  const { startCreation, confirmJob } = useJobsStore()

  // reviewJob is only ever set for the lifetime of this mount (AppShell
  // unmounts the wizard by clearing reviewJob rather than swapping it for a
  // different job while open — see AppShell.tsx), so it's safe to read once
  // here for every seeded field below rather than re-deriving per-render.
  const payload = reviewJob?.payload ?? null
  const proposal = payload?.proposal
  const effectiveKind: 'tracker' | 'bucket' = reviewJob ? (reviewJob.task_kind ?? 'tracker') : kind

  const [step] = useState<Step>(reviewJob ? 'review' : 'form')
  const [goal, setGoal] = useState('')

  // Seeded from reviewJob.payload.proposal when opened in review mode;
  // otherwise blank (start mode never touches these — they're filled in by
  // whoever reviews the resulting draft_ready job later, in a fresh mount).
  const [name, setName] = useState(() => proposal?.name ?? '')
  const [description, setDescription] = useState(() => proposal?.description ?? '')
  const [stateSchema, setStateSchema] = useState<TaskStateSchema | null>(() => proposal?.state_schema ?? null)
  // No setter used in the UI (no editing affordance exists for these today,
  // same as before Task 6) — submitReview below still reads it.
  const [keywordProbes] = useState<string[]>(() => proposal?.keyword_probes ?? [])
  const [examples, setExamples] = useState<ExampleState[]>(() => [
    ...(payload?.positives ?? []).map(ex => toExampleState(ex, 'positive')),
    ...(payload?.near_misses ?? []).map(ex => toExampleState(ex, 'near_miss')),
  ])

  const [submitting, setSubmitting] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  // Rendered inline on the form step — startCreation's real failure surface
  // now (was a stubbed "unavailable" message pre-Task-6).
  const [startError, setStartError] = useState<string | null>(null)

  function setChoice(threadId: string, choice: Choice) {
    setExamples(prev => prev.map(ex => (ex.thread_id === threadId ? { ...ex, choice } : ex)))
  }

  async function startNew() {
    if (submitting || !goal.trim()) return
    setSubmitting(true)
    setStartError(null)
    try {
      await startCreation(goal.trim(), kind)
      onClose()
    } catch (e) {
      // Stay on the form, editable, with the failure inline — no retry
      // machinery beyond letting the user hit "start" again.
      setStartError(e instanceof Error ? e.message : String(e))
      setSubmitting(false)
    }
  }

  async function submitReview() {
    if (submitting || !reviewJob) return
    // Tracker mode requires a schema (SchemaEditor keeps it non-null past
    // this point); bucket mode never has one.
    if (effectiveKind === 'tracker' && !stateSchema) return
    setSubmitting(true)
    setCreateError(null)
    try {
      const positives = examples.filter(e => e.choice === 'positive').map(toExampleIn)
      const negatives = examples.filter(e => e.choice === 'near_miss').map(toExampleIn)
      await confirmJob(reviewJob.id, {
        name, description,
        state_schema: stateSchema ? trimStateSchema(stateSchema) : null,
        keyword_probes: keywordProbes, confirmed_positives: positives, confirmed_negatives: negatives,
      })
      onClose()
    } catch (e) {
      // 409 (job no longer draft_ready — dismissed or already confirmed
      // elsewhere) or 422 (schema invalid after edits) — confirmJob's
      // throwWithDetail (api.ts) surfaces the server's detail string here.
      // Stay on the review step, editable, same as the old submit() did.
      setCreateError(e instanceof Error ? e.message : String(e))
      setSubmitting(false)
    }
  }

  return (
    <Backdrop onClose={onClose}>
      <div style={modalStyle}>
        <h3 style={{ margin: 0 }}>
          {reviewJob
            ? (effectiveKind === 'bucket' ? 'Review bucket' : 'review task')
            : (kind === 'bucket' ? 'New bucket' : 'new task')}
        </h3>

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
              <button disabled={!goal.trim() || submitting} onClick={() => void startNew()}>start</button>
              <button onClick={onClose}>cancel</button>
            </div>
          </div>
        )}

        {step === 'review' && reviewJob && (
          payload === null ? (
            <div style={{ marginTop: 16 }}>
              <div style={errorBannerStyle}>This job has no draft to review yet.</div>
              <div style={{ marginTop: 8 }}><button onClick={onClose}>close</button></div>
            </div>
          ) : (effectiveKind === 'bucket' || stateSchema) && (
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

              {/* Bucket mode skips the EPS schema step entirely — a bucket
                  has no pipeline/entity state, only criteria. */}
              {effectiveKind === 'tracker' && stateSchema && <SchemaEditor value={stateSchema} onChange={setStateSchema} />}

              <div>
                <div style={{ fontSize: 12, color: '#666', marginBottom: 12 }}>{HINT}</div>
                {examples.map(ex => (
                  <ExampleRow key={ex.thread_id} ex={ex} onChoice={c => setChoice(ex.thread_id, c)} />
                ))}
              </div>

              <div style={{ display: 'flex', gap: 8 }}>
                <button disabled={submitting} onClick={() => void submitReview()}>
                  {effectiveKind === 'bucket' ? 'create bucket' : 'create task'}
                </button>
                <button onClick={onClose}>cancel</button>
              </div>
            </div>
          )
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

// Inline error banner — shared by the form step's startError and the review
// step's createError so both render identically rather than drifting.
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
