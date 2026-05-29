<!-- stamp: 695e2f3 (main) | 2026-05-29 -->

# CI/CD Index

> Scope: Git pre-commit reference-integrity automation (NOT HTTP/Celery): `.githooks/pre-commit` + `cicd/scripts/{reference-check,load-env}.sh`, installed via `scripts/install-hooks.sh` as `.git/hooks/pre-commit` symlink, dispatched through the fuel-code GLOBAL `core.hooksPath` forwarder (`~/.fuel-code/git-hooks/pre-commit`). On `git commit`: diffs staged non-`reference/` files against `reference/MANIFEST.md` File/Scope pairs via OpenRouter (OpenAI-compatible chat/completions, model `anthropic/claude-haiku-4-5`, `OPENROUTER_API_KEY` from gitignored `.env`), warning-only/fails-open, writes `cicd/tasks/update-reference-*.md` task files. (As wired the hook calls the check with `|| true`, so `--strict` does not block a commit out of the box.)

## Files
| Path | Role / key exports | VC? |
|------|--------------------|-----|
| `cicd/scripts/reference-check.sh` | Core check. `set -euo pipefail`; arg parse `--strict`; guards; gathers staged files; awk-parses MANIFEST; builds triage prompt; `curl` OpenRouter; parses JSON array; writes per-doc task files; exit code logic. | yes |
| `cicd/scripts/load-env.sh` | Env loader. **Sourced** (not executed) by reference-check.sh. Extracts ONLY `OPENROUTER_API_KEY` from repo-root `.env` if unset; never `source`s the whole file. | yes |
| `.githooks/pre-commit` | Version-controlled hook entrypoint. `set -uo pipefail`; resolves `REPO_ROOT`; if `cicd/scripts/reference-check.sh` is `-x`, runs it (`|| true` → never blocks here); `exit 0`. Extensible for future steps. | yes |
| `scripts/install-hooks.sh` | Installer. `ln -sf ../../.githooks/pre-commit .git/hooks/pre-commit` (relative symlink). Sanity-checks `core.hooksPath` and warns if its `pre-commit` forwarder is absent. | yes |
| `.git/hooks/pre-commit` | INSTALLED symlink → `../../.githooks/pre-commit`. Created by install-hooks.sh. **Lives in `.git/` → NOT version-controlled.** | NO |
| `~/.fuel-code/git-hooks/pre-commit` | fuel-code GLOBAL forwarder (external repo, see gotchas). Pure dispatcher to repo-local `.git/hooks/pre-commit`; relays its exit code. **External / NOT in this repo.** | NO |
| `reference/MANIFEST.md` | Read input: File\|Scope rows the check triages against. (Owned by reference corpus, not this subsystem.) | yes |
| `cicd/tasks/update-reference-<slug>.md` | WRITE output. Dir + files created on demand; existing files not overwritten. | gitignored output |

## Routes / Tasks / Entrypoints
No HTTP routes, no Celery tasks, no beat entries — this is a git-hook subsystem. Entrypoints:

| Entrypoint | Trigger | Behavior |
|------------|---------|----------|
| `.git/hooks/pre-commit` (symlink → `.githooks/pre-commit`) | `git commit` (via global forwarder dispatch) | Runs reference-check.sh; warning-only. |
| `scripts/install-hooks.sh` | Developer manual invocation (one-time setup) | Creates the symlink; warns if fuel-code forwarder missing. |
| `cicd/scripts/reference-check.sh [--strict]` | Invoked by the hook; also runnable manually | The actual staleness check. |

