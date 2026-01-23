# Textual Key Binding Patterns for Multi-Screen Apps

## Problem

When building a Textual application with multiple screens (e.g., a main view and a file manager screen), key bindings can conflict between the App and Screen levels.

### Symptoms

1. **Keys not captured**: A key binding defined on a Screen doesn't work because the parent App's binding intercepts it first.
2. **Wrong handler called**: Pressing a key on a pushed Screen triggers the App's handler instead of the Screen's handler.
3. **Input widgets blocked**: Modal dialogs with Input widgets can't receive Enter key because the App captures it.

## Root Cause

Textual's binding resolution follows a hierarchy. When `priority=True` is set on a binding:
- It takes precedence over widget-level key handling
- App-level priority bindings can intercept keys before they reach pushed Screens

## Solution Pattern

### 1. Use `priority=True` for App Bindings That Must Work Globally

```python
class MyApp(App):
    BINDINGS = [
        Binding("enter", "enter_dir", "Enter", priority=True),
        Binding("home", "go_first", "First", priority=True),
        Binding("end", "go_last", "Last", priority=True),
    ]
```

### 2. Delegate to Active Screen in App Actions

Check if the current screen is a specific type and delegate:

```python
def action_enter_dir(self) -> None:
    if isinstance(self.screen, DualPanelScreen):
        self.screen.action_enter_dir()
        return
    # App's own handling
    ...

def action_go_first(self) -> None:
    if isinstance(self.screen, DualPanelScreen):
        self.screen.action_go_first()
        return
    # App's own handling
    ...
```

### 3. Modal Dialogs Need Priority Bindings Too

For ModalScreen subclasses with Input widgets, add priority enter binding:

```python
class RenameDialog(ModalScreen):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Submit", priority=True),
    ]

    def on_input_submitted(self, event: Input.Submitted):
        self.action_submit()

    def action_submit(self):
        input_widget = self.query_one("#my-input", Input)
        value = input_widget.value.strip()
        # Process value...
        self.dismiss(value)
```

### 4. Screen Bindings Can Mirror App Bindings

Define the same bindings on both App and Screen, with Screen methods handling screen-specific logic:

```python
class DualPanelScreen(Screen):
    BINDINGS = [
        Binding("home", "go_first", "First", priority=True),
        Binding("end", "go_last", "Last", priority=True),
        Binding("enter", "enter_dir", "Enter", priority=True),
    ]

    def action_go_first(self):
        list_view = self.query_one(f"#{self.active_panel}-list", ListView)
        if list_view.children:
            list_view.index = 0
            list_view.scroll_home(animate=False)
```

## Summary

| Scenario | Solution |
|----------|----------|
| App key doesn't work on DataTable/ListView | Add `priority=True` to App binding |
| Screen key captured by App | App's action checks `isinstance(self.screen, ...)` and delegates |
| Modal Input can't receive Enter | Add `Binding("enter", "submit", priority=True)` to dialog |
| Need same key on App and Screen | Define on both + use delegation pattern |
