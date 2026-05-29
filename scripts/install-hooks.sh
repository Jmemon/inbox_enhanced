#!/usr/bin/env bash
# -------------------------------------------------------------------
# install-hooks.sh — wire up inbox_enhanced's git hooks.
#
# fuel-code owns core.hooksPath globally, and its global hooks re-dispatch to
# each repo's .git/hooks/<name>. So we install our hook at .git/hooks/pre-commit
# (a symlink to the version-controlled .githooks/pre-commit) and let fuel-code's
# global pre-commit forwarder invoke it.
#
# Requires a fuel-code build that installs a pre-commit forwarder. Older builds
# only forward post-commit/post-checkout/post-merge/pre-push — re-run
# `fuel-code hooks install` after updating fuel-code to pick up pre-commit.
# -------------------------------------------------------------------
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_SRC=".githooks/pre-commit"
HOOK_DST="$REPO_ROOT/.git/hooks/pre-commit"

if [ ! -f "$REPO_ROOT/$HOOK_SRC" ]; then
  echo "error: $HOOK_SRC not found in repo root" >&2
  exit 1
fi

mkdir -p "$REPO_ROOT/.git/hooks"
# Relative symlink so it survives the repo being moved/cloned to a new path.
ln -sf "../../$HOOK_SRC" "$HOOK_DST"
echo "Linked .git/hooks/pre-commit -> $HOOK_SRC"

# Sanity-check the fuel-code dispatch path.
HOOKS_PATH="$(git config core.hooksPath || true)"
if [ -n "$HOOKS_PATH" ]; then
  HOOKS_PATH_EXPANDED="${HOOKS_PATH/#\~/$HOME}"
  if [ ! -e "$HOOKS_PATH_EXPANDED/pre-commit" ]; then
    echo "" >&2
    echo "warning: core.hooksPath = $HOOKS_PATH has no 'pre-commit' forwarder." >&2
    echo "         git will NOT dispatch to .git/hooks/pre-commit until one exists." >&2
    echo "         Update fuel-code and run 'fuel-code hooks install' to add it." >&2
  fi
fi
