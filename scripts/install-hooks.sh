#!/usr/bin/env bash
#
# Idempotently install this repo's git hooks into `.git/hooks/`.
#
# `.git/hooks/` is not version-controlled, so every fresh clone must run this
# script once to enable the checks. Re-running is safe: existing symlinks that
# already point at the right target are left alone; any other pre-existing hook
# file is backed up before being replaced.
#
# Currently installs:
#   pre-commit -> scripts/hooks/pre-commit
#     Runs scripts/check-code-english.py to enforce English-only comments and
#     log strings in source code (see CONTRIBUTING.md).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ ! -d .git ]; then
  echo "[install-hooks] .git/ not found — run this from a git working tree." >&2
  exit 1
fi

install_hook() {
  local hook_name="$1"
  local target_relpath="$2"
  local hook_path=".git/hooks/${hook_name}"
  local target_path="${REPO_ROOT}/${target_relpath}"

  if [ ! -f "$target_path" ]; then
    echo "[install-hooks] missing hook script: ${target_relpath}" >&2
    return 1
  fi
  chmod +x "$target_path"

  # If a correct symlink already exists, do nothing.
  if [ -L "$hook_path" ]; then
    local current
    current="$(readlink "$hook_path")"
    if [ "$current" = "$target_path" ] || [ "$current" = "../../${target_relpath}" ]; then
      echo "[install-hooks] ${hook_name}: already linked"
      return 0
    fi
  fi

  # Back up any pre-existing hook that is not ours.
  if [ -e "$hook_path" ] && [ ! -L "$hook_path" ]; then
    local backup="${hook_path}.pre-alephain.$(date +%s)"
    mv "$hook_path" "$backup"
    echo "[install-hooks] ${hook_name}: existing file backed up to ${backup}"
  elif [ -L "$hook_path" ]; then
    rm "$hook_path"
  fi

  ln -s "$target_path" "$hook_path"
  echo "[install-hooks] ${hook_name}: linked to ${target_relpath}"
}

install_hook "pre-commit" "scripts/hooks/pre-commit"

echo "[install-hooks] done."
