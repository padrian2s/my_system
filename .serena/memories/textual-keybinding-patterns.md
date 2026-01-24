# Textual Key Binding Patterns for Modal Dialogs

## Problem: Key Events Bubbling Through Modal Dialogs

When using Textual's modal screens (ModalScreen), key bindings with `priority=True` on the parent App or Screen can capture keys before the modal dialog processes them. This causes actions to trigger on the view behind the dialog.

### Symptoms
- Pressing Enter in a dialog's Input field navigates directories in the main view
- Pressing keys meant for the dialog triggers actions on the parent screen
- Dialog appears to "do nothing" because parent consumed the event

## Solution: Three-Part Fix

### 1. Dialog Input Handlers Must Stop Event Propagation

```python
def on_input_submitted(self, event: Input.Submitted):
    event.stop()  # CRITICAL: Stop event from bubbling
    self.action_submit()
```

### 2. Dialog Bindings Should Have Priority

```python
class MyDialog(ModalScreen):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Yes", priority=True),  # priority=True
    ]
```

### 3. Parent View Bindings Should NOT Have Priority for Conflicting Keys

```python
class MyApp(App):
    BINDINGS = [
        ("enter", "enter_dir", "Enter"),  # NO priority=True
        Binding("ctrl+f", "find", "Find", priority=True),  # OK for non-conflicting keys
    ]
```

### 4. Safety Check in Parent Actions (Belt and Suspenders)

```python
def action_enter_dir(self) -> None:
    # Don't navigate if a modal dialog is open
    if isinstance(self.screen, ModalScreen):
        return
    # ... rest of action
```

## Key Rules

1. **Dialogs win**: Modal dialogs should have `priority=True` on their key bindings
2. **Parents yield**: Parent views should NOT use `priority=True` for keys that dialogs also use (Enter, Escape, etc.)
3. **Stop propagation**: Always call `event.stop()` in `on_input_submitted` and similar event handlers
4. **Safety check**: Add `isinstance(self.screen, ModalScreen)` checks in parent actions as a safety net

## Common Conflicting Keys

- `Enter` - Used for confirmation/submission in dialogs AND navigation in file managers
- `Escape` - Used to close dialogs AND sometimes for other actions
- `y/n` - Used in confirmation dialogs

## Testing Checklist

- [ ] Press Enter in dialog Input - should submit, not affect parent
- [ ] Press Enter on dialog button - should confirm, not affect parent  
- [ ] Press Escape in dialog - should close dialog only
- [ ] After dialog closes, keys should work normally in parent view
