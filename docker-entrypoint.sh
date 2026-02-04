#!/bin/sh
set -e

mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix

export DISPLAY=${DISPLAY:-:99}
Xvfb "$DISPLAY" -screen 0 1366x768x24 -ac +extension GLX +render -noreset &

exec gosu appuser "$@"
