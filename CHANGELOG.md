# Changelog

All notable changes to quasseltui are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.1] - 2026-06-01

This release is a stability and "feels less flaky" pass over the live client.
A review traced a vague flakiness to a handful of disconnect, scroll, and
buffer-selection gaps; each is fixed below and covered by new regression
tests.

### Fixed

- **Disconnects are now visible.** A mid-session drop used to fail silently —
  the UI went quiet and kept accepting typed lines that went nowhere. The
  input bar is now disabled with a placeholder that names the reason, and a
  notification is shown, so it's clear the connection is gone.
- **Scrollback stays put while you read.** On a busy channel, scrolling up to
  read history no longer yanks you back: incoming live messages and fetched
  backlog now keep the viewport anchored on the message you were reading.
  This is correct even for long lines that wrap across multiple rows.
- **The active channel no longer jumps on its own.** A message arriving in
  another channel can no longer steal your place, and the initial channel
  selection now lands where the activity actually is instead of an arbitrary
  one.
- **Failed actions tell you why.** A message that fails to send, or history
  that fails to load, now raises a notification instead of silently bouncing
  your text back or doing nothing.
- **The "read up to here" marker no longer drags the view.** Moving the
  marker (including the empty-Enter "mark latest" shortcut) keeps your scroll
  position instead of jumping the viewport to the marker's new spot.
- **Tabbing into the message log no longer jumps to the newest message** when
  you have scrolled up — the cursor lands on a visible row and the view stays
  where it was.
- **No more "Could not load history" spam after a disconnect.** History
  requests are no longer issued once the connection is gone, so switching
  channels post-drop doesn't produce repeated failures.

### Security

- Notifications and disconnect reasons that embed untrusted core-supplied
  text are now sanitized (control bytes escaped), length-bounded, and shown
  with markup disabled, so a hostile or malformed string such as
  `[Errno 104]` can't restyle or break the on-screen toast.

### Changed

- **Release builds carry the real version.** The CI release workflow now
  resolves the version from the release tag (stripping a leading `v`) and
  stamps it into the published PyPI sdist and wheel instead of the `0.0.0`
  placeholder, and attaches the built sdist + wheel to the GitHub release as
  downloadable assets. Manual `workflow_dispatch` runs accept an optional
  `version` input.

### Documentation

- Expanded the README with additional usage examples.

[0.9.1]: https://github.com/linsomniac/quasseltui/compare/v0.9.0...v0.9.1
