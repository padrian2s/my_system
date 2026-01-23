# Textual Key Binding Patterns for Multi-Screen Apps

## Problem 1: Key Conflicts Between App and Screen

When building a Textual application with multiple screens (e.g., a main view and a file manager screen), key bindings can conflict between the App and Screen levels.

### Symptoms

1. **Keys not captured**: A key binding defined on a Screen doesn't work because the parent App's binding intercepts it first.
2. **Wrong handler called**: Pressing a key on a pushed Screen triggers the App's handler instead of the Screen's handler.

### Solution: Delegation Pattern

Check if the current screen is a specific type and delegate:

```python
def action_enter_dir(self) -> None:
    if isinstance(self.screen, DualPanelScreen):
        self.screen.action_enter_dir()
        return
    # App's own handling
    ...
```

---

## Problem 2: Modal Dialogs and Key Event Bubbling

When using Textual's modal screens (ModalScreen), key bindings with `priority=True` on the parent App can capture keys BEFORE the modal dialog processes them.

### Symptoms

- Pressing Enter in a dialog's Input field navigates directories in the main view
- Pressing keys meant for the dialog triggers actions on the parent screen
- Dialog appears to "do nothing" because parent consumed the event

### Solution: Three-Part Fix

#### 1. Dialog Input Handlers MUST Stop Event Propagation

```python
def on_input_submitted(self, event: Input.Submitted):
    event.stop()  # CRITICAL: Stop event from bubbling
    self.action_submit()
```

#### 2. Dialog Bindings Should Have Priority

```python
class MyDialog(ModalScreen):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Yes", priority=True),  # priority=True
    ]
```

#### 3. Parent View Bindings Should NOT Have Priority for Conflicting Keys

```python
class MyApp(App):
    BINDINGS = [
        ("enter", "enter_dir", "Enter"),  # NO priority=True for Enter!
        Binding("ctrl+f", "find", "Find", priority=True),  # OK for non-conflicting keys
    ]
```

#### 4. Safety Check in Parent Actions (Belt and Suspenders)

```python
def action_enter_dir(self) -> None:
    # Don't navigate if a modal dialog is open
    if isinstance(self.screen, ModalScreen):
        return
    # ... rest of action
```

---

## Key Rules Summary

| Rule | Why |
|------|-----|
| Dialogs use `priority=True` | So they capture keys before parent |
| Parents DON'T use `priority=True` for Enter/Escape | So dialogs can handle these first |
| Always `event.stop()` in input handlers | Prevents event bubbling to parent |
| Check `isinstance(self.screen, ModalScreen)` | Safety net in parent actions |

## Common Conflicting Keys

- `Enter` - Confirmation in dialogs AND navigation in file managers
- `Escape` - Close dialogs AND other parent actions
- `y/n` - Confirmation dialogs

## Testing Checklist

- [ ] Press Enter in dialog Input - should submit, not affect parent
- [ ] Press Enter on confirmation dialog - should confirm, not navigate
- [ ] Press Escape in dialog - should close dialog only
- [ ] After dialog closes, Enter should work normally for navigation
