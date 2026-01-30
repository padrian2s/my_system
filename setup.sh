#!/bin/bash
# lstime Setup Script
#
# Add this to your shell profile (.bashrc, .zshrc, etc.):
#   source /Users/adrian/personal/my_system/setup.sh

LSTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_LSTIME_LASTDIR_FILE="/tmp/lstime_lastdir_${USER:-user}"

# Helper: run lstime TUI then cd to the last directory
_lstime_cd() {
    uv run "$LSTIME_DIR/lstime.py" "$@"
    if [ -f "$_LSTIME_LASTDIR_FILE" ]; then
        local target
        target="$(cat "$_LSTIME_LASTDIR_FILE")"
        if [ -d "$target" ]; then
            cd "$target" || true
        fi
    fi
}

# Main lstime command (uses uv run with inline dependencies)
lstime() { _lstime_cd "$@"; }

# lsn: Interactive TUI with creation time (default)
lsn() { _lstime_cd --tui "$@"; }

# Additional convenience aliases (non-TUI, no cd needed)
alias lst="uv run $LSTIME_DIR/lstime.py --no-tui"
alias lsta="uv run $LSTIME_DIR/lstime.py --no-tui -a"

echo "lstime loaded (using uv). Commands:"
echo "  lsn          - Interactive TUI mode"
echo "  lstime       - Auto-detect mode"
echo "  lst / lsta   - Quick non-interactive"
