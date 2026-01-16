#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=0.40.0",
#     "rich>=13.0.0",
# ]
# ///
"""
lstime - Directory Time Listing TUI

A text user interface for viewing directories sorted by creation or access time.
Features a split-pane view with file list on the left and details preview on the right.

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
  q - Quit
"""

import json
import os
import sys
import stat
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

# Config file for persisting settings
CONFIG_PATH = Path.home() / ".config" / "lstime" / "config.json"


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


try:
    from textual.app import App, ComposeResult
    from textual.widgets import Footer, Static, DataTable
    from textual.containers import Horizontal
    from textual.binding import Binding
    from rich.text import Text
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
# Textual TUI Implementation (Full Interactive Mode)
# ═══════════════════════════════════════════════════════════════════════════════

if HAS_TEXTUAL:
    class LstimeApp(App):
        """TUI application for directory time listing."""

        CSS = """
        Screen {
            background: $surface;
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
        }

        DataTable {
            height: 1fr;
        }

        DataTable > .datatable--cursor {
            background: $secondary;
        }

        #preview-panel {
            width: 1fr;
            border-left: solid $primary-darken-1;
            padding: 1 2;
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
        """

        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("t", "toggle_time", "Toggle Time"),
            Binding("c", "sort_created", "Created"),
            Binding("a", "sort_accessed", "Accessed"),
            Binding("r", "reverse", "Reverse"),
            Binding("h", "toggle_hidden", "Hidden"),
            Binding("y", "copy_path", "Copy Path"),
            Binding("e", "show_tree", "Tree"),
            Binding("[", "shrink_preview", "Shrink"),
            Binding("]", "grow_preview", "Grow"),
        ]

        def __init__(self, path: Path = None):
            super().__init__()
            self.path = path or Path.cwd()
            self.entries: list[DirEntry] = []
            self._visible_entries: list[DirEntry] = []
            self.sort_by = "created"  # "created" or "accessed"
            self.reverse_order = True  # True = newest first
            self.show_hidden = False
            # Load preview width from config (default 30%)
            config = load_config()
            self.preview_width = config.get("preview_width", 30)

        def compose(self) -> ComposeResult:
            yield Static(id="status")
            with Horizontal(id="main-container"):
                yield DataTable(id="list-panel")
                yield Static(id="preview-panel")
            yield Footer()

        def on_mount(self) -> None:
            self.load_entries()
            self.setup_table()
            self.refresh_table()
            self._apply_preview_width()

        def load_entries(self) -> None:
            """Load directory entries."""
            self.entries = get_dir_entries(self.path)

        def setup_table(self) -> None:
            """Set up the data table columns."""
            table = self.query_one("#list-panel", DataTable)
            table.cursor_type = "row"
            table.zebra_stripes = False
            table.add_column("Name", width=40, key="name")
            table.add_column("Time", width=14, key="time")

        def refresh_table(self) -> None:
            """Refresh the table with current entries and sorting."""
            table = self.query_one("#list-panel", DataTable)
            table.clear()

            # Filter hidden files
            entries = self.entries
            if not self.show_hidden:
                entries = [e for e in entries if not e.name.startswith('.')]

            # Sort entries
            if self.sort_by == "created":
                entries = sorted(entries, key=lambda e: e.created, reverse=self.reverse_order)
            else:
                entries = sorted(entries, key=lambda e: e.accessed, reverse=self.reverse_order)

            # Store filtered/sorted entries for path lookup
            self._visible_entries = entries

            # Add rows
            for entry in entries:
                time_val = entry.created if self.sort_by == "created" else entry.accessed

                # Style the name based on type
                if entry.is_dir:
                    name = Text(entry.name + "/", style="bold cyan")
                else:
                    name = Text(entry.name)

                table.add_row(
                    name,
                    format_time(time_val),
                )

            # Update status and preview
            self.update_status()
            if self._visible_entries:
                self.update_preview(0)

        def update_status(self) -> None:
            """Update the status bar."""
            status = self.query_one("#status", Static)
            sort_label = "Creation Time" if self.sort_by == "created" else "Access Time"
            order_label = "(newest first)" if self.reverse_order else "(oldest first)"
            hidden_label = "[showing hidden]" if self.show_hidden else ""

            visible = len([e for e in self.entries if self.show_hidden or not e.name.startswith('.')])
            total = len(self.entries)

            status.update(f" Sorted by: {sort_label} {order_label}  |  {visible}/{total} items {hidden_label}")

        def action_toggle_time(self) -> None:
            """Toggle between creation and access time."""
            self.sort_by = "accessed" if self.sort_by == "created" else "created"
            self.refresh_table()

        def action_sort_created(self) -> None:
            """Sort by creation time."""
            self.sort_by = "created"
            self.refresh_table()

        def action_sort_accessed(self) -> None:
            """Sort by access time."""
            self.sort_by = "accessed"
            self.refresh_table()

        def action_reverse(self) -> None:
            """Reverse the sort order."""
            self.reverse_order = not self.reverse_order
            self.refresh_table()

        def action_toggle_hidden(self) -> None:
            """Toggle showing hidden files."""
            self.show_hidden = not self.show_hidden
            self.refresh_table()

        def action_copy_path(self) -> None:
            """Copy the full path of the selected entry to clipboard."""
            table = self.query_one("#list-panel", DataTable)
            if table.cursor_row is not None and self._visible_entries:
                try:
                    entry = self._visible_entries[table.cursor_row]
                    full_path = str(entry.path.absolute())
                    import subprocess
                    subprocess.run(["pbcopy"], input=full_path.encode(), check=True)
                    self.notify(f"Copied: {full_path}")
                except (IndexError, subprocess.CalledProcessError):
                    self.notify("Failed to copy path", severity="error")

        def action_show_tree(self) -> None:
            """Show recursive tree view for the selected directory in preview."""
            table = self.query_one("#list-panel", DataTable)
            if table.cursor_row is not None and self._visible_entries:
                try:
                    entry = self._visible_entries[table.cursor_row]
                    if entry.is_dir:
                        preview = self.query_one("#preview-panel", Static)
                        content = self._preview_tree(entry.path)
                        preview.update(content)
                    else:
                        self.notify("Not a directory", severity="warning")
                except IndexError:
                    pass

        def _apply_preview_width(self) -> None:
            """Apply the current preview width to the panels."""
            preview = self.query_one("#preview-panel", Static)
            list_panel = self.query_one("#list-panel", DataTable)
            preview.styles.width = f"{self.preview_width}%"
            list_panel.styles.width = f"{100 - self.preview_width}%"

        def _save_preview_width(self) -> None:
            """Save the preview width to config."""
            config = load_config()
            config["preview_width"] = self.preview_width
            save_config(config)

        def action_shrink_preview(self) -> None:
            """Shrink the preview panel."""
            if self.preview_width > 10:
                self.preview_width -= 5
                self._apply_preview_width()
                self._save_preview_width()

        def action_grow_preview(self) -> None:
            """Grow the preview panel."""
            if self.preview_width < 70:
                self.preview_width += 5
                self._apply_preview_width()
                self._save_preview_width()

        def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
            """Update preview when cursor moves."""
            self.update_preview(event.cursor_row)

        def update_preview(self, row_index: int) -> None:
            """Update the preview panel with selected entry details."""
            preview = self.query_one("#preview-panel", Static)

            if row_index is None or not self._visible_entries or row_index >= len(self._visible_entries):
                preview.update("")
                return

            entry = self._visible_entries[row_index]

            if entry.is_dir:
                content = self._preview_tree(entry.path)
            else:
                content = self._preview_file(entry.path)

            preview.update(content)

        def _preview_directory(self, path: Path, max_items: int = 50) -> str:
            """Generate sorted list of directory contents."""
            lines = [f"[bold magenta]{path.name}/[/]", ""]

            try:
                # Get entries with time info
                entries = get_dir_entries(path)

                # Filter hidden
                if not self.show_hidden:
                    entries = [e for e in entries if not e.name.startswith('.')]

                # Sort by current criteria
                if self.sort_by == "created":
                    entries = sorted(entries, key=lambda e: e.created, reverse=self.reverse_order)
                else:
                    entries = sorted(entries, key=lambda e: e.accessed, reverse=self.reverse_order)

                total = len(entries)
                display_entries = entries[:max_items]

                # Calculate max name width for alignment
                max_name = max((len(e.name) + (1 if e.is_dir else 0) for e in display_entries), default=20)
                max_name = min(max_name, 35)  # Cap at 35 chars

                for entry in display_entries:
                    time_val = entry.created if self.sort_by == "created" else entry.accessed
                    time_str = format_time(time_val)
                    name = entry.name + ("/" if entry.is_dir else "")

                    if len(name) > max_name:
                        name = name[:max_name-3] + "..."

                    if entry.is_dir:
                        lines.append(f"[cyan]{name:<{max_name}}[/] [dim]{time_str:>12}[/]")
                    else:
                        lines.append(f"[white]{name:<{max_name}}[/] [dim]{time_str:>12}[/]")

                if total > max_items:
                    lines.append(f"[dim]... and {total - max_items} more[/]")

                if total == 0:
                    lines.append("[dim]Empty directory[/]")

            except PermissionError:
                lines.append("[red]Permission denied[/]")

            return "\n".join(lines)

        def _preview_tree(self, path: Path, max_depth: int = 3, max_items: int = 100) -> str:
            """Generate recursive tree view of directory."""
            lines = [f"[bold magenta]{path.name}/[/]", ""]
            count = [0]  # mutable counter
            tree_lines = []  # collect lines for column alignment

            def add_tree(p: Path, prefix: str = "", depth: int = 0):
                if count[0] >= max_items or depth > max_depth:
                    return

                try:
                    entries = get_dir_entries(p)

                    if not self.show_hidden:
                        entries = [e for e in entries if not e.name.startswith('.')]

                    # Sort by current criteria
                    if self.sort_by == "created":
                        entries = sorted(entries, key=lambda e: e.created, reverse=self.reverse_order)
                    else:
                        entries = sorted(entries, key=lambda e: e.accessed, reverse=self.reverse_order)

                    for i, entry in enumerate(entries):
                        if count[0] >= max_items:
                            tree_lines.append((f"{prefix}[dim]... truncated[/]", "", False))
                            return

                        is_last = i == len(entries) - 1
                        connector = "└── " if is_last else "├── "

                        time_val = entry.created if self.sort_by == "created" else entry.accessed
                        time_str = format_time(time_val)
                        name = entry.name + ("/" if entry.is_dir else "")

                        tree_lines.append((f"{prefix}{connector}", name, entry.is_dir, time_str))
                        count[0] += 1

                        if entry.is_dir:
                            next_prefix = prefix + ("    " if is_last else "│   ")
                            add_tree(entry.path, next_prefix, depth + 1)

                except PermissionError:
                    tree_lines.append((f"{prefix}[red]Permission denied[/]", "", False, ""))

            add_tree(path)

            # Calculate max width for name column
            max_name = 25
            for item in tree_lines:
                if len(item) == 4:
                    prefix, name, _, _ = item
                    # Account for visible prefix length (tree chars)
                    name_len = len(name)
                    if name_len > max_name:
                        max_name = min(name_len, 30)

            # Format with aligned columns
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

        def _preview_file(self, path: Path, max_lines: int = 100) -> str:
            """Preview file contents."""
            lines = [f"[bold cyan]{path.name}[/]", ""]

            # Check if binary
            try:
                with open(path, 'rb') as f:
                    chunk = f.read(1024)
                    if b'\x00' in chunk:
                        size = path.stat().st_size
                        lines.append(f"[dim]Binary file ({format_size(size)})[/]")
                        return "\n".join(lines)
            except (PermissionError, OSError) as e:
                lines.append(f"[red]Cannot read: {e}[/]")
                return "\n".join(lines)

            # Read text content
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    content_lines = []
                    for i, line in enumerate(f):
                        if i >= max_lines:
                            content_lines.append(f"[dim]... truncated ({max_lines}+ lines)[/]")
                            break
                        # Escape markup and limit line length
                        line = line.rstrip('\n\r')
                        if len(line) > 200:
                            line = line[:200] + "..."
                        # Escape rich markup
                        line = line.replace("[", "\\[")
                        content_lines.append(line)

                    if not content_lines:
                        lines.append("[dim]Empty file[/]")
                    else:
                        lines.extend(content_lines)

            except (PermissionError, OSError) as e:
                lines.append(f"[red]Cannot read: {e}[/]")

            return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Rich Fallback Implementation (Static Display with Key Handling)
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

    # Create table
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

    # Print header
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
  q                 Quit

Examples:
  lstime                    # List current dir by creation time
  lstime -a                 # List by access time
  lstime --tui ~/Documents  # Interactive mode for Documents
  lstime -rH                # Oldest first, show hidden
"""
    print(help_text)


def main():
    """Main entry point."""
    # Parse arguments
    args = sys.argv[1:]

    path = None
    sort_by = "created"
    reverse = True  # newest first by default
    show_hidden = False
    force_tui = None  # None = auto, True = force TUI, False = force static

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

    # Validate path
    if path and not path.exists():
        print(f"Error: Path does not exist: {path}")
        sys.exit(1)

    # Determine mode
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
