# Tmux Cleanup Ordering Bug Pattern

## Issue
`_cleanup_tmux_toggle()` calls `tmux kill-server` which kills the entire tmux session, including the Python process running inside it. Any code **after** `_cleanup_tmux_toggle()` never executes.

## Rule
Always perform file writes, state saves, and any other side effects **before** calling `_cleanup_tmux_toggle()`. The tmux kill is a point of no return.

## Example (action_quit)
```python
# WRONG - file never gets written
_cleanup_tmux_toggle()
LASTDIR_FILE.write_text(str(self.path))  # dead code

# CORRECT - write first, then kill tmux
LASTDIR_FILE.write_text(str(self.path))
_cleanup_tmux_toggle()
```

## LASTDIR_FILE flow
1. TUI writes current path to `/tmp/lstime_lastdir_$USER` on exit
2. Shell function in `~/.zshrc` (`lst()` / `lt()`) reads the file and `cd`s to it
3. DualPanelScreen.action_close passes active panel path back via `self.dismiss(active_path)`
4. LstimeApp.action_file_manager callback updates `self.path` from the returned value
