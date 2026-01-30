#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=0.40.0",
#     "rich>=13.0.0",
#     "anthropic>=0.40.0",
#     "pyte>=0.8.2",
# ]
# ///
"""
lstime - Directory Time Listing TUI

A text user interface for viewing directories sorted by creation or access time.
Features a split-pane view with file list on the left and details preview on the right.
Includes dual-panel file manager, fuzzy search, and file operations.

Keyboard shortcuts:
  t - Toggle between creation time and access time
  c - Sort by creation time
  a - Sort by access time
  r - Reverse sort order
  h - Toggle hidden files
  y - Copy path to clipboard
  e - Show recursive tree in preview
  [ - Shrink preview panel
  ] - Grow preview panel
  f - Toggle fullscreen (hide list panel)
  g - Toggle first/last position
  m - Open dual-panel file manager
  v - View file in modal viewer
  Ctrl+F - Fuzzy file search (fzf)
  / - Grep search (rg + fzf)
  Tab - Switch focus between panels
  Enter - Navigate into directory
  Backspace - Go to parent directory
  d - Delete selected file/directory
  R - Rename file/directory
  ! - AI Shell helper (generate shell script with Claude)
  G - Git status screen (scan for repos, batch fetch/pull/push)
  Ctrl+R - Refresh directory listing
  q - Quit
  Q - Quit and sync shell to current directory
"""

import asyncio
import fcntl
import json
import os
import pty
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import stat
import termios
import threading
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

# Config file for persisting settings
CONFIG_PATH = Path.home() / ".config" / "lstime" / "config.json"
SESSION_PATHS_FILE = Path.home() / ".config" / "lstime" / "session_paths.json"
LASTDIR_FILE = Path(f"/tmp/lstime_lastdir_{os.getenv('USER', 'user')}")


def load_config() -> dict:
    """Load configuration from file."""
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_config(config: dict) -> None:
    """Save configuration to file."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(config, indent=2))
    except OSError:
        pass


def load_session_paths(home_key: str) -> dict:
    """Load saved session paths for a specific home directory."""
    if SESSION_PATHS_FILE.exists():
        try:
            data = json.loads(SESSION_PATHS_FILE.read_text())
            return data.get(home_key, {})
        except Exception:
            pass
    return {}


def save_session_paths(home_key: str, left_path: Path, right_path: Path):
    """Save session paths keyed by home directory."""
    data = {}
    if SESSION_PATHS_FILE.exists():
        try:
            data = json.loads(SESSION_PATHS_FILE.read_text())
        except Exception:
            pass

    data[home_key] = {
        "left": str(left_path),
        "right": str(right_path)
    }
    SESSION_PATHS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATHS_FILE.write_text(json.dumps(data, indent=2))


try:
    from textual.app import App, ComposeResult
    from textual.widgets import Static, DataTable, ListView, ListItem, Label, ProgressBar, Input, Markdown
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.binding import Binding
    from textual.reactive import reactive
    from textual.screen import ModalScreen, Screen
    from textual.message import Message
    from textual.coordinate import Coordinate
    from rich.text import Text
    from rich.syntax import Syntax
    from rich.console import Group
    from rich.segment import Segment
    from textual.strip import Strip
    from textual.widget import Widget
    import pyte
    import pyte.screens
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.style import Style
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


class DirEntry(NamedTuple):
    """Directory entry with time metadata."""
    name: str
    path: Path
    created: datetime
    accessed: datetime
    modified: datetime
    size: int
    is_dir: bool


class GitRepoStatus(NamedTuple):
    """Git repository status information."""
    path: Path
    name: str
    branch: str
    status: str        # "clean", "dirty", "ahead", "behind", "diverged"
    uncommitted: int
    ahead: int
    behind: int
    untracked: int
    stash_count: int


def get_dir_entries(path: Path = None) -> list[DirEntry]:
    """Get all directory entries with their time metadata."""
    if path is None:
        path = Path.cwd()

    entries = []
    try:
        for item in path.iterdir():
            try:
                stat_info = item.stat()
                # On macOS, st_birthtime is creation time
                # On Linux, fall back to st_ctime (metadata change time)
                created = datetime.fromtimestamp(
                    getattr(stat_info, 'st_birthtime', stat_info.st_ctime)
                )
                accessed = datetime.fromtimestamp(stat_info.st_atime)
                modified = datetime.fromtimestamp(stat_info.st_mtime)

                entries.append(DirEntry(
                    name=item.name,
                    path=item,
                    created=created,
                    accessed=accessed,
                    modified=modified,
                    size=stat_info.st_size,
                    is_dir=item.is_dir()
                ))
            except (PermissionError, OSError):
                continue
    except PermissionError:
        pass

    return entries


def find_git_repos(root_path: Path, max_depth: int = 5) -> list[Path]:
    """Recursively find git repositories up to max_depth."""
    repos = []
    skip_dirs = {'node_modules', '__pycache__', '.venv', 'venv', 'vendor', '.git', 'build', 'dist'}

    def scan(path: Path, depth: int):
        if depth > max_depth:
            return
        try:
            for item in path.iterdir():
                if item.is_dir():
                    if item.name in skip_dirs:
                        continue
                    if item.name == '.git':
                        repos.append(path)
                    else:
                        git_dir = item / '.git'
                        if git_dir.exists():
                            repos.append(item)
                        else:
                            scan(item, depth + 1)
        except (PermissionError, OSError):
            pass

    # Check if root itself is a git repo
    if (root_path / '.git').exists():
        repos.append(root_path)
    scan(root_path, 1)
    return repos


def get_repo_status(repo_path: Path) -> GitRepoStatus:
    """Get detailed git status for a repository."""
    repo_path = repo_path.resolve()
    
    def run_git(args: list[str]) -> str:
        try:
            result = subprocess.run(
                ['git'] + args,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return ""

    # Get branch name
    branch = run_git(['rev-parse', '--abbrev-ref', 'HEAD']) or 'unknown'

    # Get ahead/behind counts
    ahead, behind = 0, 0
    try:
        ab_output = run_git(['rev-list', '--left-right', '--count', 'HEAD...@{upstream}'])
        if ab_output and '\t' in ab_output:
            parts = ab_output.split('\t')
            ahead = int(parts[0])
            behind = int(parts[1])
    except (ValueError, IndexError):
        pass

    # Get status (uncommitted and untracked)
    uncommitted, untracked = 0, 0
    status_output = run_git(['status', '--porcelain'])
    if status_output:
        for line in status_output.split('\n'):
            if line:
                if line.startswith('??'):
                    untracked += 1
                else:
                    uncommitted += 1

    # Get stash count
    stash_count = 0
    stash_output = run_git(['stash', 'list'])
    if stash_output:
        stash_count = len(stash_output.split('\n'))

    # Determine overall status
    if uncommitted > 0 or untracked > 0:
        status = "dirty"
    elif ahead > 0 and behind > 0:
        status = "diverged"
    elif ahead > 0:
        status = "ahead"
    elif behind > 0:
        status = "behind"
    else:
        status = "clean"

    return GitRepoStatus(
        path=repo_path,
        name=repo_path.name,
        branch=branch,
        status=status,
        uncommitted=uncommitted,
        ahead=ahead,
        behind=behind,
        untracked=untracked,
        stash_count=stash_count,
    )


def format_time(dt: datetime) -> str:
    """Format datetime as relative time (x days ago)."""
    now = datetime.now()
    diff = now - dt

    if diff.total_seconds() < 60:
        return "just now"
    elif diff.total_seconds() < 3600:
        mins = int(diff.total_seconds() / 60)
        return f"{mins}m ago"
    elif diff.total_seconds() < 86400:
        hours = int(diff.total_seconds() / 3600)
        return f"{hours}h ago"
    elif diff.days == 1:
        return "1 day ago"
    elif diff.days < 30:
        return f"{diff.days} days ago"
    elif diff.days < 365:
        months = diff.days // 30
        return f"{months}mo ago" if months > 1 else "1 month ago"
    else:
        years = diff.days // 365
        return f"{years}y ago" if years > 1 else "1 year ago"


def format_size(size: int) -> str:
    """Format file size for display."""
    for unit in ['B', 'K', 'M', 'G', 'T']:
        if size < 1024:
            if unit == 'B':
                return f"{size:>4}{unit}"
            return f"{size:>4.0f}{unit}"
        size /= 1024
    return f"{size:.0f}P"


# ═══════════════════════════════════════════════════════════════════════════════
# UI Components
# ═══════════════════════════════════════════════════════════════════════════════

if HAS_TEXTUAL:

    class PathSegment(Static):
        """A clickable path segment."""

        def __init__(self, text: str, path: Path, panel: str):
            super().__init__(text, markup=False)
            self.path = path
            self.panel = panel

        def on_click(self, event) -> None:
            """Handle click to navigate to this path."""
            event.stop()
            self.post_message(PathSegment.Clicked(self.path, self.panel))

        class Clicked(Message):
            """Message sent when path segment is clicked."""
            def __init__(self, path: Path, panel: str):
                super().__init__()
                self.path = path
                self.panel = panel


    class PathBar(Horizontal):
        """A clickable path bar showing path segments."""

        DEFAULT_CSS = """
        PathBar {
            height: 1;
            width: 100%;
            background: $surface;
            padding: 0 1;
        }
        PathBar > PathSegment {
            width: auto;
            padding: 0 0;
            color: $text-muted;
        }
        PathBar > PathSegment:hover {
            color: $primary;
            text-style: underline;
        }
        PathBar > Static.separator {
            width: auto;
            padding: 0;
            color: $text-muted;
        }
        PathBar > Static.sort-icon {
            width: auto;
            padding: 0 1 0 0;
            color: $text-muted;
        }
        """

        def __init__(self, path: Path, panel: str, sort_icon: str = ""):
            super().__init__()
            self.path = path
            self.panel = panel
            self.sort_icon = sort_icon

        def compose(self) -> ComposeResult:
            if self.sort_icon:
                yield Static(self.sort_icon, classes="sort-icon")

            # Build path segments
            parts = self.path.parts
            for i, part in enumerate(parts):
                # Build path up to this segment
                segment_path = Path(*parts[:i+1])
                yield PathSegment(part, segment_path, self.panel)
                # Add separator after each part except last and root "/"
                if i < len(parts) - 1 and part != "/":
                    yield Static("/", classes="separator")

        def update_path(self, path: Path, sort_icon: str = None):
            """Update the path bar with a new path."""
            self.path = path
            if sort_icon is not None:
                self.sort_icon = sort_icon
            self.remove_children()
            if self.sort_icon:
                self.mount(Static(self.sort_icon, classes="sort-icon"))
            parts = path.parts
            for i, part in enumerate(parts):
                segment_path = Path(*parts[:i+1])
                self.mount(PathSegment(part, segment_path, self.panel))
                if i < len(parts) - 1 and part != "/":
                    self.mount(Static("/", classes="separator"))


    class FileItem(ListItem):
        """A file/directory item for the dual panel."""

        def __init__(self, path: Path, is_selected: bool = False, is_parent: bool = False):
            super().__init__()
            self.path = path
            self.is_selected = is_selected
            self.is_parent = is_parent

        def compose(self) -> ComposeResult:
            yield Static(self._render_content(), id="item-content")

        def _render_content(self) -> str:
            if self.is_parent:
                return "  [bold cyan]/..[/]"

            is_dir = self.path.is_dir()
            mark = "*" if self.is_selected else " "
            name = self.path.name or str(self.path)

            try:
                size = "" if is_dir else format_size(self.path.stat().st_size)
            except:
                size = ""

            if is_dir:
                return f"{mark} [bold cyan]/{name:<34}[/] {size}"
            else:
                return f"{mark} {name:<35} {size}"

        def update_selection(self, is_selected: bool):
            """Update selection state without full refresh."""
            self.is_selected = is_selected
            self.query_one("#item-content", Static).update(self._render_content())


    class SearchItem(ListItem):
        """An item in the search results."""

        def __init__(self, path: Path):
            super().__init__()
            self.path = path

        def compose(self) -> ComposeResult:
            is_dir = self.path.is_dir()
            icon = "/" if is_dir else " "
            try:
                size = "" if is_dir else format_size(self.path.stat().st_size)
            except:
                size = ""
            yield Static(f" {icon} {self.path.name:<35} {size}")


    # ═══════════════════════════════════════════════════════════════════════════════
    # Modal Dialogs
    # ═══════════════════════════════════════════════════════════════════════════════

    class SearchDialog(ModalScreen):
        """Popup search dialog."""

        BINDINGS = [
            ("escape", "cancel", "Cancel"),
            ("tab", "select_first", "Select First"),
            Binding("ctrl+y", "submit", "Submit", priority=True),
        ]

        CSS = """
        * {
            scrollbar-size: 1 1;
        }
        SearchDialog {
            align: center middle;
            background: transparent;
        }
        #search-dialog {
            width: 65;
            height: 22;
            border: round $primary;
            background: $surface;
            padding: 1;
            border-title-align: left;
            border-title-color: $primary;
            border-title-background: $surface;
            border-title-style: bold;
        }
        #search-input {
            margin-bottom: 1;
            border: round $border;
            background: $surface;
        }
        #search-input:focus {
            border: round $primary;
        }
        #search-results {
            height: 1fr;
            border: round $border;
            background: $surface;
        }
        #search-results:focus {
            border: round $primary;
        }
        ListView {
            background: $surface;
        }
        ListItem {
            background: $surface;
        }
        ListItem.-highlight {
            background: $primary 30%;
        }
        ListItem.-highlight > Static {
            background: transparent;
        }
        """

        def __init__(self, items: list[Path]):
            super().__init__()
            self.all_items = items
            self.filter_text = ""

        def compose(self) -> ComposeResult:
            dialog = Vertical(id="search-dialog")
            dialog.border_title = "Search"
            with dialog:
                yield Input(placeholder="Type to filter...", id="search-input")
                yield ListView(id="search-results")

        def on_mount(self):
            self._refresh_results()
            self.query_one("#search-input", Input).focus()

        def _refresh_results(self):
            results = self.query_one("#search-results", ListView)
            results.clear()
            for path in self.all_items:
                if not self.filter_text or self.filter_text.lower() in path.name.lower():
                    results.append(SearchItem(path))

        def on_input_changed(self, event: Input.Changed):
            self.filter_text = event.value
            self._refresh_results()

        def on_input_submitted(self, event: Input.Submitted):
            event.stop()  # Prevent Enter from bubbling

        def action_submit(self):
            results = self.query_one("#search-results", ListView)
            if results.children:
                results.index = 0
                item = results.children[0]
                if isinstance(item, SearchItem):
                    self.dismiss(item.path)

        def on_list_view_selected(self, event: ListView.Selected):
            if isinstance(event.item, SearchItem):
                self.dismiss(event.item.path)

        def action_cancel(self):
            self.dismiss(None)

        def action_select_first(self):
            results = self.query_one("#search-results", ListView)
            if not results.children:
                return
            if len(results.children) == 1:
                item = results.children[0]
                if isinstance(item, SearchItem):
                    self.dismiss(item.path)
            else:
                results.index = 0
                results.focus()


    class ConfirmDialog(ModalScreen):
        """Confirmation dialog."""

        BINDINGS = [
            ("escape", "cancel", "Cancel"),
            Binding("ctrl+y", "confirm", "Yes", priority=True),
            ("y", "confirm", "Yes"),
            ("n", "cancel", "No"),
        ]

        CSS = """
        ConfirmDialog {
            align: center middle;
            background: transparent;
        }
        #confirm-dialog {
            width: 50;
            height: auto;
            border: round $error;
            background: $surface;
            padding: 1 2;
            border-title-align: left;
            border-title-color: $error;
            border-title-background: $surface;
            border-title-style: bold;
            border-subtitle-align: right;
            border-subtitle-color: $text-muted;
            border-subtitle-background: $surface;
        }
        #confirm-message {
            text-align: center;
            margin: 1 0;
        }
        """

        def __init__(self, title: str, message: str):
            super().__init__()
            self.dialog_title = title
            self.message = message

        def compose(self) -> ComposeResult:
            dialog = Vertical(id="confirm-dialog")
            dialog.border_title = self.dialog_title
            dialog.border_subtitle = "y/^Y:Yes  Esc:Cancel"
            with dialog:
                yield Label(self.message, id="confirm-message")

        def action_confirm(self):
            self.dismiss(True)

        def action_cancel(self):
            self.dismiss(False)


    class RenameDialog(ModalScreen):
        """Rename dialog."""

        BINDINGS = [
            ("escape", "cancel", "Cancel"),
            Binding("ctrl+y", "submit", "Submit", priority=True),
        ]

        CSS = """
        RenameDialog {
            align: center middle;
            background: transparent;
        }
        #rename-dialog {
            width: 80%;
            max-width: 100;
            height: auto;
            border: round $primary;
            background: $surface;
            padding: 1 2;
            border-title-align: left;
            border-title-color: $primary;
            border-title-background: $surface;
            border-title-style: bold;
            border-subtitle-align: right;
            border-subtitle-color: $text-muted;
            border-subtitle-background: $surface;
        }
        #rename-input {
            margin: 1 0;
            border: round $border;
            background: $surface;
        }
        #rename-input:focus {
            border: round $primary;
        }
        """

        def __init__(self, current_name: str):
            super().__init__()
            self.current_name = current_name

        def compose(self) -> ComposeResult:
            dialog = Vertical(id="rename-dialog")
            dialog.border_title = "Rename"
            dialog.border_subtitle = "^Y:Confirm  Esc:Cancel"
            with dialog:
                yield Input(value=self.current_name, id="rename-input")

        def on_mount(self):
            input_widget = self.query_one("#rename-input", Input)
            input_widget.focus()
            name = self.current_name
            if "." in name and not name.startswith("."):
                dot_pos = name.rfind(".")
                input_widget.selection = (0, dot_pos)
            else:
                input_widget.selection = (0, len(name))

        def on_input_submitted(self, event: Input.Submitted):
            event.stop()  # Prevent Enter from bubbling

        def action_submit(self):
            input_widget = self.query_one("#rename-input", Input)
            new_name = input_widget.value.strip()
            if new_name and new_name != self.current_name:
                self.dismiss(new_name)
            else:
                self.dismiss(None)

        def action_cancel(self):
            self.dismiss(None)

    class MkdirDialog(ModalScreen):
        """Create directory dialog."""

        BINDINGS = [
            ("escape", "cancel", "Cancel"),
            Binding("ctrl+y", "submit", "Submit", priority=True),
        ]

        CSS = """
        MkdirDialog {
            align: center middle;
            background: transparent;
        }
        #mkdir-dialog {
            width: 80%;
            max-width: 100;
            height: auto;
            border: round $primary;
            background: $surface;
            padding: 1 2;
            border-title-align: left;
            border-title-color: $primary;
            border-title-background: $surface;
            border-title-style: bold;
            border-subtitle-align: right;
            border-subtitle-color: $text-muted;
            border-subtitle-background: $surface;
        }
        #mkdir-input {
            margin: 1 0;
            border: round $border;
            background: $surface;
        }
        #mkdir-input:focus {
            border: round $primary;
        }
        """

        def compose(self) -> ComposeResult:
            dialog = Vertical(id="mkdir-dialog")
            dialog.border_title = "Create Directory"
            dialog.border_subtitle = "^Y:Confirm  Esc:Cancel"
            with dialog:
                yield Input(placeholder="Directory name", id="mkdir-input")

        def on_mount(self):
            self.query_one("#mkdir-input", Input).focus()

        def on_input_submitted(self, event: Input.Submitted):
            event.stop()

        def action_submit(self):
            input_widget = self.query_one("#mkdir-input", Input)
            name = input_widget.value.strip()
            if name:
                self.dismiss(name)
            else:
                self.dismiss(None)

        def action_cancel(self):
            self.dismiss(None)

    class ShellCommandDialog(ModalScreen):
        """Execute shell command dialog."""

        BINDINGS = [
            ("escape", "cancel", "Cancel"),
            Binding("ctrl+y", "submit", "Submit", priority=True),
        ]

        CSS = """
        ShellCommandDialog {
            align: center middle;
            background: transparent;
        }
        #shell-dialog {
            width: 80%;
            max-width: 100;
            height: auto;
            border: round $primary;
            background: $surface;
            padding: 1 2;
            border-title-align: left;
            border-title-color: $primary;
            border-title-background: $surface;
            border-title-style: bold;
            border-subtitle-align: right;
            border-subtitle-color: $text-muted;
            border-subtitle-background: $surface;
        }
        #shell-input {
            margin: 1 0;
            border: round $border;
            background: $surface;
        }
        #shell-input:focus {
            border: round $primary;
        }
        """

        def compose(self) -> ComposeResult:
            dialog = Vertical(id="shell-dialog")
            dialog.border_title = "Shell Command"
            dialog.border_subtitle = "^Y:Execute  Esc:Cancel"
            with dialog:
                yield Input(placeholder="Enter command...", id="shell-input")

        def on_mount(self):
            self.query_one("#shell-input", Input).focus()

        def on_input_submitted(self, event: Input.Submitted):
            event.stop()

        def action_submit(self):
            input_widget = self.query_one("#shell-input", Input)
            cmd = input_widget.value.strip()
            if cmd:
                self.dismiss(cmd)
            else:
                self.dismiss(None)

        def action_cancel(self):
            self.dismiss(None)


    # ═══════════════════════════════════════════════════════════════════════════════
    # AI Shell Helper
    # ═══════════════════════════════════════════════════════════════════════════════

    class AIShellDialog(ModalScreen):
        """AI Shell helper dialog - generates shell scripts using Claude."""

        BINDINGS = [
            ("escape", "cancel", "Cancel"),
            ("ctrl+s", "save_script", "Save"),
            ("ctrl+c", "copy_clipboard", "Copy"),
            Binding("ctrl+y", "submit", "Submit", priority=True),
        ]

        CSS = """
        AIShellDialog {
            align: center middle;
            background: transparent;
        }
        #ai-dialog {
            width: 90%;
            height: 90%;
            border: round $primary;
            background: $surface;
            padding: 1 2;
            border-title-align: left;
            border-title-color: $primary;
            border-title-background: $surface;
            border-title-style: bold;
            border-subtitle-align: right;
            border-subtitle-color: $text-muted;
            border-subtitle-background: $surface;
        }
        #prompt-input {
            height: 3;
            margin-bottom: 1;
            border: round $border;
            background: $surface;
        }
        #prompt-input:focus {
            border: round $primary;
        }
        #response-area {
            height: 1fr;
            border: round $border;
            background: $background;
            padding: 1;
            overflow-y: auto;
        }
        #response-text {
            width: 100%;
        }
        #status-bar {
            height: 1;
            margin-top: 1;
            color: $text-muted;
        }
        """

        def __init__(self, current_path: Path):
            super().__init__()
            self.current_path = current_path
            self.generated_script = ""
            self.is_generating = False

        def compose(self) -> ComposeResult:
            dialog = Vertical(id="ai-dialog")
            dialog.border_title = "AI Shell Helper"
            dialog.border_subtitle = "^Y:Generate  ^S:Save  ^C:Copy  Esc:Cancel"
            with dialog:
                yield Input(placeholder="Describe what you want the shell script to do...", id="prompt-input")
                with VerticalScroll(id="response-area"):
                    yield Static("", id="response-text")
                yield Static("Ready. Enter your prompt and press Enter.", id="status-bar")

        def on_mount(self):
            self.query_one("#prompt-input", Input).focus()

        def on_input_submitted(self, event: Input.Submitted):
            event.stop()  # Prevent Enter from bubbling

        def action_submit(self):
            if self.is_generating:
                return
            input_widget = self.query_one("#prompt-input", Input)
            prompt = input_widget.value.strip()
            if prompt:
                self.generate_script(prompt)

        def generate_script(self, user_prompt: str):
            self.is_generating = True
            self.generated_script = ""
            status = self.query_one("#status-bar", Static)
            response_text = self.query_one("#response-text", Static)
            response_text.update("")
            status.update("[yellow]Generating...[/]")

            def stream_response():
                try:
                    import anthropic
                    client = anthropic.Anthropic()

                    system_prompt = """You are a shell script expert. Generate a shell script based on the user's description.
