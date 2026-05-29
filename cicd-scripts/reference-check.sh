#!/usr/bin/env bash
# -------------------------------------------------------------------
# reference-check.sh — LLM-powered staleness check for reference docs
#
# Runs from the pre-commit hook. Compares the staged (non-reference)
# changes against reference/MANIFEST.md and flags any reference doc
# whose documented scope the changes might have invalidated. For each
# flagged doc it writes a task file under tasks/ so an agent or
# developer can refresh + re-stamp the doc later.
#
# This is the inbox_enhanced port of the aiod-agents check, adapted to:
#   - this repo's MANIFEST format ( | File | Stamp | Scope | , where the
#     Stamp column itself contains a literal '|' between sha and date )
#   - the stamp format <!-- stamp: <short-sha> (<branch>) | <date> -->
#   - reference/prompts/ADD_REFERENCE.md as the update guide (there is
#     no separate UPDATE_REFERENCE prompt)
#   - a Python (FastAPI/Celery) + TypeScript (React/Vite) codebase
#
# Behavior:
#   - Warning-only by default (prints which docs may need updating)
#   - Pass --strict to exit non-zero when stale docs are detected
#   - Fails open: silently passes on missing key / API error / timeout
#   - Uses claude-haiku-4-5 for speed and cost
# -------------------------------------------------------------------

set -euo pipefail

STRICT=false
for arg in "$@"; do
  case "$arg" in
    --strict) STRICT=true ;;
  esac
done

REPO_ROOT="$(git rev-parse --show-toplevel)"
MANIFEST="$REPO_ROOT/reference/MANIFEST.md"

# --- Load env (provides ANTHROPIC_API_KEY from root .env if not already set) ---
# shellcheck source=load-env.sh
source "$(dirname "$0")/load-env.sh"

# --- Guards ---
if [ ! -f "$MANIFEST" ]; then
  echo "  Reference docs check: skipped — no reference/MANIFEST.md found."
  exit 0
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "  Reference docs check: skipped — ANTHROPIC_API_KEY is not set."
  exit 0
fi

# --- Gather staged files ---
STAGED_FILES=$(git diff --cached --name-only 2>/dev/null || true)
if [ -z "$STAGED_FILES" ]; then
  echo "  Reference docs check: skipped — no staged files."
  exit 0
fi

# --- Skip if only reference/ files are staged (we're editing the docs themselves) ---
STAGED_NON_REF=$(echo "$STAGED_FILES" | grep -v '^reference/' || true)
if [ -z "$STAGED_NON_REF" ]; then
  echo "  Reference docs check: skipped — only reference/ files staged."
  exit 0
fi

# Reference docs being edited in this same commit — excluded from results so a
# doc isn't flagged as "stale" on the strength of its own staged changes.
STAGED_REF_DOCS=$(echo "$STAGED_FILES" | grep '^reference/' | sed 's|^reference/||' || true)

