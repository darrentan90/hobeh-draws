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
if ! git pull --rebase --autostash origin main; then
  # latest.json is generated, so a conflict means another run published the
  # same file first. Hand-merging JSON is never the answer: drop this commit
  # and take main's. Whatever main lacks is still "due" when the watcher next
  # reads the file, so it is scraped and published again on the next pass.
  # Leaving the rebase half-done was the old behaviour, and it left conflict
  # markers in latest.json for the watcher to crash on.
  git rebase --abort || true
  git reset --hard origin/main
  echo "another run published first — took origin/main, nothing lost"
  exit 0
fi
git push
echo "published: $SUMMARY"
