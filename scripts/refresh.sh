#!/usr/bin/env bash
# Refresh the GitHub stats card and re-render all cards, then push.
# Edit data/ai.json by hand for the AI cards.
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --rebase -q
python3 scripts/build_profile.py --github
git add -A data assets
if git diff --cached --quiet; then
  echo "No changes."
  exit 0
fi
git commit -q -m "Refresh profile cards"
git push -q
echo "Pushed."
