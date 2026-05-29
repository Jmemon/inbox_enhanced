#!/usr/bin/env bash
# -------------------------------------------------------------------
# load-env.sh — Shared env loader for inbox_enhanced CI/CD scripts
#
# Makes OPENROUTER_API_KEY available to the pre-commit scripts. Order:
#   1. If OPENROUTER_API_KEY is already in the environment, keep it.
#   2. Otherwise, extract *only* that one key from the repo-root .env
#      (gitignored) — we deliberately do NOT `source` the whole file,
#      so no other secrets leak into the hook's subprocess and a
#      malformed unrelated line can't break the hook.
#
# Source this file; don't execute it directly.
# -------------------------------------------------------------------

REPO_ROOT="$(git rev-parse --show-toplevel)"
ENV_FILE="$REPO_ROOT/.env"

if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -f "$ENV_FILE" ]; then
  # Grab the first OPENROUTER_API_KEY= line, take everything after the '='
  _line="$(grep -E '^[[:space:]]*OPENROUTER_API_KEY=' "$ENV_FILE" | head -1 || true)"
  if [ -n "$_line" ]; then
    _val="${_line#*=}"
    # Strip optional surrounding single/double quotes
    _val="${_val%\"}"; _val="${_val#\"}"
    _val="${_val%\'}"; _val="${_val#\'}"
    export OPENROUTER_API_KEY="$_val"
  fi
  unset _line _val
fi