Rules:
- Output ONLY the shell script code, no explanations before or after
- Start with appropriate shebang (#!/bin/bash or #!/bin/zsh)
- Include helpful comments within the script
- Make the script robust with error handling where appropriate
- Use modern bash/zsh features when beneficial
- The script should be ready to run immediately"""

                    with client.messages.stream(
                        model="claude-sonnet-4-20250514",
                        max_tokens=4096,
                        system=system_prompt,
                        messages=[{"role": "user", "content": user_prompt}]
                    ) as stream:
                        for text in stream.text_stream:
                            self.generated_script += text
                            self.app.call_from_thread(
                                response_text.update,
                                Syntax(self.generated_script, "bash", theme="monokai", line_numbers=True)
                            )

                    self.app.call_from_thread(status.update, "[green]Done![/] Press Ctrl+S to save, Esc to cancel")
                except anthropic.APIConnectionError:
                    self.app.call_from_thread(status.update, "[red]Error: Cannot connect to API[/]")
                except anthropic.AuthenticationError:
                    self.app.call_from_thread(status.update, "[red]Error: Invalid API key (set ANTHROPIC_API_KEY)[/]")
                except Exception as e:
                    self.app.call_from_thread(status.update, f"[red]Error: {e}[/]")
                finally:
                    self.is_generating = False

            thread = threading.Thread(target=stream_response, daemon=True)
            thread.start()

        def action_save_script(self):
            if not self.generated_script.strip():
                self.query_one("#status-bar", Static).update("[red]No script to save[/]")
                return

            # Generate filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"script_{timestamp}.sh"
            filepath = self.current_path / filename

            try:
                filepath.write_text(self.generated_script)
                filepath.chmod(filepath.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                self.dismiss(filepath)
            except Exception as e:
                self.query_one("#status-bar", Static).update(f"[red]Error saving: {e}[/]")

        def action_copy_clipboard(self):
            if not self.generated_script.strip():
                self.query_one("#status-bar", Static).update("[red]No script to copy[/]")
                return

            try:
                subprocess.run(["pbcopy"], input=self.generated_script.encode(), check=True)
                self.query_one("#status-bar", Static).update("[green]Copied to clipboard![/]")
            except Exception as e:
                self.query_one("#status-bar", Static).update(f"[red]Error copying: {e}[/]")

        def action_cancel(self):
            self.dismiss(None)



    class GitHelpDialog(ModalScreen):
        """Help dialog for Git Status screen."""

        BINDINGS = [
            ("escape", "close", "Close"),
            ("q", "close", "Close"),
            ("?", "close", "Close"),
            Binding("enter", "close", "Close", priority=True),
        ]

        CSS = """
        GitHelpDialog {
            align: center middle;
            background: transparent;
        }
        #help-dialog {
            width: 70;
            height: auto;
            max-height: 90%;
            border: round $primary;
            background: $surface;
            padding: 1 2;
            border-title-align: center;
            border-title-color: $primary;
            border-title-style: bold;
        }
        #help-content {
            height: auto;
        }
        .help-section {
            margin-bottom: 1;
        }
        .help-title {
            text-style: bold;
            color: $primary;
        }
        """

        def compose(self) -> ComposeResult:
            dialog = Vertical(id="help-dialog")
            dialog.border_title = "Git Status Help"
            with dialog:
                yield Static(self._get_help_content(), id="help-content")

        def _get_help_content(self) -> Text:
            text = Text()
            
            # Columns section
            text.append("COLUMNS\n", style="bold cyan")
            text.append("Sel        ", style="bold")
            text.append("* = selected (Space to toggle)\n")
            text.append("Repository ", style="bold")
            text.append("Name of git repo directory\n")
            text.append("Branch     ", style="bold")
            text.append("Current branch\n")
            text.append("Status     ", style="bold")
            text.append("clean", style="green")
            text.append("/")
            text.append("dirty", style="red")
            text.append("/")
            text.append("ahead", style="yellow")
            text.append("/")
            text.append("behind", style="cyan")
            text.append("/")
            text.append("diverg", style="magenta")
            text.append("\n")
            text.append("Changes    ", style="bold")
            text.append("M3=modified ?2=untracked\n")
            text.append("+/-        ", style="bold")
            text.append("+5=ahead -3=behind\n")
            text.append("Stash      ", style="bold")
            text.append("Number of stashes\n\n")

            # Keys section
            text.append("KEYS\n", style="bold cyan")
            text.append("Space  ", style="bold")
            text.append("Toggle selection    ")
            text.append("a  ", style="bold")
            text.append("Select all\n")
            text.append("f      ", style="bold")
            text.append("Fetch selected      ")
            text.append("r  ", style="bold")
            text.append("Refresh\n")
            text.append("p      ", style="bold")
            text.append("Push selected       ")
            text.append("s  ", style="bold")
            text.append("Cycle sort\n")
            text.append("P      ", style="bold")
            text.append("Pull selected       ")
            text.append("S  ", style="bold")
            text.append("Auto-sync\n")
            text.append("g      ", style="bold")
            text.append("Show git status     ")
            text.append("?  ", style="bold")
            text.append("This help\n")
            text.append("Enter  ", style="bold")
            text.append("Open repo           ")
            text.append("q/Esc  ", style="bold")
            text.append("Close\n\n")

            # Auto-sync explanation
            text.append("AUTO-SYNC (S)\n", style="bold cyan")
            text.append("Runs: git add -A && git commit -m 'auto-sync' && git push\n")

            return text

        def action_close(self):
            self.dismiss()


    class GitOutputDialog(ModalScreen):
        """Dialog showing git command output."""

        BINDINGS = [
            ("escape", "close", "Close"),
            ("q", "close", "Close"),
            Binding("enter", "close", "Close", priority=True),
        ]

        CSS = """
        GitOutputDialog {
            align: center middle;
            background: transparent;
        }
        #output-dialog {
            width: 80%;
            height: 80%;
            border: round $primary;
            background: $surface;
            padding: 1 2;
            border-title-align: left;
            border-title-color: $primary;
            border-title-style: bold;
            border-subtitle-align: right;
            border-subtitle-color: $text-muted;
        }
        #output-content {
            height: 1fr;
            background: $background;
            padding: 1;
            overflow-y: auto;
        }
        """

        def __init__(self, title: str, content: str):
            super().__init__()
            self.title = title
            self.content = content

        def compose(self) -> ComposeResult:
            dialog = Vertical(id="output-dialog")
            dialog.border_title = self.title
            dialog.border_subtitle = "Enter/q/Esc: Close"
            with dialog:
                with VerticalScroll(id="output-content"):
                    yield Static(self.content)

        def action_close(self):
            self.dismiss()

    class GitStatusScreen(ModalScreen):
        """Screen for viewing git repository status across directories."""

        BINDINGS = [
            ("escape", "close", "Close"),
            ("q", "close", "Close"),
            ("space", "toggle_select", "Select"),
            ("a", "select_all", "All"),
            ("r", "refresh", "Refresh"),
            Binding("enter", "open_repo", "Open", priority=True),
            ("f", "fetch_selected", "Fetch"),
            ("p", "push_selected", "Push"),
            ("P", "pull_selected", "Pull"),
            ("s", "cycle_sort", "Sort"),
            ("S", "auto_sync", "Sync"),
            ("g", "show_git_status", "Status"),
            ("?", "show_help", "Help"),
        ]

        CSS = """
        * {
            scrollbar-size: 1 1;
        }
        GitStatusScreen {
            align: center middle;
            background: transparent;
        }
        #git-container {
            width: 95%;
            height: 95%;
            background: $surface;
            border: round $primary;
            padding: 1 2;
            border-title-align: left;
            border-title-color: $primary;
            border-title-background: $surface;
            border-title-style: bold;
            border-subtitle-align: right;
            border-subtitle-color: $text-muted;
            border-subtitle-background: $surface;
        }
        #status-header {
            height: 2;
            padding: 0 1;
            color: $text;
        }
        #table-container {
            height: 1fr;
            border: round $border;
            background: $background;
            padding: 0;
        }
        #repo-table {
            height: 100%;
            background: $background;
        }
        #progress-container {
            height: 3;
            padding: 0 1;
            display: none;
        }
        #progress-container.visible {
            display: block;
        }
        #progress-text {
            height: 1;
        }
        #help-bar {
            height: 1;
            background: $surface;
            color: $text-muted;
            text-align: center;
            padding: 0 1;
        }
        DataTable > .datatable--cursor {
            background: $primary 30%;
        }
        DataTable:focus > .datatable--cursor {
            background: $primary 50%;
        }
        """

        def __init__(self, scan_path: Path):
            super().__init__()
            self.scan_path = scan_path
            self.repos: list[GitRepoStatus] = []
            self.selected: set[Path] = set()
            self.scanning = False
            self.sort_mode = "name"  # name, status, branch

        def compose(self) -> ComposeResult:
            container = Vertical(id="git-container")
            container.border_title = "Git Status"
            container.border_subtitle = "Space:Sel  f:Fetch  p:Push  P:Pull  S:Sync  g:Status  ?:Help  Esc:Close"
            with container:
                yield Static("", id="status-header")
                with Vertical(id="table-container"):
                    table = DataTable(id="repo-table")
                    table.cursor_type = "row"
                    yield table
                with Vertical(id="progress-container"):
                    yield Static("", id="progress-text")
                    yield ProgressBar(id="progress-bar", total=100)
                yield Label("", id="help-bar")

        def on_mount(self):
            table = self.query_one("#repo-table", DataTable)
            table.add_column("", key="sel", width=3)
            table.add_column("Repository", key="name", width=30)
            table.add_column("Branch", key="branch", width=20)
            table.add_column("Status", key="status", width=10)
            table.add_column("Changes", key="changes", width=10)
            table.add_column("+/-", key="ahead_behind", width=8)
            table.add_column("Stash", key="stash", width=6)
            table.focus()
            self._start_scan()

        def _start_scan(self):
            if self.scanning:
                return
            self.scanning = True
            self.repos = []
            self.selected.clear()

            header = self.query_one("#status-header", Static)
            header.update(f"Scanning: {self.scan_path}")

            progress_container = self.query_one("#progress-container")
            progress_container.add_class("visible")
            progress_text = self.query_one("#progress-text", Static)
            progress_bar = self.query_one("#progress-bar", ProgressBar)
            progress_text.update("Finding git repositories...")
            progress_bar.update(progress=0)

            def do_scan():
                try:
                    repo_paths = find_git_repos(self.scan_path)
                    total = len(repo_paths)
                    self.app.call_from_thread(progress_text.update, f"Found {total} repositories, getting status...")

                    for i, repo_path in enumerate(repo_paths):
                        status = get_repo_status(repo_path)
                        self.repos.append(status)
                        progress = int(((i + 1) / max(total, 1)) * 100)
                        self.app.call_from_thread(progress_bar.update, progress=progress)

                    self.app.call_from_thread(self._scan_complete)
                except Exception as e:
                    self.app.call_from_thread(self.notify, f"Scan error: {e}", timeout=3)
                    self.app.call_from_thread(self._scan_complete)

            thread = threading.Thread(target=do_scan, daemon=True)
            thread.start()

        def _scan_complete(self):
            self.scanning = False
            progress_container = self.query_one("#progress-container")
            progress_container.remove_class("visible")
            self._update_header()
            self._refresh_table()

        def _update_header(self):
            header = self.query_one("#status-header", Static)
            total = len(self.repos)
            dirty = sum(1 for r in self.repos if r.status == "dirty")
            ahead = sum(1 for r in self.repos if r.status in ("ahead", "diverged"))
            behind = sum(1 for r in self.repos if r.status in ("behind", "diverged"))
            header.update(f"Path: {self.scan_path}  |  Repos: {total}  Dirty: {dirty}  Ahead: {ahead}  Behind: {behind}")

        def _refresh_table(self):
            table = self.query_one("#repo-table", DataTable)
            table.clear()

            # Sort repos
            sorted_repos = list(self.repos)
            if self.sort_mode == "name":
                sorted_repos.sort(key=lambda r: r.name.lower())
            elif self.sort_mode == "status":
                status_order = {"dirty": 0, "diverged": 1, "ahead": 2, "behind": 3, "clean": 4}
                sorted_repos.sort(key=lambda r: (status_order.get(r.status, 5), r.name.lower()))
            elif self.sort_mode == "branch":
                sorted_repos.sort(key=lambda r: (r.branch.lower(), r.name.lower()))

            for repo in sorted_repos:
                sel = "*" if repo.path in self.selected else " "

                # Format status with color
                status_styles = {
                    "clean": ("clean", "green"),
                    "dirty": ("dirty", "red"),
                    "ahead": ("ahead", "yellow"),
                    "behind": ("behind", "cyan"),
                    "diverged": ("diverg", "magenta"),
                }
                status_text, status_color = status_styles.get(repo.status, (repo.status, "white"))

                # Format changes
                changes = []
                if repo.uncommitted > 0:
                    changes.append(f"M{repo.uncommitted}")
                if repo.untracked > 0:
                    changes.append(f"?{repo.untracked}")
                changes_text = " ".join(changes) if changes else "-"

                # Format ahead/behind
                ab_parts = []
                if repo.ahead > 0:
                    ab_parts.append(f"+{repo.ahead}")
                if repo.behind > 0:
                    ab_parts.append(f"-{repo.behind}")
                ab_text = "/".join(ab_parts) if ab_parts else "-"

                # Format stash
                stash_text = str(repo.stash_count) if repo.stash_count > 0 else "-"

                table.add_row(
                    sel,
                    repo.name,
                    repo.branch,
                    Text(status_text, style=status_color),
                    changes_text,
                    ab_text,
                    stash_text,
                    key=str(repo.path),
                )

        def _get_selected_row_path(self) -> Path | None:
            table = self.query_one("#repo-table", DataTable)
            if table.cursor_row is not None and table.row_count > 0:
                try:
                    coord = Coordinate(table.cursor_row, 0)
                    cell_key = table.coordinate_to_cell_key(coord)
                    return Path(cell_key.row_key.value) if cell_key.row_key else None
                except Exception:
                    pass
            return None

        def _get_repo_by_path(self, path: Path) -> GitRepoStatus | None:
            for repo in self.repos:
                if repo.path == path:
                    return repo
            return None

        def action_close(self):
            self.dismiss()

        def action_toggle_select(self):
            table = self.query_one("#repo-table", DataTable)
            current_row = table.cursor_row
            path = self._get_selected_row_path()
            if path:
                if path in self.selected:
                    self.selected.discard(path)
                else:
                    self.selected.add(path)
                self._refresh_table()
                # Restore cursor and move down
                if current_row is not None:
                    next_row = min(current_row + 1, table.row_count - 1)
                    table.move_cursor(row=next_row)

        def action_select_all(self):
            table = self.query_one("#repo-table", DataTable)
            current_row = table.cursor_row
            if len(self.selected) == len(self.repos):
                self.selected.clear()
            else:
                self.selected = {r.path for r in self.repos}
            self._refresh_table()
            # Restore cursor position
            if current_row is not None and table.row_count > 0:
                table.move_cursor(row=min(current_row, table.row_count - 1))

        def action_refresh(self):
            self._start_scan()

        def action_open_repo(self):
            path = self._get_selected_row_path()
            if path:
                self.dismiss(path)

        def action_cycle_sort(self):
            table = self.query_one("#repo-table", DataTable)
            current_row = table.cursor_row
            modes = ["name", "status", "branch"]
            current_idx = modes.index(self.sort_mode)
            self.sort_mode = modes[(current_idx + 1) % len(modes)]
            self.notify(f"Sort: {self.sort_mode}", timeout=1)
            self._refresh_table()
            # Restore cursor position
            if current_row is not None and table.row_count > 0:
                table.move_cursor(row=min(current_row, table.row_count - 1))


        def action_show_help(self):
            self.app.push_screen(GitHelpDialog())


        def action_show_git_status(self):
            path = self._get_selected_row_path()
            if not path:
                self.notify("No repo selected", timeout=2)
                return
            try:
                result = subprocess.run(
                    ['git', 'status'],
                    cwd=str(path),
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                output = result.stdout or result.stderr or "No output"
                self.app.push_screen(GitOutputDialog(f"git status - {path.name}", output))
            except Exception as e:
                self.notify(f"Error: {e}", timeout=3)


        def action_auto_sync(self):
            """Add all, commit with 'auto-sync' message, and push for selected repos."""
            targets = list(self.selected) if self.selected else []
            path = self._get_selected_row_path()
            if not targets and path:
                targets = [path]
            if not targets:
                self.notify("No repos selected", timeout=2)
                return
            self._run_auto_sync(targets)

        def _run_auto_sync(self, paths: list[Path]):
            if self.scanning:
                return

            self.scanning = True
            progress_container = self.query_one("#progress-container")
            progress_container.add_class("visible")
            progress_text = self.query_one("#progress-text", Static)
            progress_bar = self.query_one("#progress-bar", ProgressBar)
            progress_text.update("Running auto-sync...")
            progress_bar.update(progress=0)

            def do_sync():
                total = len(paths)
                errors = []
                synced = 0
                for i, path in enumerate(paths):
                    self.app.call_from_thread(progress_text.update, f"Syncing: {path.name} ({i+1}/{total})")
                    try:
                        # git add -A
                        result = subprocess.run(
                            ['git', 'add', '-A'],
                            cwd=str(path),
                            capture_output=True,
                            text=True,
                            timeout=30
                        )
                        if result.returncode != 0:
                            errors.append(f"{path.name}: add failed - {result.stderr.strip()}")
                            continue

                        # git commit -m "auto-sync"
                        result = subprocess.run(
                            ['git', 'commit', '-m', 'auto-sync'],
                            cwd=str(path),
                            capture_output=True,
                            text=True,
                            timeout=30
                        )
                        # returncode 1 with "nothing to commit" is ok
                        if result.returncode != 0 and "nothing to commit" not in result.stdout:
                            errors.append(f"{path.name}: commit failed - {result.stderr.strip()}")
                            continue

                        # git push
                        result = subprocess.run(
                            ['git', 'push'],
                            cwd=str(path),
                            capture_output=True,
                            text=True,
                            timeout=60
                        )
                        if result.returncode != 0:
                            errors.append(f"{path.name}: push failed - {result.stderr.strip()}")
                            continue

                        synced += 1
                    except Exception as e:
                        errors.append(f"{path.name}: {e}")
                    progress = int(((i + 1) / total) * 100)
                    self.app.call_from_thread(progress_bar.update, progress=progress)

                if errors:
                    self.app.call_from_thread(self.notify, f"{len(errors)} error(s), {synced} synced", timeout=3)
                else:
                    self.app.call_from_thread(self.notify, f"Synced {synced} repo(s)", timeout=2)

                self.app.call_from_thread(self._operation_complete)

            thread = threading.Thread(target=do_sync, daemon=True)
            thread.start()

        def action_fetch_selected(self):
            targets = list(self.selected) if self.selected else []
            path = self._get_selected_row_path()
            if not targets and path:
                targets = [path]
            if not targets:
                self.notify("No repos selected", timeout=2)
                return
            self._run_git_operation(targets, "fetch", ["fetch"])

        def action_push_selected(self):
            targets = list(self.selected) if self.selected else []
            path = self._get_selected_row_path()
            if not targets and path:
                targets = [path]
            if not targets:
                self.notify("No repos selected", timeout=2)
                return
            self._run_git_operation(targets, "push", ["push"])

        def action_pull_selected(self):
            targets = list(self.selected) if self.selected else []
            path = self._get_selected_row_path()
            if not targets and path:
                targets = [path]
            if not targets:
                self.notify("No repos selected", timeout=2)
                return
            self._run_git_operation(targets, "pull", ["pull"])

        def _run_git_operation(self, paths: list[Path], op_name: str, git_args: list[str]):
            if self.scanning:
                return

            self.scanning = True
            progress_container = self.query_one("#progress-container")
            progress_container.add_class("visible")
            progress_text = self.query_one("#progress-text", Static)
            progress_bar = self.query_one("#progress-bar", ProgressBar)
            progress_text.update(f"Running git {op_name}...")
            progress_bar.update(progress=0)

            def do_operation():
                total = len(paths)
                errors = []
                for i, path in enumerate(paths):
                    self.app.call_from_thread(progress_text.update, f"{op_name}: {path.name} ({i+1}/{total})")
                    try:
                        result = subprocess.run(
                            ['git'] + git_args,
                            cwd=str(path),
                            capture_output=True,
                            text=True,
                            timeout=60
                        )
                        if result.returncode != 0:
                            errors.append(f"{path.name}: {result.stderr.strip()}")
                    except Exception as e:
                        errors.append(f"{path.name}: {e}")
                    progress = int(((i + 1) / total) * 100)
                    self.app.call_from_thread(progress_bar.update, progress=progress)

                if errors:
                    self.app.call_from_thread(self.notify, f"{len(errors)} errors during {op_name}", timeout=3)
                else:
                    self.app.call_from_thread(self.notify, f"{op_name} complete on {total} repo(s)", timeout=2)

                # Refresh status
                self.app.call_from_thread(self._operation_complete)

            thread = threading.Thread(target=do_operation, daemon=True)
            thread.start()

        def _operation_complete(self):
            self.scanning = False
            progress_container = self.query_one("#progress-container")
            progress_container.remove_class("visible")
            # Re-scan to get updated status
            self._start_scan()


    # ═══════════════════════════════════════════════════════════════════════════════
    # File Viewer
    # ═══════════════════════════════════════════════════════════════════════════════

    class FileViewer(VerticalScroll):
        """Scrollable file content viewer with syntax highlighting."""

        file_path = reactive(None)
        MARKDOWN_EXTENSIONS = {'.md', '.markdown', '.mdown', '.mkd'}

        LEXER_MAP = {
            '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
            '.tsx': 'tsx', '.jsx': 'jsx', '.json': 'json',
            '.yaml': 'yaml', '.yml': 'yaml', '.html': 'html',
            '.css': 'css', '.scss': 'scss', '.md': 'markdown',
            '.sh': 'bash', '.bash': 'bash', '.zsh': 'zsh',
            '.sql': 'sql', '.rs': 'rust', '.go': 'go',
            '.rb': 'ruby', '.java': 'java', '.c': 'c',
            '.cpp': 'cpp', '.h': 'c', '.hpp': 'cpp',
            '.toml': 'toml', '.xml': 'xml', '.vue': 'vue',
            '.php': 'php', '.swift': 'swift', '.kt': 'kotlin',
            '.lua': 'lua', '.r': 'r', '.dockerfile': 'dockerfile',
            '.hs': 'haskell', '.scala': 'scala', '.ex': 'elixir',
            '.exs': 'elixir', '.nim': 'nim', '.clj': 'clojure',
            '.erl': 'erlang', '.ml': 'ocaml', '.fs': 'fsharp',
            '.zig': 'zig', '.dart': 'dart', '.groovy': 'groovy',
            '.gradle': 'groovy', '.pl': 'perl', '.jl': 'julia',
        }

        def compose(self) -> ComposeResult:
            yield Static("", id="file-content")
            yield Markdown("", id="md-content")

        def on_mount(self):
            self.query_one("#md-content").display = False

        def load_file(self, path: Path):
            self.file_path = path
            is_markdown = path.suffix.lower() in self.MARKDOWN_EXTENSIONS

            static_widget = self.query_one("#file-content", Static)
            md_widget = self.query_one("#md-content", Markdown)

            image_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.tiff', '.tif'}
            binary_extensions = {'.pdf', '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar',
                               '.exe', '.dll', '.so', '.dylib', '.bin', '.dat',
                               '.mp3', '.mp4', '.avi', '.mov', '.mkv', '.wav', '.flac',
                               '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}

            suffix = path.suffix.lower()

            if suffix in image_extensions:
                static_widget.display = True
                md_widget.display = False
                static_widget.update(f"[bold magenta]{path.name}[/bold magenta]\n\n[dim]Image file - press 'o' to open[/dim]")
                self.scroll_home()
                return

            if suffix in binary_extensions:
                static_widget.display = True
                md_widget.display = False
                static_widget.update(f"[yellow]Binary file: {path.name}[/yellow]\n\n[dim]Cannot display {suffix} files[/dim]")
                self.scroll_home()
                return

            try:
                with open(path, 'r', errors='replace') as f:
                    code = f.read()

                if is_markdown:
                    static_widget.display = False
                    md_widget.display = True
                    md_widget.update(code)
                else:
                    static_widget.display = True
                    md_widget.display = False

                    line_count = len(code.splitlines())
                    lexer = self.LEXER_MAP.get(suffix)
                    if lexer is None and path.name.lower() == 'dockerfile':
                        lexer = 'dockerfile'

                    header = Text()
                    header.append(f"{path.name}", style="bold magenta")
                    header.append(f" ({line_count} lines)", style="dim")
                    header.append("\n" + "-" * 50 + "\n", style="dim")

                    if lexer:
                        syntax = Syntax(code, lexer, theme="monokai", line_numbers=True, word_wrap=False)
                        static_widget.update(Group(header, syntax))
                    else:
                        lines = code.splitlines()
                        plain_content = Text()
                        for i, line in enumerate(lines, 1):
                            plain_content.append(f"{i:4} ", style="dim")
                            plain_content.append(f"{line}\n")
                        static_widget.update(Group(header, plain_content))

            except Exception as e:
                static_widget.display = True
                md_widget.display = False
                static_widget.update(f"[red]Error: {e}[/red]")

            self.scroll_home()

        def clear(self):
            self.file_path = None
            self.query_one("#file-content", Static).display = True
            self.query_one("#md-content", Markdown).display = False
            self.query_one("#file-content", Static).update("[dim]Select a file to view[/dim]")


    class FileViewerScreen(ModalScreen):
        """Modal screen for viewing a file."""

        BINDINGS = [
            ("escape", "close", "Close"),
            Binding("enter", "close", "Close", priority=True),
            ("q", "close", "Close"),
            ("v", "close", "Close"),
        ]

        CSS = """
        * {
            scrollbar-size: 1 1;
        }
        FileViewerScreen {
            align: center middle;
            background: transparent;
        }
        #viewer-container {
            width: 95%;
            height: 95%;
            background: $surface;
            border: round $primary;
            padding: 0;
            border-title-align: left;
            border-title-color: $primary;
            border-title-background: $surface;
            border-title-style: bold;
            border-subtitle-align: right;
            border-subtitle-color: $text-muted;
            border-subtitle-background: $surface;
        }
        #viewer-content {
            height: 1fr;
            background: $surface;
            padding: 0 1;
        }
        FileViewer {
            background: $surface;
        }
        """

        def __init__(self, file_path: Path):
            super().__init__()
            self.file_path = file_path

        def compose(self) -> ComposeResult:
            container = Vertical(id="viewer-container")
            container.border_title = f"{self.file_path.name}"
            container.border_subtitle = "Enter/q/Esc:Close"
            with container:
                yield FileViewer(id="viewer-content")

        def on_mount(self):
            viewer = self.query_one("#viewer-content", FileViewer)
            viewer.load_file(self.file_path)

        def action_close(self):
            self.dismiss()


    # ═══════════════════════════════════════════════════════════════════════════════
    # Terminal Emulator
    # ═══════════════════════════════════════════════════════════════════════════════

    # Default character for empty terminal cells
    _TERM_DEFAULT_CHAR = pyte.screens.Char(
        data=" ",
        fg="default",
        bg="default",
        bold=False,
        italics=False,
        underscore=False,
        strikethrough=False,
        reverse=False,
        blink=False,
    )

    class Terminal(Widget, can_focus=True):
        """A terminal emulator widget that runs a shell subprocess."""

        THEMES = {
            "github-dark": {
                "bg": "#0d1117",
                "fg": "#c9d1d9",
                "border": "#30363d",
                "border_focus": "#58a6ff",
                "black": "#484f58",
                "red": "#ff7b72",
                "green": "#3fb950",
                "yellow": "#d29922",
                "blue": "#58a6ff",
                "magenta": "#bc8cff",
                "cyan": "#39c5cf",
                "white": "#b1bac4",
                "brightblack": "#6e7681",
                "brightred": "#ffa198",
                "brightgreen": "#56d364",
                "brightyellow": "#e3b341",
                "brightblue": "#79c0ff",
                "brightmagenta": "#d2a8ff",
                "brightcyan": "#56d4dd",
                "brightwhite": "#f0f6fc",
            },
        }

        DEFAULT_CSS = """
        Terminal {
            padding: 0 1;
            border: round #30363d;
        }
        Terminal:focus {
            border: round #58a6ff;
        }
        """

        cursor_visible: reactive[bool] = reactive(True)
        _blink_state: reactive[bool] = reactive(True)
        theme: reactive[str] = reactive("github-dark")

        class Ready(Message):
            """Posted when the terminal is ready."""
            def __init__(self, terminal: "Terminal") -> None:
                self.terminal = terminal
                super().__init__()

        class Closed(Message):
            """Posted when the terminal process has closed."""
            def __init__(self, terminal: "Terminal", exit_code: int | None) -> None:
                self.terminal = terminal
                self.exit_code = exit_code
                super().__init__()

        def __init__(
            self,
            command: str | None = None,
            *,
            cwd: str | None = None,
            theme: str = "github-dark",
            pty_state: dict | None = None,
            name: str | None = None,
            id: str | None = None,
            classes: str | None = None,
            disabled: bool = False,
        ) -> None:
            super().__init__(name=name, id=id, classes=classes, disabled=disabled)
            self.theme = theme
            self._command = command or os.environ.get("SHELL", "/bin/bash")
            self._cwd = cwd
            self._reader_task: asyncio.Task | None = None
            if pty_state:
                self._master_fd = pty_state["master_fd"]
                self._pid = pty_state["pid"]
                self._screen = pty_state["screen"]
                self._stream = pty_state["stream"]
                self._running = True
            else:
                self._master_fd: int | None = None
                self._pid: int | None = None
                self._screen: pyte.Screen | None = None
                self._stream: pyte.Stream | None = None
                self._running = False

        def detach_pty(self) -> dict | None:
            """Extract PTY state so the process survives widget destruction."""
            if not self._running or self._master_fd is None:
                return None
            if self._reader_task:
                self._reader_task.cancel()
                self._reader_task = None
            state = {
                "master_fd": self._master_fd,
                "pid": self._pid,
                "screen": self._screen,
                "stream": self._stream,
            }
            # Prevent on_unmount from killing the process
            self._master_fd = None
            self._pid = None
            self._running = False
            return state

        @property
        def theme_colors(self) -> dict[str, str]:
            return self.THEMES.get(self.theme, self.THEMES["github-dark"])

        def watch_theme(self, theme: str) -> None:
            colors = self.THEMES.get(theme, self.THEMES["github-dark"])
            self.styles.border = ("round", colors["border"])
            self.refresh()

        def on_mount(self) -> None:
            colors = self.theme_colors
            self.styles.border = ("round", colors["border"])
            if not self._running:
                self._start_terminal()
            else:
                # Reconnecting to existing PTY — restart the reader
                self._reader_task = asyncio.create_task(self._read_pty())
            self.set_interval(0.5, self._toggle_blink)

        def on_unmount(self) -> None:
            self._stop_terminal()

        def _toggle_blink(self) -> None:
            if self.has_focus:
                self._blink_state = not self._blink_state
                self.refresh()

        def _start_terminal(self) -> None:
            cols = max(self.size.width - 2, 80)
            rows = max(self.size.height - 2, 24)

            self._screen = pyte.Screen(cols, rows)
            self._stream = pyte.Stream(self._screen)

            self._pid, self._master_fd = pty.fork()

            if self._pid == 0:
                # Child process
                if self._cwd:
                    try:
                        os.chdir(self._cwd)
                    except OSError:
                        pass
                env = os.environ.copy()
                env["TERM"] = "xterm-256color"
                env["COLORTERM"] = "truecolor"
                env["COLUMNS"] = str(cols)
                env["LINES"] = str(rows)
                shell = self._command
                shell_name = os.path.basename(shell)
                try:
                    os.execvpe(shell, [shell_name], env)
                except Exception:
                    os._exit(1)
            else:
                self._set_pty_size(cols, rows)
                flags = fcntl.fcntl(self._master_fd, fcntl.F_GETFL)
                fcntl.fcntl(self._master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                self._running = True
                self._reader_task = asyncio.create_task(self._read_pty())
                self.post_message(self.Ready(self))

        def _stop_terminal(self) -> None:
            self._running = False
            if self._reader_task:
                self._reader_task.cancel()
                self._reader_task = None
            if self._master_fd is not None:
                try:
                    os.close(self._master_fd)
                except OSError:
                    pass
                self._master_fd = None
            if self._pid is not None:
                try:
                    os.kill(self._pid, signal.SIGTERM)
                    os.waitpid(self._pid, os.WNOHANG)
                except (OSError, ChildProcessError):
                    pass
                self._pid = None

        def _set_pty_size(self, cols: int, rows: int) -> None:
            if self._master_fd is not None:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                try:
                    fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
                except OSError:
                    pass

        async def _read_pty(self) -> None:
            while self._running and self._master_fd is not None:
                try:
                    await asyncio.sleep(0.01)
                    try:
                        data = os.read(self._master_fd, 65536)
                        if data:
                            self._stream.feed(data.decode("utf-8", errors="replace"))
                            self.refresh()
                        else:
                            break
                    except BlockingIOError:
                        import select
                        readable, _, _ = select.select([self._master_fd], [], [], 0.05)
                        if not readable:
                            continue
                except OSError:
                    break
                except asyncio.CancelledError:
                    break
            if self._running:
                self._running = False
                exit_code = self._get_exit_code()
                self.post_message(self.Closed(self, exit_code))

        def _get_exit_code(self) -> int | None:
            if self._pid is None:
                return None
            try:
                _, status = os.waitpid(self._pid, os.WNOHANG)
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
            except (OSError, ChildProcessError):
                pass
            return None

        def send_data(self, data: str | bytes) -> None:
            if self._master_fd is None or not self._running:
                return
            if isinstance(data, str):
                data = data.encode("utf-8")
            try:
                os.write(self._master_fd, data)
            except OSError:
                pass

        def on_key(self, event) -> None:
            if not self._running or self._master_fd is None:
                return
            # Let ctrl+backslash pass through to close the terminal screen
            if event.key == "ctrl+backslash":
                return
            event.stop()
            event.prevent_default()
            key_map = {
                "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
                "home": "\x1b[H", "end": "\x1b[F",
                "insert": "\x1b[2~", "delete": "\x1b[3~",
                "pageup": "\x1b[5~", "pagedown": "\x1b[6~",
                "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS",
                "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~", "f8": "\x1b[19~",
                "f9": "\x1b[20~", "f10": "\x1b[21~", "f11": "\x1b[23~", "f12": "\x1b[24~",
                "tab": "\t", "enter": "\r", "escape": "\x1b", "backspace": "\x7f",
            }
            key = event.key
            if key in key_map:
                self.send_data(key_map[key])
            elif event.character:
                self.send_data(event.character)

        def on_resize(self, event) -> None:
            if self._screen is None:
                return
            cols = max(event.size.width - 2, 1)
            rows = max(event.size.height - 2, 1)
            self._screen.resize(rows, cols)
            self._set_pty_size(cols, rows)
            self.refresh()

        def on_focus(self, event) -> None:
            self._blink_state = True
            self.refresh()

        def on_blur(self, event) -> None:
            self.refresh()

        def _get_color(self, color: str, default: str | None) -> str | None:
            if color == "default":
                return default
            theme = self.theme_colors
            color_map = {
                "black": theme["black"], "red": theme["red"],
                "green": theme["green"], "brown": theme["yellow"],
                "yellow": theme["yellow"], "blue": theme["blue"],
                "magenta": theme["magenta"], "cyan": theme["cyan"],
                "white": theme["white"],
                "brightblack": theme["brightblack"], "brightred": theme["brightred"],
                "brightgreen": theme["brightgreen"], "brightbrown": theme["brightyellow"],
                "brightyellow": theme["brightyellow"], "brightblue": theme["brightblue"],
                "brightmagenta": theme["brightmagenta"], "brightcyan": theme["brightcyan"],
                "brightwhite": theme["brightwhite"],
            }
            if color in color_map:
                return color_map[color]
            if len(color) == 6:
                try:
                    int(color, 16)
                    return f"#{color}"
                except ValueError:
                    pass
            return default

        def render_line(self, y: int) -> Strip:
            if self._screen is None:
                return Strip.blank(self.size.width)
            if y >= self._screen.lines:
                return Strip.blank(self.size.width)
            buffer = self._screen.buffer
            line = buffer.get(y, {})
            segments: list[Segment] = []
            cursor_x = self._screen.cursor.x
            cursor_y = self._screen.cursor.y
            show_cursor = self.has_focus and self._blink_state and cursor_y == y
            for x in range(self._screen.columns):
                char = line.get(x, _TERM_DEFAULT_CHAR)
                colors = self.theme_colors
                fg = self._get_color(char.fg, colors["fg"])
                bg = self._get_color(char.bg, None)
                is_cursor = show_cursor and x == cursor_x
                style = Style(
                    color=fg,
                    bgcolor=bg if bg else colors["bg"],
                    bold=char.bold,
                    italic=char.italics,
                    underline=char.underscore,
                    strike=char.strikethrough,
                    reverse=is_cursor,
                )
                text = char.data if char.data else " "
                segments.append(Segment(text, style))
            return Strip(segments)


    class TerminalScreen(ModalScreen):
        """Fullscreen terminal emulator modal."""

        BINDINGS = [
            Binding("ctrl+backslash", "close_terminal", "Close", priority=True),
            Binding("ctrl+t", "toggle_terminal", "Toggle", priority=True),
        ]

        DEFAULT_CSS = """
        TerminalScreen {
            align: center middle;
            background: #0d1117;
        }
        TerminalScreen Terminal {
            width: 100%;
            height: 100%;
            border: round #444;
            border-title-color: #58a6ff;
            border-subtitle-color: #8b949e;
        }
        TerminalScreen Terminal:focus {
            border: round #58a6ff;
        }
        """

        def __init__(self, cwd: str | None = None, pty_state: dict | None = None) -> None:
            super().__init__()
            self._cwd = cwd
            self._pty_state = pty_state

        def compose(self) -> ComposeResult:
            yield Terminal(cwd=self._cwd, theme="github-dark", pty_state=self._pty_state)

        def on_mount(self) -> None:
            terminal = self.query_one(Terminal)
            terminal.border_title = "Terminal"
            terminal.border_subtitle = "ctrl+t toggle | ctrl+\\ close"
            terminal.focus()

        def on_terminal_closed(self, event: Terminal.Closed) -> None:
            self.dismiss(("closed", None))

        def action_toggle_terminal(self) -> None:
            """Hide terminal but keep process alive."""
            pty_state = self.query_one(Terminal).detach_pty()
            self.dismiss(("toggle", pty_state))

        def action_close_terminal(self) -> None:
            """Kill terminal and close."""
            self.dismiss(("closed", None))


    # ═══════════════════════════════════════════════════════════════════════════════
    # Tmux Launcher — each lst gets its own tmux server (socket) for full isolation
    # ═══════════════════════════════════════════════════════════════════════════════

    def _lst_tmux_launch(argv: list[str], target_path: str) -> None:
        """Launch lst inside a dedicated tmux server with C-t toggle.

        Uses a separate tmux socket (-L) per path so multiple lst instances
        don't interfere with each other or the user's main tmux.

        C-t toggles between window 0 (lst) and window 1 (terminal).
        """
        import hashlib
        sock = "lst_" + hashlib.md5(target_path.encode()).hexdigest()[:8]
        tmux = ["tmux", "-L", sock]

        # Check if this server already has a session
        check = subprocess.run(tmux + ["has-session"], capture_output=True)
        if check.returncode == 0:
            # Already running — just attach
            os.execvp("tmux", tmux + ["attach-session"])

        # Build shell command that sets the env var so inner lst skips launcher
        inner_cmd = "_LST_INSIDE_TMUX=1 " + " ".join(
            shlex.quote(a) for a in argv
        )

        # Create new session: window 0 runs lst
        subprocess.run(tmux + [
            "new-session", "-d", "-s", "main", "-n", "lst",
            "-c", target_path, inner_cmd,
        ])

        # Status bar
        subprocess.run(tmux + ["set-option", "-g", "status-left",
                        f" lst [{os.path.basename(target_path)}] "])
        subprocess.run(tmux + ["set-option", "-g", "status-right",
                        " C-t: toggle terminal "])
        subprocess.run(tmux + ["set-option", "-g", "status-style",
                        "bg=#1a1a2e,fg=#58a6ff"])

        # C-t: if only 1 window, create terminal; otherwise toggle
        toggle = (
            'if [ $(tmux -L ' + sock + ' list-windows | wc -l) -eq 1 ]; then '
            'tmux -L ' + sock + ' new-window -n term; '
            'else tmux -L ' + sock + ' last-window; fi'
        )
        subprocess.run(tmux + ["bind-key", "-n", "C-t", "run-shell", toggle])

        # Attach
        os.execvp("tmux", tmux + ["attach-session"])

    def _cleanup_tmux_toggle() -> None:
        """Kill the lst tmux server on quit (no-op if not inside tmux)."""
        if not os.environ.get("_LST_INSIDE_TMUX"):
            return
        # The socket name is stored in $TMUX as /tmp/tmux-UID/lst_HASH,...
        tmux_env = os.environ.get("TMUX", "")
        if not tmux_env:
            return
        socket_path = tmux_env.split(",")[0]
        sock_name = Path(socket_path).name if socket_path else ""
        if sock_name.startswith("lst_"):
            subprocess.run(["tmux", "-L", sock_name, "kill-server"],
                           capture_output=True)

    # ═══════════════════════════════════════════════════════════════════════════════
    # Dual Panel File Manager
    # ═══════════════════════════════════════════════════════════════════════════════

    class DualPanelScreen(Screen):
        """Dual panel file manager for copying files."""

        _session_left_path: Path = None
        _session_right_path: Path = None
        _session_sort_left: bool = False
        _session_sort_right: bool = False
        _session_left_index: int = 1
        _session_right_index: int = 1
        _initial_start_path: Path = None
        _session_show_hidden: bool = True

        BINDINGS = [
            ("escape", "cancel_or_close", "Close"),
            ("q", "close", "Close"),
            ("tab", "switch_panel", "Switch"),
            ("space", "toggle_select", "Select"),
            ("backspace", "go_up", "Up"),
            ("c", "copy_selected", "Copy"),
            ("m", "move_selected", "Move"),
            ("a", "select_all", "All"),
            ("s", "toggle_sort", "Sort"),
            Binding("R", "rename", "Rename", priority=True),
            Binding("d", "delete", "Delete", priority=True),
            Binding("p", "mkdir", "Mkdir", priority=True),
            Binding("g", "toggle_position", "g=jump"),
            Binding("n", "quick_select", "n=select"),
            ("pageup", "page_up", "PgUp"),
            ("pagedown", "page_down", "PgDn"),
            ("h", "go_home", "Home"),
            ("i", "sync_panels", "Sync"),
            ("v", "view_file", "View"),
            Binding("E", "edit_nano", "Edit", priority=True),
            Binding("!", "shell_command", "Shell", priority=True),
            ("/", "start_search", "Search"),
            Binding("home", "go_first", "First", priority=True),
            Binding("end", "go_last", "Last", priority=True),
            Binding("enter", "enter_dir", "Enter", priority=True),
        ]

        CSS = """
        * {
            scrollbar-size: 1 1;
        }
        DualPanelScreen {
            align: center middle;
            background: transparent;
        }
        #dual-container {
            width: 100%;
            height: 100%;
            background: $background;
            border: none;
            padding: 0;
        }
        #panels {
            height: 1fr;
            background: $background;
        }
        .panel {
            width: 50%;
            height: 100%;
            border: round $border;
            background: $background;
            margin: 0 1;
            border-title-align: left;
            border-title-color: $text-muted;
            border-title-background: $background;
        }
        .panel:focus-within {
            border: round $primary;
            border-title-color: $primary;
            border-subtitle-color: $primary;
        }
        .panel-list {
            height: 1fr;
            background: $background;
        }
        #progress-container {
            height: 3;
            padding: 0 1;
            display: none;
            background: $background;
        }
        #progress-container.visible {
            display: block;
        }
        #help-bar {
            height: 1;
            background: $surface;
            color: $text-muted;
            text-align: center;
            padding: 0 1;
        }
        ListItem {
            padding: 0;
            background: $background;
        }
        ListItem.-highlight {
            background: $panel;
        }
        ListItem.-highlight > Static {
            background: transparent;
        }
        ListView:focus ListItem.-highlight {
            background: $primary 30%;
        }
        ListView:focus ListItem.-highlight > Static {
            background: transparent;
        }
        ListView {
            background: $background;
        }
        ProgressBar {
            background: $background;
        }
        ProgressBar > .bar--bar {
            color: $success;
        }
        """

        def __init__(self, start_path: Path = None):
            super().__init__()
            if DualPanelScreen._initial_start_path is None:
                DualPanelScreen._initial_start_path = start_path or Path.cwd()

            home_key = str(DualPanelScreen._initial_start_path)

            if DualPanelScreen._session_left_path is None:
                saved = load_session_paths(home_key)
                if saved.get("left"):
                    saved_left = Path(saved["left"])
                    if saved_left.exists():
                        DualPanelScreen._session_left_path = saved_left
                if saved.get("right"):
                    saved_right = Path(saved["right"])
                    if saved_right.exists():
                        DualPanelScreen._session_right_path = saved_right

            self.left_path = DualPanelScreen._session_left_path or start_path or Path.cwd()
            self.right_path = DualPanelScreen._session_right_path or Path.home()
            self.sort_left = DualPanelScreen._session_sort_left
            self.sort_right = DualPanelScreen._session_sort_right
            self.show_hidden = DualPanelScreen._session_show_hidden
            self.selected_left: set[Path] = set()
            self.selected_right: set[Path] = set()
            self.active_panel = "left"
            self.copying = False
            self.moving = False
            self._quick_select_mode = False
            self._quick_select_buffer = ""

        def compose(self) -> ComposeResult:
            container = Vertical(id="dual-container")
            with container:
                with Horizontal(id="panels"):
                    left_panel = Vertical(id="left-panel", classes="panel")
                    with left_panel:
                        yield PathBar(self.left_path, "left", "")
                        yield ListView(id="left-list", classes="panel-list")
                    right_panel = Vertical(id="right-panel", classes="panel")
                    with right_panel:
                        yield PathBar(self.right_path, "right", "")
                        yield ListView(id="right-list", classes="panel-list")
                with Vertical(id="progress-container"):
                    yield Static("", id="progress-text")
                    yield ProgressBar(id="progress-bar", total=100)
                yield Label(self._get_help_bar_text(), id="help-bar")

        HELP_SHORTCUTS = [
            # Navigation
            ("/", "search"),
            ("g", "jump"),
            ("h", "home"),
            ("i", "sync"),
            # Selection
            ("Space", "sel"),
            ("a", "all"),
            ("n", "qsel"),
            # File operations
            ("v", "view"),
            ("E", "edit"),
            ("c", "copy"),
            ("m", "move"),
            ("r", "ren"),
            ("d", "del"),
            ("p", "mkdir"),
            ("!", "shell"),
            # Display
            ("s", "sort"),
            ("t", "term"),
        ]

        def _get_help_bar_text(self, highlight_key: str = None) -> Text:
            """Generate help bar text with optional key highlighting."""
            # Show quick select mode buffer
            if self._quick_select_mode:
                text = Text()
                text.append("[n:select] ", style="bold yellow")
                text.append(self._quick_select_buffer or "_", style="bold reverse")
                text.append("  (type to match, Enter=confirm, Esc=cancel)", style="dim")
                return text
            
            text = Text()
            for i, (key, label) in enumerate(self.HELP_SHORTCUTS):
                if i > 0:
                    text.append("  ")
                if highlight_key and key.lower() == highlight_key.lower():
                    text.append(f"{key}:", style="bold reverse")
                    text.append(label, style="bold reverse")
                else:
                    text.append(f"{key}:", style="dim")
                    text.append(label)
            return text

        def _highlight_shortcut(self, key: str):
            """Highlight a shortcut key in the help bar temporarily."""
            try:
                help_bar = self.query_one("#help-bar", Label)
                help_bar.update(self._get_help_bar_text(key))
                self.set_timer(0.3, self._reset_help_bar)
            except Exception:
                pass

        def _reset_help_bar(self):
            """Reset help bar to normal state."""
            try:
                help_bar = self.query_one("#help-bar", Label)
                help_bar.update(self._get_help_bar_text())
            except Exception:
                pass

        def on_mount(self):
            self.refresh_panels()
            left_list = self.query_one("#left-list", ListView)
            right_list = self.query_one("#right-list", ListView)
            if left_list.children:
                max_left = len(left_list.children) - 1
                left_list.index = min(DualPanelScreen._session_left_index, max_left)
            if right_list.children:
                max_right = len(right_list.children) - 1
                right_list.index = min(DualPanelScreen._session_right_index, max_right)
            left_list.focus()
            self._update_panel_borders()

        def _update_panel_borders(self):
            left_panel = self.query_one("#left-panel", Vertical)
            right_panel = self.query_one("#right-panel", Vertical)
            if self.active_panel == "left":
                left_panel.border_subtitle = "● ACTIVE"
                right_panel.border_subtitle = ""
            else:
                left_panel.border_subtitle = ""
                right_panel.border_subtitle = "● ACTIVE"

        def on_path_segment_clicked(self, message: PathSegment.Clicked) -> None:
            if message.panel == "left":
                self.left_path = message.path
                self.selected_left.clear()
                DualPanelScreen._session_left_path = message.path
                self._refresh_single_panel("left")
            else:
                self.right_path = message.path
                self.selected_right.clear()
                DualPanelScreen._session_right_path = message.path
                self._refresh_single_panel("right")
            self._save_paths_to_config()

        def refresh_panels(self):
            self._refresh_panel("left", self.left_path, self.selected_left)
            self._refresh_panel("right", self.right_path, self.selected_right)

        def _refresh_panel(self, side: str, path: Path, selected: set):
            list_view = self.query_one(f"#{side}-list", ListView)
            panel = self.query_one(f"#{side}-panel", Vertical)

            sort_by_date = self.sort_left if side == "left" else self.sort_right
            sort_icon = "t" if sort_by_date else "n"

            try:
                path_bar = panel.query_one(PathBar)
                path_bar.update_path(path, sort_icon)
            except:
                pass

            list_view.clear()

            try:
                if path.parent != path:
                    list_view.append(FileItem(path.parent, is_selected=False, is_parent=True))

                if self.show_hidden:
                    all_items = list(path.iterdir())
                else:
                    all_items = [p for p in path.iterdir() if not p.name.startswith(".")]

                # Sort: dot directories first, then normal directories, then files
                if sort_by_date:
                    def sort_key(p):
                        is_dir = p.is_dir()
                        is_dot = p.name.startswith(".")
                        if is_dir:
                            group = 0 if is_dot else 1
                        else:
                            group = 2
                        try:
                            atime = p.stat().st_atime
                        except:
                            atime = 0
                        return (group, -atime)
                else:
                    def sort_key(p):
                        is_dir = p.is_dir()
                        is_dot = p.name.startswith(".")
                        if is_dir:
                            group = 0 if is_dot else 1
                        else:
                            group = 2
                        return (group, p.name.lower())

                items = sorted(all_items, key=sort_key)
                for item in items:
                    list_view.append(FileItem(item, item in selected))
            except PermissionError:
                pass

        def _refresh_single_panel(self, side: str):
            if side == "left":
                self._refresh_panel("left", self.left_path, self.selected_left)
            else:
                self._refresh_panel("right", self.right_path, self.selected_right)
            list_view = self.query_one(f"#{side}-list", ListView)
            self.set_timer(0.01, lambda: self._set_cursor(list_view))

        def _save_paths_to_config(self):
            home_key = str(DualPanelScreen._initial_start_path or Path.cwd())
            save_session_paths(home_key, self.left_path, self.right_path)

        def _set_cursor(self, list_view: ListView):
            if len(list_view.children) > 1:
                list_view.index = 1
            elif list_view.children:
                list_view.index = 0
            list_view.focus()

        def action_close(self):
            if not self.copying:
                DualPanelScreen._session_left_path = self.left_path
                DualPanelScreen._session_right_path = self.right_path
                DualPanelScreen._session_sort_left = self.sort_left
                DualPanelScreen._session_sort_right = self.sort_right
                left_list = self.query_one("#left-list", ListView)
                right_list = self.query_one("#right-list", ListView)
                DualPanelScreen._session_left_index = left_list.index if left_list.index is not None else 1
                DualPanelScreen._session_right_index = right_list.index if right_list.index is not None else 1
                home_key = str(DualPanelScreen._initial_start_path or Path.cwd())
                save_session_paths(home_key, self.left_path, self.right_path)
                self.dismiss()

        def action_cancel_or_close(self):
            if not self.copying:
                self.action_close()

        def action_start_search(self):
            self._highlight_shortcut("/")
            path = self.left_path if self.active_panel == "left" else self.right_path
            with self.app.suspend():
                if self.show_hidden:
                    cmd = f"ls -1a '{path}' | grep -v '^\\.$' | grep -v '^\\.\\.$' | fzf --prompt='Select: '"
                else:
                    cmd = f"ls -1 '{path}' | fzf --prompt='Select: '"
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=str(path))
                selected = result.stdout.strip()

            if selected:
                selected_path = path / selected
                if selected_path.is_dir():
                    if self.active_panel == "left":
                        self.left_path = selected_path
                        self.selected_left.clear()
                        DualPanelScreen._session_left_path = selected_path
                    else:
                        self.right_path = selected_path
                        self.selected_right.clear()
                        DualPanelScreen._session_right_path = selected_path
                    self._refresh_single_panel(self.active_panel)
                    self._save_paths_to_config()
                else:
                    list_view = self.query_one(f"#{self.active_panel}-list", ListView)
                    target_path = selected_path.resolve()
                    for i, child in enumerate(list_view.children):
                        if isinstance(child, FileItem) and not child.is_parent:
                            if child.path.resolve() == target_path:
                                list_view.index = i
                                child.scroll_visible()
                                break

            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            list_view.focus()

        def action_toggle_sort(self):
            self._highlight_shortcut("s")
            if self.active_panel == "left":
                self.sort_left = not self.sort_left
                DualPanelScreen._session_sort_left = self.sort_left
            else:
                self.sort_right = not self.sort_right
                DualPanelScreen._session_sort_right = self.sort_right
            self._refresh_single_panel(self.active_panel)

        def action_switch_panel(self):
            if self.active_panel == "left":
                self.active_panel = "right"
                self.query_one("#right-list", ListView).focus()
            else:
                self.active_panel = "left"
                self.query_one("#left-list", ListView).focus()
            self._update_panel_borders()

        def action_toggle_select(self):
            self._highlight_shortcut("space")
            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            selected = self.selected_left if self.active_panel == "left" else self.selected_right

            if list_view.highlighted_child and isinstance(list_view.highlighted_child, FileItem):
                item = list_view.highlighted_child
                if item.path.name:
                    if item.path in selected:
                        selected.discard(item.path)
                        item.update_selection(False)
                    else:
                        selected.add(item.path)
                        item.update_selection(True)
                    if list_view.index < len(list_view.children) - 1:
                        list_view.index += 1

        def action_enter_dir(self):
            # If in quick select mode, just exit the mode without entering directory
            if self._quick_select_mode:
                self._exit_quick_select()
                return
            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            if list_view.highlighted_child and isinstance(list_view.highlighted_child, FileItem):
                item = list_view.highlighted_child
                if item.path.is_dir():
                    if self.active_panel == "left":
                        self.left_path = item.path
                        self.selected_left.clear()
                        DualPanelScreen._session_left_path = item.path
                    else:
                        self.right_path = item.path
                        self.selected_right.clear()
                        DualPanelScreen._session_right_path = item.path
                    self._refresh_single_panel(self.active_panel)
                    self._save_paths_to_config()

        def on_list_view_selected(self, event: ListView.Selected):
            if isinstance(event.item, FileItem):
                item = event.item
                if item.path.is_dir():
                    list_id = event.list_view.id
                    if list_id == "left-list":
                        self.left_path = item.path
                        self.selected_left.clear()
                        self.active_panel = "left"
                        DualPanelScreen._session_left_path = item.path
                    else:
                        self.right_path = item.path
                        self.selected_right.clear()
                        self.active_panel = "right"
                        DualPanelScreen._session_right_path = item.path
                    self._refresh_single_panel(self.active_panel)
                    self._save_paths_to_config()
                    self._update_panel_borders()

        def action_go_up(self):
            # If in quick select mode, handle backspace for buffer
            if self._quick_select_mode:
                if self._quick_select_buffer:
                    self._quick_select_buffer = self._quick_select_buffer[:-1]
                    self._update_help_bar()
                    self._quick_select_match()
                return
            if self.active_panel == "left":
                if self.left_path.parent != self.left_path:
                    self.left_path = self.left_path.parent
                    self.selected_left.clear()
                    DualPanelScreen._session_left_path = self.left_path
            else:
                if self.right_path.parent != self.right_path:
                    self.right_path = self.right_path.parent
                    self.selected_right.clear()
                    DualPanelScreen._session_right_path = self.right_path
            self._refresh_single_panel(self.active_panel)
            self._save_paths_to_config()

        def action_go_home(self):
            self._highlight_shortcut("h")
            home_path = DualPanelScreen._initial_start_path or Path.cwd()
            if self.active_panel == "left":
                self.left_path = home_path
                self.selected_left.clear()
                DualPanelScreen._session_left_path = home_path
            else:
                self.right_path = home_path
                self.selected_right.clear()
                DualPanelScreen._session_right_path = home_path
            self._refresh_single_panel(self.active_panel)
            self._save_paths_to_config()
            self.notify(f"Home: {home_path}", timeout=1)

        def action_sync_panels(self):
            self._highlight_shortcut("i")
            if self.active_panel == "left":
                self.right_path = self.left_path
                self.selected_right.clear()
                DualPanelScreen._session_right_path = self.left_path
                self._refresh_single_panel("right")
            else:
                self.left_path = self.right_path
                self.selected_left.clear()
                DualPanelScreen._session_left_path = self.right_path
                self._refresh_single_panel("left")
            self._save_paths_to_config()
            self.notify("Synced panels", timeout=1)

        def action_select_all(self):
            self._highlight_shortcut("a")
            path = self.left_path if self.active_panel == "left" else self.right_path
            selected = self.selected_left if self.active_panel == "left" else self.selected_right
            try:
                all_items = {item for item in path.iterdir() if not item.name.startswith(".")}
                if all_items and all_items <= selected:
                    selected.clear()
                else:
                    selected.update(all_items)
            except:
                pass
            self._refresh_single_panel(self.active_panel)

        def action_toggle_position(self):
            self._highlight_shortcut("g")
            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            if list_view.children:
                current = list_view.index if list_view.index is not None else 0
                if current == 0:
                    list_view.index = len(list_view.children) - 1
                    list_view.scroll_end(animate=False)
                else:
                    list_view.index = 0
                    list_view.scroll_home(animate=False)
                list_view.focus()

        def action_quick_select(self) -> None:
            """Enter quick select mode - type to jump to matching files."""
            self._quick_select_mode = True
            self._quick_select_buffer = ""
            self._highlight_shortcut("n")
            self._update_help_bar()

        def _update_help_bar(self) -> None:
            """Update the help bar display."""
            try:
                help_bar = self.query_one("#help-bar", Label)
                help_bar.update(self._get_help_bar_text())
            except Exception:
                pass

        def _quick_select_match(self) -> None:
            """Find and move cursor to first entry matching the buffer."""
            if not self._quick_select_buffer:
                return
            
            search = self._quick_select_buffer.lower()
            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            
            for i, item in enumerate(list_view.children):
                if isinstance(item, FileItem) and item.path.name.lower().startswith(search):
                    list_view.index = i
                    # Scroll to make item visible
                    if hasattr(item, 'scroll_visible'):
                        item.scroll_visible()
                    return

        def _exit_quick_select(self) -> None:
            """Exit quick select mode."""
            self._quick_select_mode = False
            self._quick_select_buffer = ""
            self._update_help_bar()

        def on_key(self, event) -> None:
            """Handle key events for quick select mode."""
            if not self._quick_select_mode:
                return
            
            key = event.key
            
            # Prevent bindings from firing while typing in quick select
            event.stop()
            event.prevent_default()

            # Exit on Escape
            if key == "escape":
                self._exit_quick_select()
                return

            # Confirm on Enter
            if key == "enter":
                self._exit_quick_select()
                return

            # Backspace removes last character
            if key == "backspace":
                if self._quick_select_buffer:
                    self._quick_select_buffer = self._quick_select_buffer[:-1]
                    self._update_help_bar()
                    self._quick_select_match()
                return

            # Add printable characters to buffer
            if len(key) == 1 and key.isprintable():
                self._quick_select_buffer += key
                self._update_help_bar()
                self._quick_select_match()
                return

        def action_page_up(self):
            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            if list_view.children:
                current = list_view.index if list_view.index is not None else 0
                page_size = max(1, list_view.size.height - 2)
                list_view.index = max(0, current - page_size)
                list_view.focus()

        def action_page_down(self):
            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            if list_view.children:
                current = list_view.index if list_view.index is not None else 0
                page_size = max(1, list_view.size.height - 2)
                list_view.index = min(len(list_view.children) - 1, current + page_size)
                list_view.focus()

        def action_go_first(self):
            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            if list_view.children:
                list_view.index = 0
                list_view.scroll_home(animate=False)
                list_view.focus()

        def action_go_last(self):
            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            if list_view.children:
                list_view.index = len(list_view.children) - 1
                list_view.scroll_end(animate=False)
                list_view.focus()

        def action_copy_selected(self):
            self._highlight_shortcut("c")
            if self.copying:
                return

            if self.active_panel == "left":
                selected = self.selected_left.copy()
                dest_path = self.right_path
            else:
                selected = self.selected_right.copy()
                dest_path = self.left_path

            used_explicit_selection = bool(selected)

            if not selected:
                list_view = self.query_one(f"#{self.active_panel}-list", ListView)
                if list_view.highlighted_child and isinstance(list_view.highlighted_child, FileItem):
                    item = list_view.highlighted_child
                    if not item.is_parent:
                        selected = {item.path}

            if not selected:
                self.notify("No files to copy", timeout=2)
                return

            self.copying = True
            self._copy_used_explicit_selection = used_explicit_selection
            items = list(selected)
            total = len(items)

            progress_container = self.query_one("#progress-container")
            progress_container.add_class("visible")
            progress_bar = self.query_one("#progress-bar", ProgressBar)
            progress_text = self.query_one("#progress-text", Static)
            progress_text.update(f"Copying {total} item(s)...")
            progress_bar.update(progress=0)

            def do_copy():
                for i, src in enumerate(items):
                    try:
                        dest = dest_path / src.name
                        self.app.call_from_thread(progress_text.update, f"Copying: {src.name} ({i+1}/{total})")
                        self.app.call_from_thread(progress_bar.update, progress=int(((i + 0.5) / total) * 100))
                        if src.is_dir():
                            shutil.copytree(src, dest, dirs_exist_ok=True)
                        else:
                            shutil.copy2(src, dest)
                        self.app.call_from_thread(progress_bar.update, progress=int(((i + 1) / total) * 100))
                    except Exception as e:
                        self.app.call_from_thread(self.notify, f"Error copying {src.name}: {e}", timeout=5)
                self.app.call_from_thread(self._copy_complete)

            thread = threading.Thread(target=do_copy, daemon=True)
            thread.start()

        def _copy_complete(self):
            self.copying = False
            progress_bar = self.query_one("#progress-bar", ProgressBar)
            progress_text = self.query_one("#progress-text", Static)
            progress_container = self.query_one("#progress-container")

            # Save cursor positions before refresh
            left_list = self.query_one("#left-list", ListView)
            right_list = self.query_one("#right-list", ListView)
            left_index = left_list.index
            right_index = right_list.index

            progress_bar.update(progress=100)
            progress_text.update("Done!")
            self.notify("Copy complete!", timeout=2)

            if getattr(self, '_copy_used_explicit_selection', False):
                self.selected_left.clear()
                self.selected_right.clear()

            # Reset indexes before refresh to avoid stale index issues
            left_list.index = 0
            right_list.index = 0
            self.refresh_panels()

            # Restore cursor positions clamped to new list sizes
            if left_index is not None:
                new_left = min(left_index, len(left_list.children) - 1)
                if new_left >= 0:
                    left_list.index = new_left
            if right_index is not None:
                new_right = min(right_index, len(right_list.children) - 1)
                if new_right >= 0:
                    right_list.index = new_right

            self.set_timer(2, lambda: progress_container.remove_class("visible"))

        def action_move_selected(self):
            self._highlight_shortcut("m")
            if self.moving:
                return

            if self.active_panel == "left":
                selected = self.selected_left.copy()
                dest_path = self.right_path
            else:
                selected = self.selected_right.copy()
                dest_path = self.left_path

            used_explicit_selection = bool(selected)

            if not selected:
                list_view = self.query_one(f"#{self.active_panel}-list", ListView)
                if list_view.highlighted_child and isinstance(list_view.highlighted_child, FileItem):
                    item = list_view.highlighted_child
                    if not item.is_parent:
                        selected = {item.path}

            if not selected:
                self.notify("No files to move", timeout=2)
                return

            items = list(selected)
            count = len(items)
            if count == 1:
                message = f"Move '{items[0].name}' to {dest_path}?"
            else:
                message = f"Move {count} item(s) to {dest_path}?"

            def handle_confirm(confirmed: bool):
                if confirmed:
                    self.moving = True
                    self._move_used_explicit_selection = used_explicit_selection
                    total = len(items)

                    progress_container = self.query_one("#progress-container")
                    progress_container.add_class("visible")
                    progress_bar = self.query_one("#progress-bar", ProgressBar)
                    progress_text = self.query_one("#progress-text", Static)
                    progress_text.update(f"Moving {total} item(s)...")
                    progress_bar.update(progress=0)

                    def do_move():
                        for i, src in enumerate(items):
                            try:
                                dest = dest_path / src.name
                                self.app.call_from_thread(progress_text.update, f"Moving: {src.name} ({i+1}/{total})")
                                self.app.call_from_thread(progress_bar.update, progress=int(((i + 0.5) / total) * 100))
                                shutil.move(str(src), str(dest))
                                self.app.call_from_thread(progress_bar.update, progress=int(((i + 1) / total) * 100))
                            except Exception as e:
                                self.app.call_from_thread(self.notify, f"Error moving {src.name}: {e}", timeout=5)
                        self.app.call_from_thread(self._move_complete)

                    thread = threading.Thread(target=do_move, daemon=True)
                    thread.start()

            self.app.push_screen(ConfirmDialog("Move", message), handle_confirm)

        def _move_complete(self):
            self.moving = False
            progress_bar = self.query_one("#progress-bar", ProgressBar)
            progress_text = self.query_one("#progress-text", Static)
            progress_container = self.query_one("#progress-container")

            # Save cursor positions before refresh
            left_list = self.query_one("#left-list", ListView)
            right_list = self.query_one("#right-list", ListView)
            left_index = left_list.index
            right_index = right_list.index

            progress_bar.update(progress=100)
            progress_text.update("Done!")
            self.notify("Move complete!", timeout=2)

            if getattr(self, '_move_used_explicit_selection', False):
                self.selected_left.clear()
                self.selected_right.clear()

            # Reset indexes before refresh to avoid stale index on fewer items
            left_list.index = 0
            right_list.index = 0
            self.refresh_panels()

            # Restore cursor positions clamped to new list sizes
            if left_index is not None:
                new_left = min(left_index, len(left_list.children) - 1)
                if new_left >= 0:
                    left_list.index = new_left
            if right_index is not None:
                new_right = min(right_index, len(right_list.children) - 1)
                if new_right >= 0:
                    right_list.index = new_right

            self.set_timer(2, lambda: progress_container.remove_class("visible"))

        def action_rename(self):
            self._highlight_shortcut("r")
            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            if not list_view.highlighted_child:
                self.notify("No item to rename", timeout=2)
                return

            item = list_view.highlighted_child
            if not isinstance(item, FileItem) or item.is_parent:
                self.notify("Cannot rename this item", timeout=2)
                return

            path = item.path
            current_index = list_view.index

            def handle_rename(new_name: str | None):
                if new_name:
                    try:
                        new_path = path.parent / new_name
                        path.rename(new_path)
                        self.notify(f"Renamed to: {new_name}", timeout=2)
                        self._refresh_single_panel(self.active_panel)
                        # Restore cursor position
                        new_index = min(current_index, len(list_view.children) - 1)
                        if new_index >= 0:
                            list_view.index = new_index
                    except Exception as e:
                        self.notify(f"Error: {e}", timeout=3)

            self.app.push_screen(RenameDialog(path.name), handle_rename)

        def action_mkdir(self):
            self._highlight_shortcut("p")
            if self.active_panel == "left":
                parent_path = self.left_path
            else:
                parent_path = self.right_path

            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            current_index = list_view.index

            def handle_mkdir(name: str | None):
                if name:
                    try:
                        new_dir = parent_path / name
                        new_dir.mkdir(parents=True, exist_ok=False)
                        self.notify(f"Created: {name}", timeout=2)
                        self._refresh_single_panel(self.active_panel)
                        new_index = min(current_index, len(list_view.children) - 1)
                        if new_index >= 0:
                            list_view.index = new_index
                    except FileExistsError:
                        self.notify(f"Already exists: {name}", timeout=3)
                    except Exception as e:
                        self.notify(f"Error: {e}", timeout=3)

            self.app.push_screen(MkdirDialog(), handle_mkdir)

        def action_shell_command(self):
            self._highlight_shortcut("!")
            if self.active_panel == "left":
                cwd = self.left_path
            else:
                cwd = self.right_path

            def handle_shell(cmd: str | None):
                if cmd:
                    with self.app.suspend():
                        subprocess.run(cmd, shell=True, cwd=str(cwd))
                        input("\n[Press Enter to continue]")
                    self.refresh_panels()
                    self.notify(f"Executed: {cmd}", timeout=2)

            self.app.push_screen(ShellCommandDialog(), handle_shell)

        def action_delete(self):
            self._highlight_shortcut("d")
            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            current_index = list_view.index

            if self.active_panel == "left":
                selected = self.selected_left.copy()
            else:
                selected = self.selected_right.copy()

            used_explicit_selection = bool(selected)

            if not selected:
                if list_view.highlighted_child and isinstance(list_view.highlighted_child, FileItem):
                    item = list_view.highlighted_child
                    if not item.is_parent:
                        selected = {item.path}

            if not selected:
                self.notify("No files selected", timeout=2)
                return

            items = list(selected)
            count = len(items)
            message = f"Delete '{items[0].name}'?" if count == 1 else f"Delete {count} items?"

            def handle_confirm(confirmed: bool):
                if confirmed:
                    errors = []
                    for item_path in items:
                        try:
                            if item_path.is_dir():
                                shutil.rmtree(item_path)
                            else:
                                item_path.unlink()
                        except Exception as e:
                            errors.append(f"{item_path.name}: {e}")

                    if errors:
                        self.notify(f"Errors: {len(errors)}", timeout=3)
                    else:
                        self.notify(f"Deleted {count} item(s)", timeout=2)

                    if used_explicit_selection:
                        self.selected_left.clear()
                        self.selected_right.clear()
                    self._refresh_single_panel(self.active_panel)
                    # Restore cursor position
                    new_index = min(current_index, len(list_view.children) - 1)
                    if new_index >= 0:
                        list_view.index = new_index

            self.app.push_screen(ConfirmDialog("Delete", message), handle_confirm)

        def action_view_file(self):
            self._highlight_shortcut("v")
            for screen in self.app.screen_stack:
                if isinstance(screen, FileViewerScreen):
                    screen.dismiss()
                    return

            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            if not list_view.highlighted_child:
                self.notify("No file selected", timeout=2)
                return

            item = list_view.highlighted_child
            if not isinstance(item, FileItem) or item.is_parent:
                return

            if item.path.is_dir():
                self.notify("Cannot view directory", timeout=2)
                return

            self.app.push_screen(FileViewerScreen(item.path))

        def action_edit_nano(self):
            list_view = self.query_one(f"#{self.active_panel}-list", ListView)
            if not list_view.highlighted_child:
                self.notify("No file selected", timeout=2)
                return

            item = list_view.highlighted_child
            if not isinstance(item, FileItem) or item.is_parent:
                return

            if item.path.is_dir():
                self.notify("Cannot edit directory", timeout=2)
                return

            binary_extensions = {'.pdf', '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar',
                               '.exe', '.dll', '.so', '.dylib', '.bin', '.dat',
                               '.mp3', '.mp4', '.avi', '.mov', '.mkv', '.wav', '.flac',
                               '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
                               '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.tiff', '.tif'}
            if item.path.suffix.lower() in binary_extensions:
                self.notify("Cannot edit binary file", timeout=2)
                return

            with self.app.suspend():
                subprocess.run(["nano", str(item.path)])
            self.notify(f"Edited: {item.path.name}", timeout=1)

        def action_terminal(self):
            """Open terminal (handled by tmux C-t binding)."""
            pass


    # ═══════════════════════════════════════════════════════════════════════════════
    # Main Application
    # ═══════════════════════════════════════════════════════════════════════════════

    class LstimeApp(App):
        """TUI application for directory time listing."""

        CSS = """
        Screen {
            background: $surface;
        }

        * {
            scrollbar-size: 1 1;
        }

        Footer {
            dock: bottom;
            height: 1;
        }

        #status {
            dock: top;
            height: 1;
            background: $primary-darken-2;
            color: $text;
            padding: 0 1;
        }

        #main-container {
            height: 1fr;
        }

        #list-panel {
            width: 2fr;
            border: round $border;
            margin: 0 0 0 1;
            border-title-align: left;
            border-title-color: $text-muted;
            border-title-background: $surface;
        }

        #list-panel:focus-within {
            border: round $primary;
            border-title-color: $primary;
            border-subtitle-color: $primary;
        }

        DataTable {
            height: 1fr;
        }

        DataTable > .datatable--cursor {
            background: $secondary;
        }

        #preview-panel {
            width: 1fr;
            border: round $border;
            margin: 0 1 0 0;
            padding: 1 2;
            border-title-align: left;
            border-title-color: $text-muted;
            border-title-background: $surface;
        }

        #preview-panel:focus-within {
            border: round $primary;
            border-title-color: $primary;
            border-subtitle-color: $primary;
        }

        #preview-title {
            text-style: bold;
            color: $text;
            margin-bottom: 1;
        }

        #preview-content {
            color: $text-muted;
        }

        .preview-label {
            color: $text-muted;
        }

        .preview-value {
            color: $text;
        }

        FileViewer {
            background: $surface;
        }

        #file-content {
            background: $surface;
        }

        #md-content {
            background: $surface;
        }

        #help-bar {
            dock: bottom;
            height: 1;
            background: $surface;
            color: $text-muted;
            text-align: center;
            padding: 0 1;
        }
        """

        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("Q", "quit_cd", "Quit+CD"),
            Binding("t", "toggle_time", "Toggle Time"),
            Binding("c", "sort_created", "Created"),
            Binding("a", "sort_accessed", "Accessed"),
            Binding("r", "reverse", "Reverse"),
            Binding("h", "toggle_hidden", "Hidden"),
            Binding("y", "copy_path", "Copy Path"),
            Binding("e", "show_tree", "Tree"),
            Binding("[", "grow_preview", "Shrink"),
            Binding("]", "shrink_preview", "Grow"),
            Binding("f", "toggle_fullscreen", "Fullscreen"),
            Binding("g", "toggle_position", "g=jump"),
            Binding("n", "quick_select", "n=select"),
            Binding("m", "file_manager", "Manager"),
            Binding("v", "view_file", "View"),
            Binding("E", "edit_nano", "Edit"),
            Binding("ctrl+f", "fzf_files", "Find", priority=True),
            Binding("/", "fzf_grep", "Grep", priority=True),
            Binding("tab", "toggle_focus", "Switch"),
            Binding("enter", "enter_dir", "Enter", priority=True),
            Binding("backspace", "go_parent", "Parent"),
            Binding("d", "delete_item", "Delete"),
            Binding("R", "rename_item", "Rename"),
            Binding("o", "open_system", "Open"),
            Binding("!", "ai_shell", "AI Shell"),
            Binding("G", "git_status", "Git Status"),
            Binding("ctrl+r", "refresh", "Refresh"),
            Binding("home", "go_first", "First", priority=True),
            Binding("end", "go_last", "Last", priority=True),
        ]

        preview_width = reactive(30)
        fullscreen_panel = reactive(None)  # None, "list", or "preview"

        def __init__(self, path: Path = None):
            super().__init__()
            self.path = path or Path.cwd()
            self.entries: list[DirEntry] = []
            self._visible_entries: list[DirEntry] = []
            self.sort_by = "created"
            self.reverse_order = True
            self.show_hidden = False
            self._quick_select_mode = False
            self._quick_select_buffer = ""
            config = load_config()
            self.preview_width = config.get("preview_width", 30)
            self.show_hidden = config.get("show_hidden", False)

        def compose(self) -> ComposeResult:
            yield Static(id="status")
            with Horizontal(id="main-container"):
                list_panel = Vertical(id="list-panel")
                list_panel.border_title = "Files"
                with list_panel:
                    yield DataTable(id="file-table")
                preview_panel = Vertical(id="preview-panel")
                preview_panel.border_title = "Preview"
                with preview_panel:
                    yield FileViewer(id="file-viewer")
            yield Label(self._get_help_bar_text(), id="help-bar")

        HELP_SHORTCUTS = [
            ("^F", "find"),
            ("/", "grep"),
            ("!", "AI"),
            ("G", "git"),
            ("m", "manager"),
            ("v", "view"),
            ("E", "edit"),
            ("t", "time"),
            ("r", "rev"),
            ("h", "hidden"),
            ("f", "full"),
            ("g", "jump"),
            ("n", "sel"),
            ("d", "del"),
            ("R", "ren"),
            ("q", "quit"),
        ]

        def _get_help_bar_text(self, highlight_key: str = None) -> Text:
            """Generate help bar text with optional key highlighting."""
            # Show quick select mode buffer
            if self._quick_select_mode:
                text = Text()
                text.append("[n:select] ", style="bold yellow")
                text.append(self._quick_select_buffer or "_", style="bold reverse")
                text.append("  (type to match, Enter=confirm, Esc=cancel)", style="dim")
                return text
            
            text = Text()
            for i, (key, label) in enumerate(self.HELP_SHORTCUTS):
                if i > 0:
                    text.append("  ")
                if highlight_key and key.lower() == highlight_key.lower():
                    text.append(f"{key}:", style="bold reverse")
                    text.append(label, style="bold reverse")
                else:
                    text.append(f"{key}:", style="dim")
                    text.append(label)
            return text

        def _highlight_shortcut(self, key: str):
            """Highlight a shortcut key in the help bar temporarily."""
            try:
                help_bar = self.query_one("#help-bar", Label)
                help_bar.update(self._get_help_bar_text(key))
                self.set_timer(0.3, self._reset_help_bar)
            except Exception:
                pass

        def _reset_help_bar(self):
            """Reset help bar to normal state."""
            try:
                help_bar = self.query_one("#help-bar", Label)
                help_bar.update(self._get_help_bar_text())
            except Exception:
                pass

        def on_mount(self) -> None:
            self.load_entries()
            self.setup_table()
            self.refresh_table()
            self._apply_panel_widths()

        def on_descendant_focus(self, event) -> None:
            """Update panel border subtitles based on focus."""
            try:
                list_panel = self.query_one("#list-panel", Vertical)
                preview_panel = self.query_one("#preview-panel", Vertical)
            except Exception:
                return
            focused = self.focused
            if focused is None:
                return
            # Check if focused widget is inside list-panel or preview-panel
            node = focused
            in_list = False
            in_preview = False
            while node is not None:
                if node.id == "list-panel":
                    in_list = True
                    break
                if node.id == "preview-panel":
                    in_preview = True
                    break
                node = node.parent
            if in_list:
                list_panel.border_subtitle = "● ACTIVE"
                preview_panel.border_subtitle = ""
            elif in_preview:
                list_panel.border_subtitle = ""
                preview_panel.border_subtitle = "● ACTIVE"

        def load_entries(self) -> None:
            self.entries = get_dir_entries(self.path)

        def setup_table(self) -> None:
            table = self.query_one("#file-table", DataTable)
            table.cursor_type = "row"
            table.zebra_stripes = False
            table.add_column("Name", width=40, key="name")
            table.add_column("Time", width=14, key="time")

        def refresh_table(self) -> None:
            table = self.query_one("#file-table", DataTable)
            table.clear()

            entries = self.entries
            if not self.show_hidden:
                entries = [e for e in entries if not e.name.startswith('.')]

            # Sort: dot directories first, then normal directories, then files
            def sort_key(e):
                is_dot = e.name.startswith(".")
                if e.is_dir:
                    group = 0 if is_dot else 1
                else:
                    group = 2
                time_val = e.created if self.sort_by == "created" else e.accessed
                timestamp = time_val.timestamp()
                if self.reverse_order:
                    return (group, -timestamp)
                else:
                    return (group, timestamp)

            entries = sorted(entries, key=sort_key)

            self._visible_entries = entries

            for entry in entries:
                time_val = entry.created if self.sort_by == "created" else entry.accessed
                if entry.is_dir:
                    name = Text("/" + entry.name, style="bold cyan")
                else:
                    name = Text(entry.name)
                table.add_row(name, format_time(time_val))

            self.update_status()
            if self._visible_entries:
                self.update_preview(0)

        def update_status(self) -> None:
            status = self.query_one("#status", Static)
            sort_label = "Creation Time" if self.sort_by == "created" else "Access Time"
            order_label = "(newest first)" if self.reverse_order else "(oldest first)"
            hidden_label = "[hidden]" if self.show_hidden else ""

            visible = len([e for e in self.entries if self.show_hidden or not e.name.startswith('.')])
            total = len(self.entries)

            path_str = str(self.path)
            if len(path_str) > 40:
                path_str = "..." + path_str[-37:]

            # Show quick select mode indicator
            mode_label = "[n:select] " if self._quick_select_mode else ""

            status.update(f" {mode_label}{path_str}  |  {sort_label} {order_label}  |  {visible}/{total} {hidden_label}")

        def _apply_panel_widths(self) -> None:
            preview = self.query_one("#preview-panel", Vertical)
            list_panel = self.query_one("#list-panel", Vertical)
            if self.fullscreen_panel == "list":
                preview.styles.width = "0%"
                preview.styles.display = "none"
                list_panel.styles.display = "block"
                list_panel.styles.width = "100%"
            elif self.fullscreen_panel == "preview":
                list_panel.styles.width = "0%"
                list_panel.styles.display = "none"
                preview.styles.display = "block"
                preview.styles.width = "100%"
            else:
                preview.styles.display = "block"
                list_panel.styles.display = "block"
                preview.styles.width = f"{self.preview_width}%"
                list_panel.styles.width = f"{100 - self.preview_width}%"

        def _save_config(self) -> None:
            config = load_config()
            config["preview_width"] = self.preview_width
            config["show_hidden"] = self.show_hidden
            save_config(config)

        def action_toggle_time(self) -> None:
            self._highlight_shortcut("t")
            self.sort_by = "accessed" if self.sort_by == "created" else "created"
            self.refresh_table()

        def action_sort_created(self) -> None:
            self.sort_by = "created"
            self.refresh_table()

        def action_sort_accessed(self) -> None:
            self.sort_by = "accessed"
            self.refresh_table()

        def action_reverse(self) -> None:
            self._highlight_shortcut("r")
            self.reverse_order = not self.reverse_order
            self.refresh_table()

        def action_toggle_hidden(self) -> None:
            self._highlight_shortcut("h")
            self.show_hidden = not self.show_hidden
            self._save_config()
            self.refresh_table()

        def action_copy_path(self) -> None:
            table = self.query_one("#file-table", DataTable)
            if table.cursor_row is not None and self._visible_entries:
                try:
                    entry = self._visible_entries[table.cursor_row]
                    full_path = str(entry.path.absolute())
                    subprocess.run(["pbcopy"], input=full_path.encode(), check=True)
                    self.notify(f"Copied: {full_path}")
                except (IndexError, subprocess.CalledProcessError):
                    self.notify("Failed to copy path", severity="error")

        def action_show_tree(self) -> None:
            table = self.query_one("#file-table", DataTable)
            if table.cursor_row is not None and self._visible_entries:
                try:
                    entry = self._visible_entries[table.cursor_row]
                    if entry.is_dir:
                        viewer = self.query_one("#file-viewer", FileViewer)
                        content = self._preview_tree(entry.path)
                        static = viewer.query_one("#file-content", Static)
                        static.display = True
                        viewer.query_one("#md-content").display = False
                        static.update(content)
                    else:
                        self.notify("Not a directory", severity="warning")
                except IndexError:
                    pass

        def action_shrink_preview(self) -> None:
            if self.preview_width > 10:
                self.preview_width -= 5
                self._apply_panel_widths()
                self._save_config()

        def action_grow_preview(self) -> None:
            if self.preview_width < 70:
                self.preview_width += 5
                self._apply_panel_widths()
                self._save_config()

        def action_toggle_fullscreen(self) -> None:
            self._highlight_shortcut("f")
            table = self.query_one("#file-table", DataTable)
            viewer = self.query_one("#file-viewer", FileViewer)

            # Determine which panel is active
            if table.has_focus:
                active = "list"
            elif viewer.has_focus:
                active = "preview"
            else:
                active = "list"  # default to list

            # Toggle fullscreen for the active panel
            if self.fullscreen_panel == active:
                self.fullscreen_panel = None
                self.notify("Normal view", timeout=1)
            else:
                self.fullscreen_panel = active
                self.notify(f"Fullscreen: {active} (f to restore)", timeout=1)

            self._apply_panel_widths()

        def action_toggle_position(self) -> None:
            self._highlight_shortcut("g")
            table = self.query_one("#file-table", DataTable)
            if self._visible_entries:
                current = table.cursor_row if table.cursor_row is not None else 0
                if current == 0:
                    table.move_cursor(row=len(self._visible_entries) - 1)
                else:
                    table.move_cursor(row=0)

        def action_quick_select(self) -> None:
            """Enter quick select mode - type to jump to matching files."""
            self._quick_select_mode = True
            self._quick_select_buffer = ""
            self._highlight_shortcut("n")
            self._update_help_bar()

        def _update_help_bar(self) -> None:
            """Update the help bar and status bar display."""
            try:
                help_bar = self.query_one("#help-bar", Label)
                help_bar.update(self._get_help_bar_text())
                self.update_status()
            except Exception:
                pass

        def _quick_select_match(self) -> None:
            """Find and move cursor to first entry matching the buffer."""
            if not self._quick_select_buffer or not self._visible_entries:
                return
            
            search = self._quick_select_buffer.lower()
            table = self.query_one("#file-table", DataTable)
            
            for i, entry in enumerate(self._visible_entries):
                if entry.name.lower().startswith(search):
                    table.move_cursor(row=i)
                    return

        def _exit_quick_select(self) -> None:
            """Exit quick select mode."""
            self._quick_select_mode = False
            self._quick_select_buffer = ""
            self._update_help_bar()

        def on_key(self, event) -> None:
            """Handle key events for quick select mode."""
            if not self._quick_select_mode:
                return
            
            key = event.key
            
            # Prevent bindings from firing while typing in quick select
            event.stop()
            event.prevent_default()

            # Exit on Escape
            if key == "escape":
                self._exit_quick_select()
                return

            # Confirm on Enter
            if key == "enter":
                self._exit_quick_select()
                return

            # Backspace removes last character
            if key == "backspace":
                if self._quick_select_buffer:
                    self._quick_select_buffer = self._quick_select_buffer[:-1]
                    self._update_help_bar()
                    self._quick_select_match()
                return

            # Add printable characters to buffer
            if len(key) == 1 and key.isprintable():
                self._quick_select_buffer += key
                self._update_help_bar()
                self._quick_select_match()
                return

        def action_go_first(self) -> None:
            if isinstance(self.screen, DualPanelScreen):
                self.screen.action_go_first()
                return
            table = self.query_one("#file-table", DataTable)
            if self._visible_entries:
                table.move_cursor(row=0)

        def action_go_last(self) -> None:
            if isinstance(self.screen, DualPanelScreen):
                self.screen.action_go_last()
                return
            table = self.query_one("#file-table", DataTable)
            if self._visible_entries:
                table.move_cursor(row=len(self._visible_entries) - 1)

        def action_toggle_focus(self) -> None:
            table = self.query_one("#file-table", DataTable)
            viewer = self.query_one("#file-viewer", FileViewer)
            if table.has_focus:
                viewer.focus()
            else:
                table.focus()

        def action_file_manager(self) -> None:
            self._highlight_shortcut("m")
            self.push_screen(DualPanelScreen(self.path))

        def action_view_file(self) -> None:
            self._highlight_shortcut("v")
            table = self.query_one("#file-table", DataTable)
            if table.cursor_row is not None and self._visible_entries:
                entry = self._visible_entries[table.cursor_row]
                if not entry.is_dir:
                    self.push_screen(FileViewerScreen(entry.path))
                else:
                    self.notify("Cannot view directory", timeout=2)

        def action_edit_nano(self) -> None:
            table = self.query_one("#file-table", DataTable)
            if table.cursor_row is not None and self._visible_entries:
                entry = self._visible_entries[table.cursor_row]
                if entry.is_dir:
                    self.notify("Cannot edit directory", timeout=2)
                    return
                binary_extensions = {'.pdf', '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar',
                                   '.exe', '.dll', '.so', '.dylib', '.bin', '.dat',
                                   '.mp3', '.mp4', '.avi', '.mov', '.mkv', '.wav', '.flac',
                                   '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
                                   '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.tiff', '.tif'}
                if entry.path.suffix.lower() in binary_extensions:
                    self.notify("Cannot edit binary file", timeout=2)
                    return
                with self.suspend():
                    subprocess.run(["nano", str(entry.path)])
                self.update_preview(table.cursor_row)
                self.notify(f"Edited: {entry.name}", timeout=1)

        def action_fzf_files(self) -> None:
            self._highlight_shortcut("^F")
            with self.suspend():
                fd_check = subprocess.run(["which", "fd"], capture_output=True)
                if fd_check.returncode == 0:
                    cmd = f"fd --type f --hidden -E .git -E .venv -E node_modules -E __pycache__ . '{self.path}' 2>/dev/null | fzf --preview 'head -100 {{}}'"
                else:
                    cmd = f"find '{self.path}' -type f 2>/dev/null | fzf --preview 'head -100 {{}}'"
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                selected = result.stdout.strip()

            if selected:
                path = Path(selected).resolve()
                if path.is_file():
                    # Navigate to parent directory and highlight file
                    if path.parent != self.path:
                        self.path = path.parent
                        self.load_entries()
                        self.refresh_table()
                    # Find and select the file
                    for i, entry in enumerate(self._visible_entries):
                        if entry.path.resolve() == path:
                            table = self.query_one("#file-table", DataTable)
                            table.move_cursor(row=i)
                            break
                    self.query_one("#file-viewer", FileViewer).load_file(path)
                    self.notify(f"Opened: {path.name}", timeout=1)

        def action_fzf_grep(self) -> None:
            self._highlight_shortcut("/")
            with self.suspend():
                result = subprocess.run(
                    f'rg -n --color=always "" "{self.path}" 2>/dev/null | fzf --ansi --preview "echo {{}} | cut -d: -f1 | xargs head -100"',
                    shell=True, capture_output=True, text=True
                )
                selected = result.stdout.strip()

            if selected:
                parts = selected.split(":", 2)
                if len(parts) >= 2:
                    file_path = Path(parts[0]).resolve()
                    if file_path.is_file():
                        if file_path.parent != self.path:
                            self.path = file_path.parent
                            self.load_entries()
                            self.refresh_table()
                        for i, entry in enumerate(self._visible_entries):
                            if entry.path.resolve() == file_path:
                                table = self.query_one("#file-table", DataTable)
                                table.move_cursor(row=i)
                                break
                        self.query_one("#file-viewer", FileViewer).load_file(file_path)
                        self.notify(f"Opened: {file_path.name}:{parts[1]}", timeout=1)


        def action_enter_dir(self) -> None:
            # If in quick select mode, just exit the mode without entering directory
            if self._quick_select_mode:
                self._exit_quick_select()
                return
            # If DualPanelScreen is active, delegate to it
            if isinstance(self.screen, DualPanelScreen):
                self.screen.action_enter_dir()
                return
            table = self.query_one("#file-table", DataTable)
            if table.cursor_row is not None and self._visible_entries:
                entry = self._visible_entries[table.cursor_row]
                if entry.is_dir:
                    if not entry.path.exists():
                        self.notify(f"Directory no longer exists: {entry.name}", timeout=2)
                        self.load_entries()
                        self.refresh_table()
                        return
                    self.path = entry.path
                    self.load_entries()
                    self.refresh_table()
                    self.notify(f"/{entry.name}", timeout=1)

        def action_go_parent(self) -> None:
            # If in quick select mode, handle backspace for buffer
            if self._quick_select_mode:
                if self._quick_select_buffer:
                    self._quick_select_buffer = self._quick_select_buffer[:-1]
                    self._update_help_bar()
                    self._quick_select_match()
                return
            if self.path.parent != self.path:
                old_path = self.path
                self.path = self.path.parent
                self.load_entries()
                self.refresh_table()
                # Try to select the old directory
                for i, entry in enumerate(self._visible_entries):
                    if entry.path == old_path:
                        table = self.query_one("#file-table", DataTable)
                        table.move_cursor(row=i)
                        break
                self.notify(f"/{self.path.name or self.path}", timeout=1)

        def action_delete_item(self) -> None:
            self._highlight_shortcut("d")
            table = self.query_one("#file-table", DataTable)
            if table.cursor_row is None or not self._visible_entries:
                self.notify("No item selected", timeout=2)
                return

            entry = self._visible_entries[table.cursor_row]
            message = f"Delete '{entry.name}'?"

            current_row = table.cursor_row
            def handle_confirm(confirmed: bool):
                if confirmed:
                    try:
                        if entry.is_dir:
                            shutil.rmtree(entry.path)
                        else:
                            entry.path.unlink()
                        self.notify(f"Deleted: {entry.name}", timeout=2)
                        self.load_entries()
                        self.refresh_table()
                        # Restore cursor position
                        new_row = min(current_row, len(self._visible_entries) - 1)
                        if new_row >= 0:
                            table.move_cursor(row=new_row)
                    except Exception as e:
                        self.notify(f"Error: {e}", timeout=3)

            self.push_screen(ConfirmDialog("Delete", message), handle_confirm)

        def action_rename_item(self) -> None:
            self._highlight_shortcut("R")
            table = self.query_one("#file-table", DataTable)
            if table.cursor_row is None or not self._visible_entries:
                self.notify("No item selected", timeout=2)
                return

            entry = self._visible_entries[table.cursor_row]
            current_row = table.cursor_row

            def handle_rename(new_name: str | None):
                if new_name:
                    try:
                        new_path = entry.path.parent / new_name
                        entry.path.rename(new_path)
                        self.notify(f"Renamed to: {new_name}", timeout=2)
                        self.load_entries()
                        self.refresh_table()
                        # Restore cursor position
                        new_row = min(current_row, len(self._visible_entries) - 1)
                        if new_row >= 0:
                            table.move_cursor(row=new_row)
                    except Exception as e:
                        self.notify(f"Error: {e}", timeout=3)

            self.push_screen(RenameDialog(entry.name), handle_rename)

        def action_open_system(self) -> None:
            table = self.query_one("#file-table", DataTable)
            if table.cursor_row is not None and self._visible_entries:
                entry = self._visible_entries[table.cursor_row]
                subprocess.run(["open", str(entry.path)])
                self.notify(f"Opened: {entry.name}", timeout=1)

        def action_ai_shell(self) -> None:
            self._highlight_shortcut("!")
            def handle_result(result: Path | None):
                if result:
                    self.notify(f"Saved: {result.name}", timeout=2)
                    self.load_entries()
                    self.refresh_table()
                    # Select the new file
                    for i, entry in enumerate(self._visible_entries):
                        if entry.path == result:
                            table = self.query_one("#file-table", DataTable)
                            table.move_cursor(row=i)
                            break

            self.push_screen(AIShellDialog(self.path), handle_result)


        def action_git_status(self) -> None:
            self._highlight_shortcut("G")
            def handle_result(result: Path | None):
                if result:
                    # Navigate to the selected repo
                    if result.is_dir():
                        self.path = result
                        self.load_entries()
                        self.refresh_table()
                        self.update_status()
                        self.notify(f"Opened: {result.name}", timeout=1)

            self.push_screen(GitStatusScreen(self.path), handle_result)

        def action_refresh(self) -> None:
            table = self.query_one("#file-table", DataTable)
            current_row = table.cursor_row
            self.load_entries()
            self.refresh_table()
            # Restore cursor position if possible
            if current_row is not None and self._visible_entries:
                table.move_cursor(row=min(current_row, len(self._visible_entries) - 1))
            self.notify("Refreshed", timeout=1)

        def action_terminal(self) -> None:
            """Open terminal (handled by tmux C-t binding)."""
            pass

        def action_quit_cd(self) -> None:
            """Quit and write current directory to temp file for shell to cd."""
            _cleanup_tmux_toggle()
            try:
                LASTDIR_FILE.write_text(str(self.path))
            except OSError:
                pass
            self.exit()

        def action_quit(self) -> None:
            """Quit and save current directory for shell cd."""
            _cleanup_tmux_toggle()
            try:
                LASTDIR_FILE.write_text(str(self.path))
            except OSError:
                pass
            self.exit()

        def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
            self.update_preview(event.cursor_row)

        def update_preview(self, row_index: int) -> None:
            viewer = self.query_one("#file-viewer", FileViewer)

            if row_index is None or not self._visible_entries or row_index >= len(self._visible_entries):
                viewer.clear()
                return

            entry = self._visible_entries[row_index]

            if entry.is_dir:
                content = self._preview_tree(entry.path)
                static = viewer.query_one("#file-content", Static)
                static.display = True
                viewer.query_one("#md-content").display = False
                static.update(content)
                viewer.scroll_home()
            else:
                viewer.load_file(entry.path)

        def _preview_tree(self, path: Path, max_depth: int = 3, max_items: int = 100) -> str:
            lines = [f"[bold magenta]/{path.name}[/]", ""]
            count = [0]
            tree_lines = []

            def add_tree(p: Path, prefix: str = "", depth: int = 0):
                if count[0] >= max_items or depth > max_depth:
                    return
                try:
                    entries = get_dir_entries(p)
                    if not self.show_hidden:
                        entries = [e for e in entries if not e.name.startswith('.')]
                    # Sort with directories first, then files, each group sorted by time
                    if self.sort_by == "created":
                        entries = sorted(entries, key=lambda e: e.created, reverse=self.reverse_order)
                    else:
                        entries = sorted(entries, key=lambda e: e.accessed, reverse=self.reverse_order)
                    # Stable sort: directories first, preserving time order within each group
                    entries = sorted(entries, key=lambda e: not e.is_dir)

                    for i, entry in enumerate(entries):
                        if count[0] >= max_items:
                            tree_lines.append((f"{prefix}[dim]... truncated[/]", "", False, ""))
                            return
                        is_last = i == len(entries) - 1
                        connector = "└── " if is_last else "├── "
                        time_val = entry.created if self.sort_by == "created" else entry.accessed
                        time_str = format_time(time_val)
                        name = ("/" if entry.is_dir else "") + entry.name
                        tree_lines.append((f"{prefix}{connector}", name, entry.is_dir, time_str))
                        count[0] += 1
                        if entry.is_dir:
                            next_prefix = prefix + ("    " if is_last else "│   ")
                            add_tree(entry.path, next_prefix, depth + 1)
                except PermissionError:
                    tree_lines.append((f"{prefix}[red]Permission denied[/]", "", False, ""))

            add_tree(path)

            max_name = 25
            for item in tree_lines:
                if len(item) == 4:
                    _, name, _, _ = item
                    if len(name) > max_name:
                        max_name = min(len(name), 30)

            for item in tree_lines:
                if len(item) == 4:
                    prefix, name, is_dir, time_str = item
                    display_name = name[:max_name-3] + "..." if len(name) > max_name else name
                    color = "cyan" if is_dir else "white"
                    padding = max_name - len(display_name)
                    lines.append(f"{prefix}[{color}]{display_name}[/]{' ' * padding} [dim]{time_str:>12}[/]")
                else:
                    lines.append(item[0])

            if count[0] >= max_items:
                lines.append(f"\n[dim]Showing {max_items} items (truncated)[/]")

            return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Rich Fallback Implementation
# ═══════════════════════════════════════════════════════════════════════════════

def rich_display(path: Path = None, sort_by: str = "created", reverse: bool = True, show_hidden: bool = False):
    """Display directory listing using Rich (non-interactive fallback)."""
    if not HAS_RICH:
        print("Error: Neither 'textual' nor 'rich' is installed.")
        print("Install with: pip install textual rich")
        sys.exit(1)

    console = Console()
    path = path or Path.cwd()
    entries = get_dir_entries(path)

    if not show_hidden:
        entries = [e for e in entries if not e.name.startswith('.')]

    if sort_by == "created":
        entries = sorted(entries, key=lambda e: e.created, reverse=reverse)
        time_label = "Created"
    else:
        entries = sorted(entries, key=lambda e: e.accessed, reverse=reverse)
        time_label = "Accessed"

    table = Table(
        box=box.ROUNDED,
        header_style="bold cyan",
        border_style="dim",
        show_lines=False,
        padding=(0, 1),
    )

    table.add_column("Name", style="white", no_wrap=True, min_width=30)
    table.add_column(time_label, style="green", justify="right")
    table.add_column("Size", style="yellow", justify="right")

    for entry in entries:
        time_val = entry.created if sort_by == "created" else entry.accessed

        if entry.is_dir:
            name = f"[bold blue]{entry.name}/[/]"
            size = "-"
        else:
            name = entry.name
            size = format_size(entry.size)

        table.add_row(name, format_time(time_val), size)

    order_str = "newest first" if reverse else "oldest first"
    console.print()
    console.print(Panel(
        f"[bold]{path}[/]\n[dim]{len(entries)} items | Sorted by {time_label.lower()} ({order_str})[/]",
        title="[cyan]lstime[/]",
        border_style="cyan",
        padding=(0, 1),
    ))
    console.print(table)
    console.print("[dim]Tip: Run with --tui for interactive mode, or use -a for access time[/]")


# ═══════════════════════════════════════════════════════════════════════════════
# Plain Text Fallback
# ═══════════════════════════════════════════════════════════════════════════════

def plain_display(path: Path = None, sort_by: str = "created", reverse: bool = True, show_hidden: bool = False):
    """Plain text display without any dependencies."""
    path = path or Path.cwd()
    entries = get_dir_entries(path)

    if not show_hidden:
        entries = [e for e in entries if not e.name.startswith('.')]

    if sort_by == "created":
        entries = sorted(entries, key=lambda e: e.created, reverse=reverse)
        time_label = "Created"
    else:
        entries = sorted(entries, key=lambda e: e.accessed, reverse=reverse)
        time_label = "Accessed"

    order_str = "newest first" if reverse else "oldest first"
    print(f"\n  lstime - {path}")
    print(f"  {len(entries)} items | Sorted by {time_label.lower()} ({order_str})")
    print("  " + "=" * 60)
    print(f"  {'Name':<35} {time_label:>15} {'Size':>8}")
    print("  " + "-" * 60)

    for entry in entries:
        time_val = entry.created if sort_by == "created" else entry.accessed
        name = entry.name + ("/" if entry.is_dir else "")
        if len(name) > 34:
            name = name[:31] + "..."

        size = "-" if entry.is_dir else format_size(entry.size)
        print(f"  {name:<35} {format_time(time_val):>15} {size:>8}")

    print()


# ═══════════════════════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

def print_help():
    """Print help message."""
    help_text = """
