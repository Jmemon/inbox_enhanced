import { useCallback, useEffect, useState } from 'react'
import { useAuth } from '../../auth/useAuth'
import {
  createRule, deleteRule, listRules, patchRule,
  type ActionRule, type RuleActionType, type RuleMode, type RuleTrigger, type TaskStateSchema,
} from '../../lib/api'

// Rules ARE the grants (spec 006 §2) — a tracker's own action rules, owned
// entirely by this component: fetch on mount, refetch after each of its own
// mutations. Deliberately NOT threaded through TasksProvider (unlike
// entities/events/threads on TaskDetail itself) — the rules list has no
// other consumer on this page, same "simplest self-contained data
// ownership" posture as HudPage's ReviewTray/ActivityTicker.
function triggerSummary(rule: ActionRule): string {
  if (rule.trigger === 'entity_entered_stage') {
    return `when entity enters '${rule.trigger_params?.stage ?? '?'}'`
  }
  return 'when a thread is linked'
}

function actionSummary(rule: ActionRule): string {
  if (rule.action_type === 'archive_thread') return 'archive thread'
  if (rule.action_type === 'label_thread') return `apply label "${rule.action_params?.label ?? '?'}"`
  return 'draft a reply'
}

const modeChipStyle = (mode: RuleMode) => ({
  display: 'inline-block', padding: '1px 8px', borderRadius: 999, fontSize: 11, fontWeight: 500,
  background: mode === 'auto' ? '#e7f1ea' : '#eef2f7',
  color: mode === 'auto' ? '#2f6b46' : '#4b5563',
})

const needsPermissionBadgeStyle = {
  display: 'inline-block', padding: '1px 8px', borderRadius: 999, fontSize: 11, fontWeight: 500,
  background: '#fff2e0', color: '#a06a00',
} as const

// One inline add/edit form, never a modal — matches TaskDetail's existing
// section idioms (ThreadsPanel's search-to-attach box, EntityDrawer's
// per-field rows), all rendered inline on the page rather than as an
// overlay.
type FormState = {
  trigger: RuleTrigger
  stage: string
  actionType: RuleActionType
  label: string
  instructions: string
  mode: RuleMode
}

function emptyForm(firstStage: string): FormState {
  return { trigger: 'entity_entered_stage', stage: firstStage, actionType: 'archive_thread', label: '', instructions: '', mode: 'propose' }
}

function formFromRule(rule: ActionRule): FormState {
  return {
    trigger: rule.trigger,
    stage: rule.trigger === 'entity_entered_stage' ? (rule.trigger_params?.stage ?? '') : '',
    actionType: rule.action_type,
    label: rule.action_type === 'label_thread' ? (rule.action_params?.label ?? '') : '',
    instructions: rule.action_type === 'draft_reply' ? (rule.action_params?.instructions ?? '') : '',
    mode: rule.mode,
  }
}

// Mirrors the server's own request-body shape (app/api/actions.py's
// _RuleCreateBody/_validate_rule_fields) — the client-side mode lock for
// draft_reply below is belt-and-suspenders on top of the server's 422.
function buildBody(form: FormState) {
  return {
    trigger: form.trigger,
    trigger_params: form.trigger === 'entity_entered_stage' ? { stage: form.stage } : null,
    action_type: form.actionType,
    action_params:
      form.actionType === 'label_thread' ? { label: form.label }
        : form.actionType === 'draft_reply' ? { instructions: form.instructions }
        : null,
    mode: form.actionType === 'draft_reply' ? ('propose' as const) : form.mode,
  }
}

type Mode = { kind: 'idle' } | { kind: 'add' } | { kind: 'edit'; ruleId: string }

