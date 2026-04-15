"""Cross-layer utilities that are not tied to a specific architectural layer.

Modules here are imported by both the low-layer protocol code and the
high-layer Textual UI — they're intentionally outside the `qt→app`
layered contract because the rules they encode ("escape control
characters before printing") are the same regardless of which layer
happens to be doing the printing.

Design rule: nothing in `quasseltui.util` may import from
`quasseltui.{qt,protocol,sync,client,app}`. It's a leaf package.
"""
