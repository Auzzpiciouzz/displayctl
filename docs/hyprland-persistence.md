# Hyprland: making display changes stick

Status: design note, not implemented yet.

## Why the protocol alone isn't enough

Tested on Hyprland 0.56.2 (2026-09-23):

- On a commit that enables or disables an output, Hyprland re-applies its
  config rules to every head whose properties the commit doesn't set.
  Example: DP-3 set to 144 Hz at 0,0 through the protocol; then enabling
  eDP-1 (with DP-3 sent as "enabled, unchanged") reset DP-3 to 200 Hz at
  4480,0, which is what the catch-all rule (preferred, auto) gives.
- So a change made only through wlr-output-management lasts until the next
  commit that happens to re-evaluate rules, or until a config reload.
- Workaround in place now: every configuration sends every property of every
  enabled head (backend.py `_configure_head`).

## Design

**Protocol = preview. monitors.lua = keep.**

1. `set` applies the change through wlr-output-management (test, then apply)
   and runs the 15-second "Keep changes?" countdown, exactly as today. This
   is only a preview.
2. On "keep", displayctl writes the new layout as Hyprland monitor rules to
   its own file, `~/.config/hypr/displayctl/monitors.lua`, then reloads
   Hyprland so the rules and the live state agree.
3. The user loads that file themselves: one `dofile(...)` line at the **end**
   of `hyprland.lua`, so our rules come after (and override) the defaults.
   displayctl never edits `hyprland.lua` (CLAUDE.md rule). The line should
   tolerate the file being missing (e.g. wrapped in `pcall`), so a fresh
   install still boots.

## Revert of a kept change

A kept change lives in a file, so reverting means reverting the file:

1. Before writing, keep the previous version (`monitors.lua.prev`). Write the
   new file atomically (write temp file, then rename).
2. Revert = put the previous file back, reload Hyprland, then **verify**:
   re-read the outputs through the protocol and compare with the expected
   state (same check as watchdog.py `verify`). If they don't match, log a
   loud error.
3. The watchdog owns this for the preview phase as it does today. Once the
   file is written, the same "restore + reload + verify" is what a later
   "undo" command would run.

## Hard rule: never disable eDP-1 in the generated file

The generated monitors.lua must **never** disable eDP-1 (or any laptop
panel). It is loaded at every start, including an undocked boot, where
eDP-1 is the only screen: a rule disabling it would leave the laptop with no
display at all.

Turning outputs on and off depends on what's plugged in, so it belongs to the
dock daemon (the "auto dock profiles" part of the goal), applied at runtime
through the protocol, not to a static rules file. The rules file only
describes mode, position, scale and transform.

## Open questions

- How to trigger a reload with the Lua config (`hyprctl keyword/dispatch`
  don't work; `hyprctl eval '<lua>'` does). Does a reload re-evaluate
  monitor rules immediately?
- The exact `hl.*` API for monitor rules, to generate the file.
- kanshi (see CLAUDE.md) also drives outputs through the protocol. Decide
  whether the dock daemon replaces it, so two tools don't fight.
- **GUI + watchdog conflict.** displayctl.py can close its Wayland connection
  right after apply, so the separate watchdog's revert works. A GUI stays
  connected the whole time, and on Hyprland a revert from another client
  while the applying client is still connected is answered "succeeded" but
  not carried out. So a separate watchdog's revert would fail under the GUI.
  Test: can the GUI revert from its own connection (the same client that
  applied)? If yes, the GUI reverts itself and the watchdog stays as the
  crash-only fallback (the GUI's connection is gone when it crashes, so the
  watchdog's revert works then). If no, the GUI may need to apply through a
  short-lived helper connection instead of its main one.
