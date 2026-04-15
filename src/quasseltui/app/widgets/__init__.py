"""Reusable Textual widgets for the quasseltui TUI.

Every widget in here takes a `ClientState` reference (or a narrower
projection of one) at construction time and renders by reading from it.
Phase 6 seeds the state statically in `demo_data.py`; phase 7 swaps in
the live `QuasselClient.state`. Keeping the widgets data-driven means
that swap touches only the construction site, never the rendering code.
"""
