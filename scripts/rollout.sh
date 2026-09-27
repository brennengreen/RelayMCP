#!/bin/bash
# Roll the tested `perf` branch out to production: ~/Projects/RelayMCP (main, the editable install `relaymcp` runs
# from) and the handheld. Only what changed is restarted, and device updates wait for a quiet moment (no tool calls
# on the handheld for 60 s) so an agent session that's using it isn't cut off mid-action.
#
#   scripts/rollout.sh            ff main -> perf (CI must be green), push, update what changed, then doctor
#   scripts/rollout.sh --no-ci    skip the CI check (e.g. docs-only changes)
#   QUIET_WAIT_MIN=20             how long to wait for a quiet moment before giving up (production stays unchanged)
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
restart_service=false; update_device=false
grep -qE '^src/relaymcp/host/(daemon|tunnel|voice|config|sshconf|services)\.py$' <<<"$changed" && restart_service=true
grep -q '^src/relaymcp/device/' <<<"$changed" && update_device=true

# Restarting the service drops the tunnels and a device update restarts the handheld's servers, so either waits for a
# quiet moment first. Nothing in production changes until then, so giving up leaves everything as it was.
if $restart_service || $update_device; then
  quiet='$u = Join-Path $env:LOCALAPPDATA "RelayMCP"; $t = @("hardware.log", "windows-mcp.out.log") | ForEach-Object { $p = Join-Path $u $_; if (Test-Path $p) { (Get-Item $p).LastWriteTime } } | Sort-Object -Descending | Select-Object -First 1; if ($t) { [int]((Get-Date) - $t).TotalSeconds } else { 9999 }'
  tries=$(( ${QUIET_WAIT_MIN:-20} * 3 ))
  for i in $(seq 1 "$tries"); do
    idle="$("$RELAY" exec -- "$quiet" 2>/dev/null | tr -dc '0-9' || true)"
    [ -n "$idle" ] && [ "$idle" -ge 60 ] && break
    [ "$i" = 1 ] && say "Waiting for a quiet moment on the handheld (last tool call ${idle:-?} s ago)"
    if [ "$i" = "$tries" ]; then
      echo "no quiet moment in ${QUIET_WAIT_MIN:-20} min (last tool call ${idle:-?} s ago); production is unchanged, try again later" >&2
      exit 1
    fi
    sleep 20
  done
fi

say "Updating $PROD: $(git rev-list --count "$old..$new") commit(s)"
git -C "$PROD" merge --ff-only -q perf
git -C "$PROD" push -q origin main

if grep -q '^pyproject.toml$' <<<"$changed"; then
  say "Dependencies or entry points changed: reinstalling the relaymcp command"
  uv tool install -q -e "$PROD[voice]" --force   # the voice extra: a warm Copilot runtime for voice prompts
fi
if $restart_service; then
  say "Restarting the background service (tunnels reconnect in a few seconds)"
  "$RELAY" service restart
fi
if $update_device; then
  if grep -q 'Relay-Setup.ps1$' <<<"$changed"; then
    say "Updating the handheld (full setup)"
    "$RELAY" deploy --full
  else
    say "Updating the handheld's runtime"
    "$RELAY" deploy
  fi
fi

if grep -q "written by relaymcp" "$HOME/.copilot/agents/handheld.agent.md" 2>/dev/null; then
  "$RELAY" agent install >/dev/null && say "Refreshed the handheld custom agent"
fi

say "Checking everything"
"$RELAY" doctor || true
say "Rolled out $(git log --oneline -1 "$new") (rollback: git -C $PROD reset --hard $old, then relaymcp deploy)"
