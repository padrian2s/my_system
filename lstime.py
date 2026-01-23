#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=0.40.0",
#     "rich>=13.0.0",
#     "anthropic>=0.40.0",
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
  Ctrl+R - Refresh directory listing
  q - Quit
  Q - Quit and sync shell to current directory
"""

import json
import os
import shutil
import subprocess
import sys
import stat
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
    from rich.text import Text
    from rich.syntax import Syntax
    from rich.console import Group
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
            Binding("enter", "submit", "Submit", priority=True),
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
            self.action_submit()

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
            dialog.border_subtitle = "y:Yes  n:No  Esc:Cancel"
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
            Binding("enter", "submit", "Submit", priority=True),
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
            dialog.border_subtitle = "Enter:Confirm  Esc:Cancel"
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
            self.action_submit()

        def action_submit(self):
            input_widget = self.query_one("#rename-input", Input)
            new_name = input_widget.value.strip()
            if new_name and new_name != self.current_name:
                self.dismiss(new_name)
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
            ("ctrl+y", "copy_clipboard", "Copy"),
            Binding("enter", "submit", "Submit", priority=True),
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
            dialog.border_subtitle = "Enter:Generate  ^S:Save  ^Y:Copy  Esc:Cancel"
            with dialog:
                yield Input(placeholder="Describe what you want the shell script to do...", id="prompt-input")
                with VerticalScroll(id="response-area"):
                    yield Static("", id="response-text")
                yield Static("Ready. Enter your prompt and press Enter.", id="status-bar")

        def on_mount(self):
            self.query_one("#prompt-input", Input).focus()

        def on_input_submitted(self, event: Input.Submitted):
            self.action_submit()

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
            container.border_subtitle = "v/q/Esc:Close"
            with container:
                yield FileViewer(id="viewer-content")

        def on_mount(self):
            viewer = self.query_one("#viewer-content", FileViewer)
            viewer.load_file(self.file_path)

        def action_close(self):
            self.dismiss()


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
            ("a", "select_all", "All"),
            ("s", "toggle_sort", "Sort"),
            ("r", "rename", "Rename"),
            ("d", "delete", "Delete"),
            Binding("g", "toggle_position", "g=jump"),
            ("pageup", "page_up", "PgUp"),
            ("pagedown", "page_down", "PgDn"),
            ("h", "go_home", "Home"),
            ("i", "sync_panels", "Sync"),
            ("v", "view_file", "View"),
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
                yield Label("/search  Space:sel  v:view  c:copy  r:ren  d:del  a:all  s:sort  h:home  i:sync  g:jump", id="help-bar")

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

                if sort_by_date:
                    def sort_key(p):
                        try:
                            atime = p.stat().st_atime
                        except:
                            atime = 0
                        return (not p.is_dir(), -atime)
                else:
                    def sort_key(p):
                        return (not p.is_dir(), p.name.lower())

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

        def action_toggle_select(self):
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

        def action_go_up(self):
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

            self.refresh_panels()

            # Restore cursor positions
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

        def action_delete(self):
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
            Binding("m", "file_manager", "Manager"),
            Binding("v", "view_file", "View"),
            Binding("ctrl+f", "fzf_files", "Find", priority=True),
            Binding("/", "fzf_grep", "Grep", priority=True),
            Binding("tab", "toggle_focus", "Switch"),
            Binding("enter", "enter_dir", "Enter", priority=True),
            Binding("backspace", "go_parent", "Parent"),
            Binding("d", "delete_item", "Delete"),
            Binding("R", "rename_item", "Rename"),
            Binding("o", "open_system", "Open"),
            Binding("!", "ai_shell", "AI Shell"),
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
            yield Label("^F:find  /:grep  !:AI  m:manager  v:view  t:time  r:rev  h:hidden  f:full  g:jump  d:del  R:ren  q:quit", id="help-bar")

        def on_mount(self) -> None:
            self.load_entries()
            self.setup_table()
            self.refresh_table()
            self._apply_panel_widths()

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

            # Sort with directories first, then files, each group sorted by time
            if self.sort_by == "created":
                entries = sorted(entries, key=lambda e: (not e.is_dir, e.created), reverse=self.reverse_order)
            else:
                entries = sorted(entries, key=lambda e: (not e.is_dir, e.accessed), reverse=self.reverse_order)
            # Re-sort to ensure directories always come first regardless of reverse order
            entries = sorted(entries, key=lambda e: not e.is_dir)

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

            status.update(f" {path_str}  |  {sort_label} {order_label}  |  {visible}/{total} {hidden_label}")

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
            self.sort_by = "accessed" if self.sort_by == "created" else "created"
            self.refresh_table()

        def action_sort_created(self) -> None:
            self.sort_by = "created"
            self.refresh_table()

        def action_sort_accessed(self) -> None:
            self.sort_by = "accessed"
            self.refresh_table()

        def action_reverse(self) -> None:
            self.reverse_order = not self.reverse_order
            self.refresh_table()

        def action_toggle_hidden(self) -> None:
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
            table = self.query_one("#file-table", DataTable)
            if self._visible_entries:
                current = table.cursor_row if table.cursor_row is not None else 0
                if current == 0:
                    table.move_cursor(row=len(self._visible_entries) - 1)
                else:
                    table.move_cursor(row=0)

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
            self.push_screen(DualPanelScreen(self.path))

        def action_view_file(self) -> None:
            table = self.query_one("#file-table", DataTable)
            if table.cursor_row is not None and self._visible_entries:
                entry = self._visible_entries[table.cursor_row]
                if not entry.is_dir:
                    self.push_screen(FileViewerScreen(entry.path))
                else:
                    self.notify("Cannot view directory", timeout=2)

        def action_fzf_files(self) -> None:
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

        def action_refresh(self) -> None:
            table = self.query_one("#file-table", DataTable)
            current_row = table.cursor_row
            self.load_entries()
            self.refresh_table()
            # Restore cursor position if possible
            if current_row is not None and self._visible_entries:
                table.move_cursor(row=min(current_row, len(self._visible_entries) - 1))
            self.notify("Refreshed", timeout=1)

        def action_quit_cd(self) -> None:
            """Quit and write current directory to temp file for shell to cd."""
            try:
                LASTDIR_FILE.write_text(str(self.path))
            except OSError:
                pass
            self.exit()

        def action_quit(self) -> None:
            """Quit and save current directory for shell cd."""
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
