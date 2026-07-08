import { useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import type { PreviewExample } from '../../lib/sse'
import {
  type Bucket, type BucketExampleIn,
} from '../../lib/api'

type Choice = 'positive' | 'near_miss' | 'rejected'
type ExampleState = PreviewExample & { initial: Exclude<Choice, 'rejected'>; choice: Choice }

const HINT = "We recommend confirming at least 2 positives + 2 near-misses before saving — but it's not required."

const PREVIEW_UNAVAILABLE =
  'Example preview is unavailable in this build — bucket creation is moving to the task wizard.'

// onSave is owned by Home's useBuckets instance so its bucket list refreshes
// after creation. Calling useBuckets() here would create a separate state
// instance that nobody renders from, leaving the toolbar/filter dropdown
// stale until a page reload.
//
// Phase 4 Task 4: the draft-preview backend this modal relied on (POST/GET
// /api/buckets/draft/preview + the bucket_draft_preview SSE event) was
// deleted in Task 3. This whole modal is dead code walking — Task 5 replaces
// bucket creation with the task wizard — so startPreview/moreExamples below
// are stubbed to an inline error instead of restoring the deleted backend.
export function NewBucketModal({ onClose, onSave }: {
  onClose: () => void
  onSave: (body: {
    name: string; description: string
    confirmed_positives: BucketExampleIn[]; confirmed_negatives: BucketExampleIn[]
  }) => Promise<Bucket>
}) {
  // Never transitions past 'form' now that startPreview/moreExamples are
  // stubbed below — 'pending'/'review' JSX is unreachable dead code, kept
  // only because this whole modal is replaced in Task 5.
  const [step] = useState<'form' | 'pending' | 'review'>('form')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [examples, setExamples] = useState<ExampleState[]>([])

  async function startPreview() {
    setError(PREVIEW_UNAVAILABLE)
  }

  async function moreExamples() {
    setError(PREVIEW_UNAVAILABLE)
  }

  function setChoice(threadId: string, choice: Choice) {
    setExamples(prev => prev.map(ex => ex.thread_id === threadId ? { ...ex, choice } : ex))
  }

  async function save() {
    const positives = examples.filter(e => e.choice === 'positive').map(toExampleIn)
    const negatives = examples.filter(e => e.choice === 'near_miss').map(toExampleIn)
    await onSave({ name, description, confirmed_positives: positives, confirmed_negatives: negatives })
    onClose()
  }

  return (
    <Backdrop onClose={onClose}>
      <div style={modalStyle}>
        <h3 style={{ margin: 0 }}>new bucket</h3>
        {step === 'form' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16, marginTop: 16 }}>
            <label style={fieldLabelStyle}>
              <span>name</span>
              <input
                style={inputStyle}
                value={name}
                onChange={e => setName(e.target.value)}
              />
            </label>
            <label style={fieldLabelStyle}>
              <span>what kind of email goes in this bucket?</span>
              <textarea
                style={textareaStyle}
                rows={4}
                value={description}
                onChange={e => setDescription(e.target.value)}
              />
            </label>
            {error && <div style={{ color: '#8a1c25', fontSize: 13 }}>{error}</div>}
            <div style={{ display: 'flex', gap: 8 }}>
              <button disabled={!name || !description} onClick={startPreview}>find examples</button>
              <button onClick={onClose}>cancel</button>
            </div>
          </div>
        )}
        {step === 'pending' && <div style={{ marginTop: 16 }}>Scanning your inbox, one minute ...</div>}
        {step === 'review' && (
          <div style={{ marginTop: 12 }}>
            <div style={{ fontSize: 12, color: '#666', marginBottom: 12 }}>{HINT}</div>
            {examples.map(ex => (
              <ExampleRow key={ex.thread_id} ex={ex} onChoice={c => setChoice(ex.thread_id, c)} />
            ))}
            <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
              <button onClick={save}>save</button>
              <button onClick={moreExamples}>more examples</button>
              <button onClick={onClose}>cancel</button>
            </div>
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
