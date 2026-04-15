# quasseltui

Terminal client for [Quassel IRC](https://www.quassel-irc.org/) cores. Connects
to your existing `quasselcore` and gives you a Textual-based TUI as an
alternative to `quasselclient` (the Qt GUI) or Quasseldroid.

**Status: under construction.** See `/home/sean/.claude/plans/prancy-plotting-lovelace.md`
for the build plan.

## Quick start

```sh
uv sync
uv run python -m quasseltui --help
```

## Development

```sh
uv run pytest          # unit tests
uv run ruff check      # lint
uv run ruff format     # format
uv run mypy src        # type-check
uv run lint-imports    # enforce layer boundaries
```
