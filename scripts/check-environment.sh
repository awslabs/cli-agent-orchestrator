#!/usr/bin/env bash
set -u

check() {
  local command_name="$1"
  if command -v "$command_name" >/dev/null 2>&1; then
    printf '%-12s %-10s %s\n' "$command_name" "OK" "$(command -v "$command_name")"
  else
    printf '%-12s %-10s %s\n' "$command_name" "MISSING" "optional unless noted"
  fi
}

printf 'CAO development environment\n'
printf 'Working directory: %s\n\n' "$(pwd)"
for command_name in git tmux curl gcc make python3 uv node npm pnpm; do
  check "$command_name"
done

printf '\nAgent CLIs\n'
for command_name in claude codex opencode gemini kimi gh copilot cursor agent kiro-cli hermes agy; do
  check "$command_name"
done

printf '\nVersions (when available)\n'
git --version 2>/dev/null || true
tmux -V 2>/dev/null || true
python3 --version 2>/dev/null || true
uv --version 2>/dev/null || true
node --version 2>/dev/null || true
npm --version 2>/dev/null || true
pnpm --version 2>/dev/null || true

if [ -x .venv/bin/python ]; then
  printf '\nVirtual environment: '
  .venv/bin/python --version
else
  printf '\nVirtual environment: missing (.venv)\n'
fi

printf 'Open-file limit: %s\n' "$(ulimit -n)"

