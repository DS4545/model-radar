#!/usr/bin/env bash
# run.sh — one daily cycle of Model Radar on the VM.
# Fetch → diff → render → commit → push. Publishing is the point; a run that
# builds the page and never pushes it is the failure this project exists to stop.
#
# The VM is the single writer of this repo. Edits made elsewhere are copied in
# and committed from here, so the history never forks.

set -uo pipefail

ROOT=/srv/model-radar
SITE=$ROOT/site          # git repo root — everything in here is published
LOG=$ROOT/run.log
LINK=$(cat "$ROOT/link.txt" 2>/dev/null || echo "https://example.invalid/")
export GIT_SSH_COMMAND="ssh -i $ROOT/deploy_key -o StrictHostKeyChecking=accept-new"

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { echo "$(ts) $*" >> "$LOG"; }

cd "$SITE" || { log "FATAL no site dir"; exit 1; }

OUT=$(timeout 600 python3 "$SITE/radar.py" --out "$SITE" --link "$LINK" 2>&1)
RC=$?
log "radar rc=$RC :: $(echo "$OUT" | tail -1)"
# rc=2 is a deliberate refusal on a partial fetch — not an error worth pushing.
[ "$RC" -ne 0 ] && exit "$RC"

if [ ! -d "$SITE/.git" ]; then
  log "no git repo yet — built locally, nothing pushed"
  exit 0
fi

git add -A
if git diff --cached --quiet; then
  log "no file changes, nothing to commit"
  exit 0
fi

COUNT=$(python3 -c "
import json
try:
    print(len(json.load(open('$SITE/data/events-$(date +%F).json'))))
except Exception:
    print(0)
" 2>/dev/null)

git -c user.name="Hex (Model Radar)" -c user.email="hex@qh.foundation" \
    commit -q -m "radar: $(date +%F) — $COUNT event(s)" || { log "commit failed"; exit 1; }

# Someone may have committed from the browser; rebase rather than fight it.
git -c user.name="Hex (Model Radar)" -c user.email="hex@qh.foundation" \
    pull -q --rebase origin main 2>>"$LOG" || log "pull --rebase failed, pushing anyway"

if git push -q origin HEAD:main 2>>"$LOG"; then
  log "pushed $COUNT event(s)"
else
  log "PUSH FAILED — built but not published"
  exit 1
fi
