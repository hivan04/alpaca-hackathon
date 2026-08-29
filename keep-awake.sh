#!/usr/bin/env bash
# Keep this Mac awake for a trading session, and put it back afterwards.
#
#   ./keep-awake.sh          # awake until you press Ctrl-C
#
# What it does NOT do: keep the machine awake with the lid closed. No script
# can - closing the lid on a Mac laptop sleeps it unless an external display is
# attached. Lid open, plugged in, or this achieves nothing.
set -euo pipefail

if ! pmset -g ps | grep -q "AC Power"; then
    echo "WARNING: on battery. Sleep settings differ and the agent may stop."
fi

cleanup() {
    echo ""
    echo "Sleep restored. The agent stops when this machine does."
    exit 0
}
trap cleanup INT TERM

echo "Awake until Ctrl-C. Lid OPEN, plugged in."
echo "Judged window: 09:30-16:00 ET = 14:30-21:00 UK."
echo ""
# -d display, -i idle, -m disk, -s system: everything that could stop the loop.
caffeinate -dims