export function RulesSection({ taskId, schema }: { taskId: string; schema: TaskStateSchema | null }) {
  const { state } = useAuth()
  // This page only renders once authed (see App.tsx's Gate), so state.status
  // is always 'authed' here in practice; the `false` fallback is a harmless
  // defensive default, never expected to actually apply.
  const hasWriteScopes = state.status === 'authed' ? state.user.has_write_scopes : false

  // all_stages() equivalent (server: task_engine/schema.py) — non-terminal
  // stages followed by terminal ones, same list PipelineBoard's "move to"
  // select already offers, so a rule can target a terminal stage directly.
  const allStages = schema ? [...schema.pipeline.stages, ...schema.pipeline.terminal] : []

  const [rules, setRules] = useState<ActionRule[]>([])
  const [loading, setLoading] = useState(true)
  const [mode, setMode] = useState<Mode>({ kind: 'idle' })
  const [form, setForm] = useState<FormState | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const refetch = useCallback(async () => {
    try {
      const { rules } = await listRules(taskId)
      setRules(rules)
    } catch (e) {
      console.error('[RulesSection] refetch failed', e)
    } finally {
      setLoading(false)
    }
  }, [taskId])

  useEffect(() => { void refetch() }, [refetch])

  const startAdd = () => {
    setMode({ kind: 'add' })
    setForm(emptyForm(allStages[0] ?? ''))
    setFormError(null)
  }

  const startEdit = (rule: ActionRule) => {
    setMode({ kind: 'edit', ruleId: rule.id })
    setForm(formFromRule(rule))
    setFormError(null)
  }

  const cancelForm = () => {
    setMode({ kind: 'idle' })
    setForm(null)
    setFormError(null)
  }

  const handleSubmit = async () => {
    if (!form) return
    setSubmitting(true)
    setFormError(null)
    try {
      const body = buildBody(form)
      if (mode.kind === 'edit') {
        await patchRule(taskId, mode.ruleId, body)
      } else {
        await createRule(taskId, body)
      }
      setMode({ kind: 'idle' })
      setForm(null)
      await refetch()
    } catch (e: any) {
      setFormError(e?.message ?? 'failed to save rule')
    } finally {
      setSubmitting(false)
    }
  }

  // Immediate delete with a plain window.confirm — mirrors TaskDetail's own
  // handleDelete/handleMergeEntity confirm idiom on this same page (simpler
  // than replicating ViewBucketsModal's single-confirming-id state machine
  // for what's already a flat, non-modal list here).
  const handleDelete = async (rule: ActionRule) => {
    if (!window.confirm(`Delete this rule (${triggerSummary(rule)} → ${actionSummary(rule)})? This cannot be undone.`)) return
    try {
      await deleteRule(taskId, rule.id)
      await refetch()
    } catch (e) {
      console.error('[RulesSection] delete failed', e)
    }
  }

  return (
    <section style={{ border: '1px solid #eee', borderRadius: 8, padding: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <h2 style={{ fontSize: 14, margin: 0 }}>Rules</h2>
        {mode.kind === 'idle' && (
          <button onClick={startAdd} style={{ fontSize: 12, marginLeft: 'auto' }}>+ add rule</button>
        )}
      </div>

      {loading && <div style={{ color: '#888', fontSize: 13, marginTop: 8 }}>loading…</div>}

      {!loading && rules.length === 0 && (
        <div style={{ color: '#888', fontSize: 13, marginTop: 8 }}>no rules yet — actions only ever fire from a rule you add here</div>
      )}

      {!loading && rules.length > 0 && (
        <ul style={{ listStyle: 'none', margin: '8px 0 0', padding: 0, display: 'grid', gap: 6 }}>
          {rules.map((rule) => (
            <li
              key={rule.id}
              style={{
                border: '1px solid #eee', borderRadius: 6, padding: 8, fontSize: 13,
                display: 'flex', alignItems: 'center', gap: 8,
              }}
            >
              <div style={{ flex: 1 }}>
                <span>{triggerSummary(rule)} → {actionSummary(rule)}</span>
                <span style={{ marginLeft: 8, ...modeChipStyle(rule.mode) }}>{rule.mode}</span>
                {!hasWriteScopes && (
                  <span
                    style={{ marginLeft: 8, ...needsPermissionBadgeStyle }}
                    title="Reconnect Gmail (see the banner above) to let this rule run"
                  >
                    needs permission
                  </span>
                )}
              </div>
              <button onClick={() => startEdit(rule)} style={{ fontSize: 12 }} disabled={mode.kind !== 'idle'}>
                edit
              </button>
              <button
                onClick={() => handleDelete(rule)}
                style={{ fontSize: 12, color: '#8a1c25' }}
                disabled={mode.kind !== 'idle'}
              >
                delete
              </button>
            </li>
          ))}
        </ul>
      )}

      {form && (
        <div style={{ border: '1px solid #ddd', borderRadius: 6, padding: 12, marginTop: 8, display: 'grid', gap: 8 }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <label style={{ fontSize: 12, color: '#666' }}>when</label>
            <select
              value={form.trigger}
              onChange={(e) => setForm({ ...form, trigger: e.target.value as RuleTrigger })}
              style={{ fontSize: 12 }}
            >
              <option value="entity_entered_stage">entity enters stage</option>
              <option value="thread_linked">thread is linked</option>
            </select>
            {form.trigger === 'entity_entered_stage' && (
              allStages.length === 0 ? (
                <span style={{ fontSize: 12, color: '#a06a00' }}>this task has no stages defined</span>
              ) : (
                <select
                  value={form.stage}
                  onChange={(e) => setForm({ ...form, stage: e.target.value })}
                  style={{ fontSize: 12 }}
                >
                  {allStages.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              )
            )}
          </div>

          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <label style={{ fontSize: 12, color: '#666' }}>then</label>
            <select
              value={form.actionType}
              onChange={(e) => {
                const actionType = e.target.value as RuleActionType
                // draft_reply is server-capped at propose (spec §6 invariant
                // 2) — lock the toggle the moment this action is picked so
                // the form can never even attempt to submit auto+draft_reply.
                setForm({ ...form, actionType, mode: actionType === 'draft_reply' ? 'propose' : form.mode })
              }}
              style={{ fontSize: 12 }}
            >
              <option value="archive_thread">archive thread</option>
              <option value="label_thread">apply label</option>
              <option value="draft_reply">draft a reply</option>
            </select>
          </div>

          {form.actionType === 'label_thread' && (
            <input
              placeholder="label name"
              value={form.label}
              onChange={(e) => setForm({ ...form, label: e.target.value })}
              style={{ fontSize: 12, padding: 4 }}
            />
          )}

          {form.actionType === 'draft_reply' && (
            <textarea
              placeholder="instructions for the draft"
              value={form.instructions}
              onChange={(e) => setForm({ ...form, instructions: e.target.value })}
              rows={3}
              style={{ fontSize: 12, padding: 4, fontFamily: 'inherit' }}
            />
          )}

          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <label style={{ fontSize: 12, color: '#666' }}>mode</label>
            <button
              type="button"
              disabled={form.actionType === 'draft_reply'}
              onClick={() => setForm({ ...form, mode: form.mode === 'auto' ? 'propose' : 'auto' })}
              style={{ fontSize: 12 }}
            >
              {form.mode}
            </button>
            {form.actionType === 'draft_reply' && (
              <span style={{ fontSize: 11, color: '#888' }}>drafts always need your approval</span>
            )}
          </div>

          {formError && <div style={{ color: '#8a1c25', fontSize: 12 }}>{formError}</div>}

          <div style={{ display: 'flex', gap: 8 }}>
            <button
              onClick={handleSubmit}
              disabled={submitting || (form.trigger === 'entity_entered_stage' && !form.stage)}
              style={{ fontSize: 12 }}
            >
              {mode.kind === 'edit' ? 'save' : 'add rule'}
            </button>
            <button onClick={cancelForm} disabled={submitting} style={{ fontSize: 12 }}>cancel</button>
          </div>
        </div>
      )}
    </section>
  )
}