lstime - Directory Time Listing Tool

Usage: lstime [OPTIONS] [PATH]

Options:
  -c, --created     Sort by creation time (default)
  -a, --accessed    Sort by last access time
  -r, --reverse     Reverse sort order (oldest first)
  -H, --hidden      Show hidden files
  --tui             Force interactive TUI mode
  --no-tui          Force non-interactive mode
  --no-tmux         Run without tmux (no C-t terminal toggle)
  -h, --help        Show this help message

Interactive TUI Shortcuts:
  t                 Toggle between creation/access time
  c                 Sort by creation time
  a                 Sort by access time
  r                 Reverse sort order
  h                 Toggle hidden files
  y                 Copy selected path to clipboard
  e                 Show recursive tree in preview
  [                 Shrink preview panel
  ]                 Grow preview panel
  f                 Toggle fullscreen (hide preview)
  g                 Toggle first/last position
  m                 Open dual-panel file manager
  v                 View file in modal
  Ctrl+F            Fuzzy file search (fzf)
  /                 Grep search (rg + fzf)
  Tab               Switch focus (list/preview)
  Enter             Navigate into directory
  Backspace         Go to parent directory
  d                 Delete file/directory
  R                 Rename file/directory
  o                 Open with system app
  q                 Quit

File Manager (m) Shortcuts:
  Tab               Switch panels
  Space             Toggle selection
  c                 Copy to other panel
  r                 Rename
  d                 Delete
  a                 Select all/none
  s                 Toggle sort (name/time)
  h                 Go to home (start path)
  i                 Sync panels
  v                 View file
  /                 Search (fzf)
  g                 Toggle first/last
  q/Esc             Close file manager

