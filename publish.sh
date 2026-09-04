#!/usr/bin/env bash
# Commit and push latest.json if it changed. Called by build_latest.py --watch
# after every write, and once more by the workflow at the end of the job.
#
# `build_latest.py` leaves the file untouched when nothing changed, so an empty
# diff is the normal outcome and must not fail or commit.
set -euo pipefail
cd "$(dirname "$0")"

if git diff --quiet -- latest.json; then
  echo "no change to publish"
  exit 0
fi

SUMMARY=$(python summarise.py)
git config user.name  "hobeh-draws bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add latest.json
git commit -m "$SUMMARY"
# Rebase rather than force: a concurrent run may have landed first, and its
# draws are as real as this run's.
git pull --rebase --autostash origin main
git push
echo "published: $SUMMARY"
