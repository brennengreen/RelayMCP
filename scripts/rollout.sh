#!/bin/bash
# Roll the tested `perf` branch out to production: ~/Projects/RelayMCP (main, the editable install `relaymcp` runs
# from) and the handheld. Only what changed is restarted, and device updates wait for a quiet moment (no tool calls
# on the handheld for 60 s) so an agent session that's using it isn't cut off mid-action.
#
#   scripts/rollout.sh            ff main -> perf (CI must be green), push, update what changed, then doctor
#   scripts/rollout.sh --no-ci    skip the CI check (e.g. docs-only changes)
set -euo pipefail
DEV="$(cd "$(dirname "$0")/.." && pwd)"
PROD="${RELAYMCP_PROD:-$HOME/Projects/RelayMCP}"
RELAY="$HOME/.local/bin/relaymcp"
unset RELAYMCP_DEV RELAYMCP_DEV_ALLOW RELAYMCP_HOME   # production commands use production settings
say() { printf '==> %s\n' "$*"; }

cd "$DEV"
[ -z "$(git status --porcelain)" ] || { echo "the dev tree has uncommitted changes" >&2; exit 1; }
old="$(git -C "$PROD" rev-parse HEAD)"
new="$(git rev-parse perf)"
if [ "$old" = "$new" ]; then say "production is already at $(git log --oneline -1 "$new")"; exit 0; fi
git merge-base --is-ancestor "$old" "$new" || { echo "perf doesn't contain production's commit $old; rebase first" >&2; exit 1; }

if [ "${1:-}" != "--no-ci" ]; then
  say "Checking CI for $(git log --format='%h %s' -1 "$new")"
  for _ in $(seq 1 60); do
    state="$(gh run list --commit "$new" --workflow CI --json status,conclusion --jq '.[0] | (.status + " " + (.conclusion // ""))' 2>/dev/null || true)"
    case "$state" in
      "completed success") break ;;
      completed*) echo "CI for $new: $state" >&2; exit 1 ;;
      "") echo "no CI run for $new (push it first)" >&2; exit 1 ;;
    esac
    sleep 20
  done
  [ "$state" = "completed success" ] || { echo "CI didn't finish in time" >&2; exit 1; }
fi

changed="$(git diff --name-only "$old" "$new")"
say "Updating $PROD: $(git rev-list --count "$old..$new") commit(s)"
git -C "$PROD" merge --ff-only -q perf
git -C "$PROD" push -q origin main

if grep -q '^pyproject.toml$' <<<"$changed"; then
  say "Dependencies or entry points changed: reinstalling the relaymcp command"
  uv tool install -q -e "$PROD" --force
fi
if grep -qE '^src/relaymcp/host/(daemon|tunnel|voice|config|sshconf|services)\.py$' <<<"$changed"; then
  say "Restarting the background service (tunnels reconnect in a few seconds)"
  "$RELAY" service restart
fi

if grep -q '^src/relaymcp/device/' <<<"$changed"; then
  quiet='$u = Join-Path $env:LOCALAPPDATA "RelayMCP"; $t = @("hardware.log", "windows-mcp.out.log") | ForEach-Object { $p = Join-Path $u $_; if (Test-Path $p) { (Get-Item $p).LastWriteTime } } | Sort-Object -Descending | Select-Object -First 1; if ($t) { [int]((Get-Date) - $t).TotalSeconds } else { 9999 }'
  for i in $(seq 1 30); do
    idle="$("$RELAY" exec -- "$quiet" 2>/dev/null | tr -dc '0-9' || true)"
    [ -n "$idle" ] && [ "$idle" -ge 60 ] && break
    [ "$i" = 1 ] && say "Waiting for a quiet moment on the handheld (last tool call ${idle:-?} s ago)"
    sleep 20
  done
  if grep -q 'Relay-Setup.ps1$' <<<"$changed"; then
    say "Updating the handheld (full setup)"
    "$RELAY" deploy --full
  else
    say "Updating the handheld's runtime"
    "$RELAY" deploy
  fi
fi

say "Checking everything"
"$RELAY" doctor || true
say "Rolled out $(git log --oneline -1 "$new") (rollback: git -C $PROD reset --hard $old, then relaymcp deploy)"
