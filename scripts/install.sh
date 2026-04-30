#!/bin/sh
# Install the ai-session-capture scheduling units for the current user.
# Detects macOS vs Linux and installs the appropriate back-end:
#   macOS: ~/Library/LaunchAgents/$LABEL.plist  (default: ai-session-capture.daily.plist)
#   Linux: ~/.config/systemd/user/ai-session-capture.{service,timer}
#
# Re-running this script is idempotent — existing units are reloaded.

set -eu

# Reverse-DNS label used by launchd (macOS) and as the prefix for
# systemd unit files (Linux). Override via env if you want a
# personalized label, e.g. LABEL=io.github.myhandle.ai-session-capture
LABEL="${LABEL:-ai-session-capture.daily}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Resolve the installed CLI entrypoint. The user is expected to have run
# `uv tool install .` or `pip install --user -e .` from REPO_DIR before
# invoking this script, so the binary is somewhere on PATH.
if ! SCRIPT_PATH="$(command -v ai-session-capture 2>/dev/null)"; then
    cat >&2 <<EOF
error: ai-session-capture not found on PATH.
  Install the tool first, e.g.:
    cd "$REPO_DIR"
    uv tool install .        # or: pip install --user -e .
  Then re-run this installer.
EOF
    exit 1
fi

subst() {
    sed \
        -e "s|{{LABEL}}|$LABEL|g" \
        -e "s|{{SCRIPT_PATH}}|$SCRIPT_PATH|g" \
        -e "s|{{HOME}}|$HOME|g" \
        "$1"
}

case "$(uname -s)" in
Darwin)
    PLIST_DIR="$HOME/Library/LaunchAgents"
    PLIST="$PLIST_DIR/$LABEL.plist"
    LOG_DIR="$HOME/Library/Logs/ai-session-capture"

    mkdir -p "$PLIST_DIR" "$LOG_DIR"
    subst "$REPO_DIR/scheduling/launchd.plist.tmpl" > "$PLIST"
    chmod 0644 "$PLIST"

    # Idempotent reload: bootout is the modern verb; launchctl unload +
    # load still works for older macOS. Ignore errors from the first call
    # when the agent wasn't previously loaded.
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"

    echo "installed launchd agent: $PLIST"
    echo "next fire:"
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E 'next run|state' || true
    echo
    echo "manual test: launchctl kickstart -k \"gui/$(id -u)/$LABEL\""
    ;;
Linux)
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"

    subst "$REPO_DIR/scheduling/systemd.service.tmpl" > "$UNIT_DIR/ai-session-capture.service"
    subst "$REPO_DIR/scheduling/systemd.timer.tmpl"   > "$UNIT_DIR/ai-session-capture.timer"
    chmod 0644 "$UNIT_DIR/ai-session-capture.service" "$UNIT_DIR/ai-session-capture.timer"

    systemctl --user daemon-reload
    systemctl --user enable --now ai-session-capture.timer

    # Enable lingering so the timer fires even when the user isn't logged in
    # (e.g. closed laptop lid, headless box). Needs root — skip silently if
    # we don't have sudo available without a prompt.
    if command -v loginctl >/dev/null 2>&1; then
        if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
            if sudo -n true 2>/dev/null; then
                sudo loginctl enable-linger "$USER" || true
            else
                cat <<EOF

note: user-lingering is not enabled. Without it, the timer only fires
while you're logged in. To enable it system-wide:
    sudo loginctl enable-linger "$USER"

EOF
            fi
        fi
    fi

    echo "installed systemd user units in: $UNIT_DIR"
    echo "next fire:"
    systemctl --user list-timers ai-session-capture.timer --no-pager 2>/dev/null || true
    echo
    echo "manual test: systemctl --user start ai-session-capture.service"
    echo "tail logs:   journalctl --user -u ai-session-capture -f"
    ;;
*)
    echo "error: unsupported platform $(uname -s)" >&2
    exit 1
    ;;
esac
