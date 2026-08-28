# Owner marks

The images here are what the apps draw in the owner column: one mark per kind of
runner, so a glance at the column tells you who holds a row.

| File | Used for |
|---|---|
| `claude.png` | chat-agent rows |
| `codex.png` | code-agent rows |
| `accelerator.png` | local work holding the GPU |
| `compute.png` | local work on CPU |

`claude-menu.png` and `codex-menu.png` are compact, project-drawn menu-bar
variants of the corresponding owner marks. `wordmark.png` is the
maintainer-supplied COORD wordmark. These assets are project identifiers, not
vendor-supplied artwork, and their use does not imply affiliation or endorsement.

To substitute your own, replace the file. The lookup is by name, so no code
changes: `AgentMarks.owner(_:size:)` checks for a named asset first and only
falls back to the marks drawn in `AgentMarks.swift` when none is present. Those
drawn marks are also what you get if you delete this directory entirely, so the
apps never render a blank column.

Square, transparent PNGs at 256px look best; they are drawn at 9-18pt.

Files here are stripped of EXIF and XMP on the way in. Authoring metadata
travels with an image when it is opened and re-saved, so it describes whatever
file was opened rather than the art as it now stands.
