#!/bin/bash
# lstime Setup Script
#
# Add this to your shell profile (.bashrc, .zshrc, etc.):
#   source /Users/adrian/personal/my_system/setup.sh

LSTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Main lstime command (uses uv run with inline dependencies)
alias lstime="uv run $LSTIME_DIR/lstime.py"

# lsn: Interactive TUI with creation time (default)
alias lsn="uv run $LSTIME_DIR/lstime.py --tui"

# Additional convenience aliases
alias lst="uv run $LSTIME_DIR/lstime.py --no-tui"
alias lsta="uv run $LSTIME_DIR/lstime.py --no-tui -a"

echo "lstime aliases loaded (using uv). Commands:"
echo "  lsn          - Interactive TUI mode"
echo "  lstime       - Auto-detect mode"
echo "  lst / lsta   - Quick non-interactive"