## Data & state touched
| Direction | Resource | Notes |
|-----------|----------|-------|
| READ | `git diff --cached --name-only` | Staged file list. Filtered: drop `^reference/`; keep `STAGED_NON_REF`. |
| READ | `reference/MANIFEST.md` | awk `-F'|'`: `file=$2`, `scope=$(NF-1)` (robust to stamp's internal `|`). Skips header/separator/`_(`-placeholder rows. |
| READ | `OPENROUTER_API_KEY` (env-var NAME only) | From process env, else extracted from gitignored root `.env` by load-env.sh. |
| READ | `git rev-parse` (`--show-toplevel`, `--short HEAD`, `HEAD`, `--abbrev-ref HEAD`), `date +%Y-%m-%d` | For REPO_ROOT + task-file stamp metadata. |
| WRITE | `cicd/tasks/` dir + `cicd/tasks/update-reference-<slug>.md` | `mkdir -p`; slug = doc minus `.md`, lowercased, `_`→`-`. Idempotent (skips existing). |
| EXTERNAL | OpenRouter chat/completions `https://openrouter.ai/api/v1/chat/completions` | model `anthropic/claude-haiku-4-5`, `max_tokens 256`, `curl -s --max-time 10`, headers `Authorization: Bearer`/`X-Title`. Parses `.choices[0].message.content` → JSON array of stale doc filenames. (cf. ARCHITECTURE.md §5.4) |

## Data flows / cross-subsystem touchpoints
Primary dispatch + triage chain:

```
git commit
  ──> ~/.fuel-code/git-hooks/pre-commit            (fuel-code GLOBAL forwarder; git uses it because
                                                     core.hooksPath is set globally in ~/.gitconfig
                                                     to ~/.fuel-code/git-hooks)
  ──[dispatch]──> <repo>/.git/hooks/pre-commit      (symlink, created by install-hooks.sh)
  ──> .githooks/pre-commit                          (version-controlled entrypoint)
  ──> cicd/scripts/reference-check.sh               (sources load-env.sh for OPENROUTER_API_KEY)
  ──[staged non-reference diff + MANIFEST File|Scope pairs]──> OpenRouter (anthropic/claude-haiku-4-5)
  ──[JSON array of stale doc filenames]──> cicd/tasks/update-reference-*.md
```

Cross-subsystem (operates ON the reference corpus):

```
reference-check.sh ──[reads]──> reference/MANIFEST.md (File|Scope rows)
reference-check.sh ──[writes task]──> cicd/tasks/update-reference-<slug>.md
        ──[task body cites]──> reference/prompts/ADD_REFERENCE.md   (refresh guide)
        ──[task body cites]──> .claude/skills/reference-lookup/SKILL.md (routing + stamp contract)
        ──[fulfilled by an agent following]──> reference/prompts/{CREATE_INDEX,ADD_REFERENCE}.md
                                               ──> updates doc + MANIFEST stamp
```

Doc-stamp contract the generated tasks tell agents to update (top of every reference doc + matching MANIFEST Stamp column):
`<!-- stamp: <short-sha> (<branch>) | <YYYY-MM-DD> -->`

## Decision points & gotchas
- **`core.hooksPath` SHADOWS `.git/hooks/`.** It is set GLOBALLY in `~/.gitconfig` → `~/.fuel-code/git-hooks/` (verified: both `git config core.hooksPath` and `--global` return that path). fuel-code's global hooks re-dispatch back to `.git/hooks/<name>`.
- **Forwarder must EXIST for our hook to fire.** Git only fires hooks present in the global dir. `pre-commit` is the one lifecycle hook fuel-code does NOT natively emit, so a fuel-code `pre-commit` *forwarder* was added (external repo: `packages/hooks/git/pre-commit`, registered in `GIT_HOOK_NAMES` in `packages/cli/src/lib/git-hook-installer.ts`) and copied to `~/.fuel-code/git-hooks/pre-commit`. **A fuel-code reinstall from an OLD build drops it → re-run `fuel-code hooks install`.** `install-hooks.sh` warns when the forwarder is absent.
- **fuel-code recursion guard:** the global forwarder SKIPS the repo-local hook if its first 5 lines contain the literal string `fuel-code:`. So `.githooks/pre-commit` MUST NOT contain that marker (verified: marker count = 0 in its first 5 lines).
- **Warning-only by default:** the check always `exit 0` (commit proceeds). `--strict` makes it `exit 1`; the fuel-code forwarder propagates the repo-local exit code, so `--strict` CAN block a commit. NOTE: `.githooks/pre-commit` invokes the check with `|| true`, so as wired it never passes `--strict` and never blocks — `--strict` only bites if invoked directly or the hook is edited to pass it.
- **Fails open (silent `exit 0`):** no `reference/MANIFEST.md`; no staged files; `OPENROUTER_API_KEY` unset; no MANIFEST entries; curl fails / 10s timeout; unparseable API response.
- **Skips when ONLY `reference/` files are staged** (`STAGED_NON_REF` empty) — you're editing the docs themselves.
- **No self-flagging:** reference docs staged in the same commit (`STAGED_REF_DOCS`) are filtered out of the flagged list via a `grep -vE` pattern.
- **MANIFEST parse robustness:** because the Stamp column embeds a literal `|` (`323bf5a (main) | 2026-05-29`), rows have a variable field count. Parser fixes `File=$2`, `Scope=$(NF-1)` (last content column before the trailing `| `).
- **load-env.sh is sourced, not executed**, and extracts ONLY `OPENROUTER_API_KEY` (grep first matching line, strip quotes) — deliberately does NOT `source` `.env` so other secrets don't leak into the hook subprocess and a malformed unrelated line can't break the hook.
- **Task idempotency:** existing `cicd/tasks/update-reference-<slug>.md` is reported and skipped, not duplicated.
- **Test/build context** (ARCHITECTURE.md §1.7–1.8): this subsystem is a dev-time git hook, distinct from Vite/Bun + uv-sync build stages and pytest/fakeredis test processes; it has no eager-mode/test harness of its own.
