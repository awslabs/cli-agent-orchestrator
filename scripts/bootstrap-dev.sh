#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

missing=()
for command_name in git tmux curl gcc make uv node npm; do
  command -v "$command_name" >/dev/null 2>&1 || missing+=("$command_name")
done

if ((${#missing[@]})); then
  printf 'Missing required commands: %s\n' "${missing[*]}"
  printf 'Install them with your trusted system/package manager, then rerun.\n'
  printf 'This script never invokes sudo or installs system packages silently.\n'
  exit 1
fi

python_312="$(command -v python3.12 || true)"
if [ -z "$python_312" ]; then
  printf 'Python 3.12 is missing; installing a user-scoped uv-managed runtime.\n'
  uv python install 3.12
  python_312="$(uv python find 3.12)"
fi

if [ -x .venv/bin/python ]; then
  current_version="$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if [ "$current_version" != "3.12" ]; then
    printf '.venv uses Python %s; refusing to delete it.\n' "$current_version"
    printf 'Move it to a backup name, then rerun this script.\n'
    exit 1
  fi
else
  uv venv --python "$python_312"
fi

uv sync --all-extras
(
  cd web
  npm ci --include=dev
)

printf '\nBootstrap complete. Next steps:\n'
printf '  uv run python --version\n'
printf '  uv run cao --help\n'
printf '  uv run cao-server\n'
printf '  (cd web && npm run dev)\n'
printf 'Optional providers may remain absent; run scripts/check-environment.sh.\n'
