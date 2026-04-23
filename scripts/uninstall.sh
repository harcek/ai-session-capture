#!/bin/sh
# Remove the claude-session-capture scheduling units installed by install.sh.
# Leaves logs, state, and the data repo in place — they're yours.

set -eu

# Must match the LABEL used by install.sh — override via env if you
# installed with a custom label, e.g. LABEL=io.github.myhandle.ai-session-capture
LABEL="${LABEL:-ai-session-capture.daily}"

case "$(uname -s)" in
Darwin)
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    if [ -f "$PLIST" ]; then
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        rm -f "$PLIST"
        echo "removed: $PLIST"
    else
        echo "no launchd agent installed"
    fi
    ;;
Linux)
    UNIT_DIR="$HOME/.config/systemd/user"
    if [ -f "$UNIT_DIR/claude-session-capture.timer" ]; then
        systemctl --user disable --now claude-session-capture.timer 2>/dev/null || true
        rm -f "$UNIT_DIR/claude-session-capture.service" \
              "$UNIT_DIR/claude-session-capture.timer"
        systemctl --user daemon-reload
        echo "removed systemd user units from: $UNIT_DIR"
    else
        echo "no systemd units installed"
    fi
    ;;
*)
    echo "error: unsupported platform $(uname -s)" >&2
    exit 1
    ;;
esac

echo
echo "note: logs, state, and the ~/.local/share/claude-sessions data repo"
echo "were not touched. Remove them manually if you want a full clean."