# --- Extract "File|Scope" pairs from the manifest table ---
# The manifest stamp column embeds a literal '|' (e.g. "13a07e5 (main) | 2026-05-29"),
# so a row has a variable number of '|'-delimited fields. We rely on a stable shape:
# the File is the first content column ($2) and the Scope is the last content column
# ($(NF-1), since each row ends with a trailing '| '). This is robust to the stamp's
# internal pipe. Header ("File"), separator ("---"), and placeholder ("_(...") rows
# are skipped.
MANIFEST_ENTRIES=$(awk -F'|' '
  /^[[:space:]]*\|/ {
    file=$2; scope=$(NF-1);
    gsub(/^[ \t]+|[ \t]+$/, "", file);
    gsub(/^[ \t]+|[ \t]+$/, "", scope);
    if (file=="" || file=="File" || file ~ /^[-]+$/ || file ~ /^_\(/) next;
    print file "|" scope;
  }' "$MANIFEST")

if [ -z "$MANIFEST_ENTRIES" ]; then
  echo "  Reference docs check: skipped — no entries found in MANIFEST.md."
  exit 0
fi

# --- Build the triage prompt ---
PROMPT="You are a documentation staleness detector for inbox_enhanced, a Gmail inbox app:
a Python backend (FastAPI API + Celery workers, SQLAlchemy/Postgres, Redis) and a
TypeScript React/Vite client. Reference docs are dense per-subsystem indexes.

Below are two inputs:
1. STAGED FILES — files being committed in this change
2. REFERENCE DOCS — each line is 'filename|scope description'

For each reference doc, decide whether the staged files MIGHT affect the content its
scope describes (file paths, routes, tasks, data flows, types it indexes). Be inclusive:
a plausible overlap counts. Return ONLY a JSON array of the doc filenames that may need
updating. Return [] if none. No prose, no markdown fencing — just the JSON array.

STAGED FILES:
$STAGED_NON_REF

REFERENCE DOCS:
$MANIFEST_ENTRIES"

# --- Call the Anthropic API (10s timeout — never block commits on a slow API) ---
RESPONSE=$(curl -s --max-time 10 \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d "$(jq -n \
    --arg prompt "$PROMPT" \
    '{
      model: "claude-haiku-4-5-20251001",
      max_tokens: 256,
      messages: [{ role: "user", content: $prompt }]
    }')" \
  https://api.anthropic.com/v1/messages 2>/dev/null) || {
  echo "  Reference docs check: skipped — API call failed."
  exit 0
}

RAW_TEXT=$(echo "$RESPONSE" | jq -r '.content[0].text // empty' 2>/dev/null) || {
  echo "  Reference docs check: skipped — could not parse API response."
  exit 0
}

if [ -z "$RAW_TEXT" ]; then
  echo "  Reference docs check: all docs up-to-date."
  exit 0
fi

# Strip any stray markdown fencing and isolate the bare JSON array
STALE_DOCS=$(echo "$RAW_TEXT" | sed 's/^```[a-z]*//; s/```$//' | tr -d '\n' | sed 's/^[^[]*\[/[/; s/\][^]]*$/]/')

if [ -z "$STALE_DOCS" ] || [ "$STALE_DOCS" = "[]" ]; then
  echo "  Reference docs check: all docs up-to-date."
  exit 0
fi

DOC_COUNT=$(echo "$STALE_DOCS" | jq 'length' 2>/dev/null) || {
  echo "  Reference docs check: skipped — could not parse stale list (raw: $RAW_TEXT)."
  exit 0
}
if [ "$DOC_COUNT" -eq 0 ]; then
  echo "  Reference docs check: all docs up-to-date."
  exit 0
fi

# --- Drop any flagged doc that is itself being edited in this commit ---
if [ -n "$STAGED_REF_DOCS" ]; then
  FILTER_PATTERN=$(echo "$STAGED_REF_DOCS" | paste -sd'|' -)
  STALE_LIST=$(echo "$STALE_DOCS" | jq -r '.[]' 2>/dev/null | grep -vE "^($FILTER_PATTERN)$" || true)
else
  STALE_LIST=$(echo "$STALE_DOCS" | jq -r '.[]' 2>/dev/null)
fi

if [ -z "$STALE_LIST" ]; then
  echo "  Reference docs check: all docs up-to-date."
  exit 0
fi

echo ""
echo "⚠  Reference docs may be stale:"
echo "$STALE_LIST" | while read -r doc; do
  echo "   → reference/$doc"
done

# --- Create one task file per flagged doc ---
TASKS_DIR="$REPO_ROOT/tasks"
mkdir -p "$TASKS_DIR"
SHORT_SHA=$(git rev-parse --short HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "HEAD")
CURRENT_SHA=$(git rev-parse HEAD)
TODAY=$(date +%Y-%m-%d)
TASK_COUNT=0

while read -r doc; do
  SLUG=$(echo "$doc" | sed 's/\.md$//' | tr '[:upper:]' '[:lower:]' | tr '_' '-')
  TASK_FILE="$TASKS_DIR/update-reference-${SLUG}.md"

  if [ -f "$TASK_FILE" ]; then
    echo "   ↳ task already exists: tasks/update-reference-${SLUG}.md"
    continue
  fi

  cat > "$TASK_FILE" <<EOF
# Update reference doc: ${doc}

## Reference Files

- \`reference/${doc}\` — the reference doc flagged as potentially stale
- \`reference/MANIFEST.md\` — manifest with the commit-stamp row to update
- \`reference/prompts/ADD_REFERENCE.md\` — how to extend/refresh an existing index
- \`.claude/skills/reference-lookup/SKILL.md\` — how docs are routed + the stamp contract

## Base Commit

\`${CURRENT_SHA}\` (${BRANCH}) — commit that triggered the staleness detection.

## Description

The pre-commit reference check judged that \`reference/${doc}\` may be stale after
changes to:
$(echo "$STAGED_FILES" | sed 's/^/- `/' | sed 's/$/`/')

Refresh the doc against the current code (follow \`reference/prompts/ADD_REFERENCE.md\`),
then re-stamp it. The doc's top-of-file stamp and its MANIFEST.md row both use:
\`<short-sha> (<branch>) | <date>\`. Per the repo convention, commit any code the doc
cites first, then stamp.

## Success Criteria

- [ ] \`reference/${doc}\` reflects the current state of the code it documents
- [ ] Top-of-file stamp updated to \`<new-sha> (${BRANCH}) | <date>\` (currently suggest \`${SHORT_SHA} (${BRANCH}) | ${TODAY}\`)
- [ ] Matching row in \`reference/MANIFEST.md\` updated to the same stamp
EOF

  TASK_COUNT=$((TASK_COUNT + 1))
  echo "   ↳ created task: tasks/update-reference-${SLUG}.md"
done <<< "$STALE_LIST"

if [ "$TASK_COUNT" -gt 0 ]; then
  echo ""
  echo "  📋 ${TASK_COUNT} task(s) created in tasks/. Run them to refresh stale docs."
fi
echo ""

if [ "$STRICT" = true ]; then
  exit 1
fi

exit 0
