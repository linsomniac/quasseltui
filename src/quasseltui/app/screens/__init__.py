"""Textual screens for the quasseltui app.

Phase 6 only ships `ChatScreen`, the main 3-pane view. Phase 11 adds a
`ConnectScreen` for first-run credential entry — deliberately kept as
its own file rather than gated inside `ChatScreen`, because the
transition between them is a `push_screen` / `pop_screen` pair and
mixing those lifecycles is where bugs hide.
"""
