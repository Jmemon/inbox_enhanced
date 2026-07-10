import { Link } from 'react-router-dom'
import type { ActionFeedItem, RuleActionParams, RuleActionType } from '../lib/api'

// Shared presentational pieces for every surface that renders an
// ActionFeedItem (Phase 5, spec 006 §5): the HUD's ReviewTray/ActivityTicker
// (cross-task) and the task page's TaskActionsPanel (single-task, self-owned
// fetch — see that file). Deliberately factored out here rather than
// duplicated per-file (unlike this codebase's usual small-helper-duplication
// convention, e.g. OriginBadge in ReviewFeed/ActivityTicker) because the two
// pieces below carry real interaction logic (approve/reject/undoButtons,
// busy state, inline error surfacing), not just a few lines of markup.

// Human action-description text ("archive thread" / apply label "X" / draft
// a reply) — mirrors RulesSection.tsx's own `actionSummary` wording exactly
// for consistency across the task page, kept as a separate copy since that
// one operates on ActionRule's fields, not a feed item's (same shape,
// different owning type, no shared module between the two areas yet).
export function actionSummaryText(actionType: RuleActionType, actionParams: RuleActionParams): string {
  if (actionType === 'archive_thread') return 'archive thread'
  if (actionType === 'label_thread') return `apply label "${actionParams?.label ?? '?'}"`
  return 'draft a reply'
}

const STATUS_PALETTE: Record<ActionFeedItem['status'], { bg: string; fg: string }> = {
  proposed: { bg: '#eef2f7', fg: '#4b5563' },
  executed: { bg: '#e7f1ea', fg: '#2f6b46' },
  rejected: { bg: '#f1eee7', fg: '#8a7a4b' },
  undone: { bg: '#f1eee7', fg: '#8a7a4b' },
  failed: { bg: '#fde7e9', fg: '#8a1c25' },
}

export function ActionStatusBadge({ status }: { status: ActionFeedItem['status'] }) {
  const { bg, fg } = STATUS_PALETTE[status]
  return (
    <span style={{
      display: 'inline-block', marginLeft: 6, padding: '1px 8px', borderRadius: 999,
      fontSize: 10, fontWeight: 500, background: bg, color: fg,
    }}>
      {status}
    </span>
  )
}

// Pending-action review card: same visual shape as ReviewTray/ReviewFeed's
// event cards (border/radius/padding/fontSize) so action and event cards
// read as one family in a mixed feed. `showTaskLink` is false on the task
// page's own panel (redundant there) and true in the cross-task HUD tray.
// `errorText` is set by the caller when an approve attempt resolved
// 200-with-status:'failed' (e.g. missing Gmail scopes) — per spec the card
// is NOT removed on that outcome (it's no longer 'proposed' so a plain
// refetch would just make it vanish); the caller keeps rendering it from a
// local override and this component swaps the buttons for the error line,
// since a failed action can never be re-approved (no retry UI in v1 — see
// specs/006_actions/design.md §7).
export function ActionCard({
  item, busy, errorText, onApprove, onReject, showTaskLink,
}: {
  item: ActionFeedItem
  busy: boolean
  errorText: string | null
  onApprove: () => void
  onReject: () => void
  showTaskLink: boolean
}) {
  return (
    <li style={{ border: '1px solid #eee', borderRadius: 6, padding: 8, fontSize: 13 }}>
      {showTaskLink && (
        <Link to={`/tasks/${item.task_id}`} style={{ fontSize: 11, color: '#4b5563', display: 'block' }}>
          {item.task_name}
        </Link>
      )}
      <div style={{ fontWeight: 600 }}>
        {actionSummaryText(item.action_type, item.action_params)}
        {item.thread_subject && <span style={{ fontWeight: 400, color: '#666' }}> — {item.thread_subject}</span>}
      </div>
      <div style={{ color: '#666' }}>{item.rule_summary}</div>
      {errorText ? (
        <div style={{ color: '#8a1c25', fontSize: 12, marginTop: 4 }}>{errorText}</div>
      ) : (
        <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
          <button disabled={busy} onClick={onApprove} style={{ fontSize: 12 }}>approve</button>
          <button disabled={busy} onClick={onReject} style={{ fontSize: 12 }}>reject</button>
        </div>
      )}
    </li>
  )
}

// Settled-action activity line: "{task_name}: {action summary} · {ago}" plus
// a status badge, with an `[undo]` button only when the CALLER passes
// `onUndo` — callers gate on `status === 'executed' && action_type in
// (archive_thread, label_thread)`, the same reversibility rule the server
// enforces (`_UNDOABLE_ACTION_TYPES` in api/actions.py), so a stale client
// can offer at worst a transient 409 (surfaced via `undoErrorText`), never a
// permanently-wrong button. undone/failed/rejected items render via the
// status badge alone (no button, no special-cased text — the badge already
// says it).
export function ActionActivityLine({
  item, agoText, showTaskLink, busy, undoErrorText, onUndo,
}: {
  item: ActionFeedItem
  agoText: string
  showTaskLink: boolean
  busy: boolean
  undoErrorText: string | null
  onUndo: (() => void) | null
}) {
  return (
    <li style={{ fontSize: 12, color: '#444', display: 'grid', gap: 2 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {showTaskLink ? (
            <Link to={`/tasks/${item.task_id}`} style={{ color: 'inherit', textDecoration: 'none' }}>
              {item.task_name}:
            </Link>
          ) : null}
          {' '}
          {actionSummaryText(item.action_type, item.action_params)}
          {item.thread_subject && ` — ${item.thread_subject}`}
          {' · '}{agoText}
          <ActionStatusBadge status={item.status} />
        </span>
        {onUndo && (
          <button onClick={onUndo} disabled={busy} style={{ fontSize: 11, flexShrink: 0 }}>undo</button>
        )}
      </div>
      {undoErrorText && <div style={{ color: '#8a1c25', fontSize: 11 }}>{undoErrorText}</div>}
    </li>
  )
}