Examples:
  lstime                    # List current dir by creation time
  lstime -a                 # List by access time
  lstime --tui ~/Documents  # Interactive mode for Documents
  lstime -rH                # Oldest first, show hidden
"""
    print(help_text)


def main():
    """Main entry point."""
    args = sys.argv[1:]

    path = None
    sort_by = "created"
    reverse = True
    show_hidden = False
    force_tui = None
    no_tmux = False

    i = 0
    while i < len(args):
        arg = args[i]

        if arg in ("-h", "--help"):
            print_help()
            sys.exit(0)
        elif arg in ("-c", "--created"):
            sort_by = "created"
        elif arg in ("-a", "--accessed"):
            sort_by = "accessed"
        elif arg in ("-r", "--reverse"):
            reverse = not reverse
        elif arg in ("-H", "--hidden"):
            show_hidden = True
        elif arg == "--tui":
            force_tui = True
        elif arg == "--no-tui":
            force_tui = False
        elif arg == "--no-tmux":
            no_tmux = True
        elif not arg.startswith("-"):
            path = Path(arg).expanduser().resolve()
        else:
            print(f"Unknown option: {arg}")
            print("Use --help for usage information")
            sys.exit(1)

        i += 1

    if path and not path.exists():
        print(f"Error: Path does not exist: {path}")
        sys.exit(1)

    use_tui = force_tui if force_tui is not None else (HAS_TEXTUAL and sys.stdout.isatty())

    if use_tui and HAS_TEXTUAL:
        target = str(path or Path.cwd())

        # Launch inside dedicated tmux unless:
        # - --no-tmux flag passed
        # - already inside our dedicated tmux socket
        # - already inside another tmux session
        if (not no_tmux
                and shutil.which("tmux")
                and not os.environ.get("_LST_INSIDE_TMUX")
                and not os.environ.get("TMUX")):
            _lst_tmux_launch([sys.executable] + sys.argv, target)
            # execvp in _lst_tmux_launch means we never reach here

        app = LstimeApp(path)
        app.sort_by = sort_by
        app.reverse_order = reverse
        app.show_hidden = show_hidden
        app.run()
    elif HAS_RICH:
        rich_display(path, sort_by, reverse, show_hidden)
    else:
        plain_display(path, sort_by, reverse, show_hidden)


if __name__ == "__main__":
    main()
