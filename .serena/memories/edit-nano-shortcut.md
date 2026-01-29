# Edit in Nano Shortcut (E key)

## Overview
The `E` (uppercase) key opens the currently highlighted text file in `nano` for editing.
Available in both `LstimeApp` (single-panel view) and `DualPanelScreen` (dual-panel file manager).

## Implementation Details

### Binding
- Key: `E` (uppercase)
- Action: `edit_nano`
- Label in toolbar: `"edit"`

### Behavior
1. Gets the currently highlighted file from the table/list
2. Skips directories (notifies "Cannot edit directory")
3. Skips binary files like images, archives, media, office docs (notifies "Cannot edit binary file")
4. Suspends the Textual app (`self.suspend()` / `self.app.suspend()`) to hand terminal control to nano
5. After nano exits, resumes the Textual app
6. In `LstimeApp`, refreshes the preview panel via `self.update_preview(table.cursor_row)`
7. Shows notification "Edited: {filename}"

### Binary extensions excluded
`.pdf .zip .tar .gz .bz2 .xz .7z .rar .exe .dll .so .dylib .bin .dat .mp3 .mp4 .avi .mov .mkv .wav .flac .doc .docx .xls .xlsx .ppt .pptx .png .jpg .jpeg .gif .bmp .ico .webp .tiff .tif`

### Pattern
Uses the same `with self.suspend()` + `subprocess.run()` pattern as `action_fzf_files`.
