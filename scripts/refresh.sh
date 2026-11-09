#!/usr/bin/env bash
# Refresh every card (GitHub + local AI usage logs) and push.
# Run from my machine: ./scripts/refresh.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --rebase -q
python3 scripts/build_profile.py --github --ai
git add data assets README.md
if git diff --cached --quiet; then
  echo "No changes."
  exit 0
fi
git commit -q -m "Refresh profile cards"
git push -q
echo "Pushed."
