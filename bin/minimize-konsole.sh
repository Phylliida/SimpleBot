#!/usr/bin/env bash
# Minimize any KWin-managed window owned by the given PID.
# Wayland-friendly (uses KWin's D-Bus scripting interface).
#
# Usage: minimize-konsole.sh <pid>
set -u
PID="${1:?usage: minimize-konsole.sh <pid>}"

# qdbus must be available; if not, silently give up.
command -v qdbus >/dev/null 2>&1 || exit 0

# Small delay so konsole has a chance to create its window. The windowAdded
# hook below also catches windows that appear after the script loads.
sleep 0.2

SCRIPT=$(mktemp --suffix=.js) || exit 0
trap 'rm -f "$SCRIPT"' EXIT

cat >"$SCRIPT" <<JS
(function () {
  var targetPid = $PID;
  function tryMinimize(w) {
    try {
      if (w && w.pid === targetPid && !w.minimized) {
        w.minimized = true;
      }
    } catch (e) {}
  }
  var ws = (typeof workspace.windows !== 'undefined')
    ? workspace.windows
    : (workspace.clientList ? workspace.clientList() : []);
  for (var i = 0; i < ws.length; i++) tryMinimize(ws[i]);
  if (workspace.windowAdded) workspace.windowAdded.connect(tryMinimize);
  else if (workspace.clientAdded) workspace.clientAdded.connect(tryMinimize);
})();
JS

PLUGIN="simplebot-minimize-$PID-$$"
qdbus org.kde.KWin /Scripting loadScript "$SCRIPT" "$PLUGIN" >/dev/null 2>&1 || exit 0
qdbus org.kde.KWin /Scripting start >/dev/null 2>&1 || true

# Keep the script loaded long enough for windowAdded to fire.
sleep 5
qdbus org.kde.KWin /Scripting unloadScript "$PLUGIN" >/dev/null 2>&1 || true
