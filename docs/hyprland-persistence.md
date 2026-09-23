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
- Should displayctl skip `test()` on Hyprland? See "test() is not
  side-effect-free" below. Without it we lose validation; with it, every
  change briefly passes through the rule state.
- Startup race (not yet reproduced): see "Protocol vs hyprctl mismatch".

## Test results (2026-09-23, Hyprland 0.56.2)

### GUI + watchdog conflict: answered

Question: a GUI stays connected, and a revert from *another* client is
ignored while the applying client is connected. Can the connected client
revert its own change? Tested with the hidden `displayctl.py set ...
--self-revert-test 5 --self-revert-via {same,helper}` (DP-3 200 -> 144, keep
the applying connection open, revert after 5 s, verify on a fresh
connection, watchdog as backup):

| Revert sent through | Result |
|---|---|
| the same connection that applied | **works** (verified; watchdog told "keep") |
| a second connection in the same process, first still open | answered "succeeded", **not carried out**; the watchdog restored it after the applying connection closed |
| a separate process, applying connection closed (displayctl today) | works |
| a separate process, applying connection still open (T3) | "succeeded", not carried out |

So Hyprland honours a revert only from the client that applied the change,
or from anyone once that client has disconnected.

**GUI design:** the GUI reverts through its own connection. The watchdog
stays as the crash fallback: if the GUI dies, its connection closes and the
watchdog's revert works. A helper connection does **not** help.

### test() is not side-effect-free

A test-only `--dry-run` (`configuration.test()`, never `apply()`) made
Hyprland re-apply its config rules to all outputs: eDP-1 had been disabled
through the protocol and came back on (catch-all rule), DP-3 got a modeset
at its preferred mode. Our test configuration itself was not applied.
Seen twice: once inferred from the Hyprland log (an unexplained rule-state
commit right after a dry-run), once in a direct probe (disable eDP-1 ->
both views agree -> dry-run -> eDP-1 back on; log shows `Modesetting DP-3
@200`, `Modesetting eDP-1 @144`). Consequence: on Hyprland, `--dry-run` can change
the screen, and every `set` passes through the rule state between its
`test()` and `apply()`.

### Protocol vs hyprctl mismatch

After a reboot, the protocol reported eDP-1 as enabled at 2560,0 while
hyprctl said disabled. Because every configuration restates all enabled
heads, applying from that view switched eDP-1 on. hyprland.lua starts kanshi
at login (`hl.exec_cmd("kanshi")`), whose `external` profile disables eDP-1.
- Hypothesis "kanshi exits -> Hyprland forgets its disable": **not
  reproduced** (start kanshi, it disables eDP-1, pkill after 3 s: both views
  still say disabled).
- Remaining hypothesis: a startup race between kanshi and Hyprland's output
  setup. Testing needs a Hyprland restart.
- Guard added: on Hyprland, `displayctl.py set` compares every head's
  enabled/mode/position with `hyprctl monitors all -j` before snapshotting
  and refuses to apply if they disagree (prints both views).
