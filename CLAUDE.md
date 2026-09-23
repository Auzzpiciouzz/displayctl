# displayctl — Windows-style display settings for Wayland

## Goal
GUI display manager for Arch: drag-to-arrange monitors, resolution/refresh/scale/rotation,
Extend / Duplicate / Only-show-on-1/2, auto dock profiles, 15s "keep changes?" auto-revert.

## Machine
- HP Omen 16, NVIDIA primary GPU (open-dkms), Intel iGPU secondary
- Outputs (as reported by list_outputs.py):
  - eDP-1: laptop panel, BOE 0x0AAE, 1920x1080 @ 144Hz (preferred) or 60Hz; currently disabled
  - DP-3: Samsung Odyssey G5 27" (LS27FG53x), 2560x1440, max 200Hz; currently 1440p@200, pos 0,0, scale 1
- Hyprland 0.56.2 with LUA config (~/.config/hypr/hyprland.lua, hl.* API).
  `hyprctl keyword/dispatch` do NOT work — use `hyprctl eval '<lua>'`
- Only Hyprland monitor rule: the catch-all (preferred mode, auto position)
- niri is a secondary target

## Findings (tested 2026-09-23, Hyprland 0.56.2)
- Hyprland re-applies its config rules to any head whose properties a commit
  doesn't set (seen on commits that enable/disable an output). So:
  - every configuration sends ALL properties of every enabled head (never a diff)
  - protocol-only changes aren't durable; see docs/hyprland-persistence.md
- Hyprland honours a revert only from the client that applied the change, or
  from anyone once that client has disconnected. Another client (the
  watchdog, or even a second connection in the same process) gets
  "succeeded" but nothing happens. displayctl.py closes its connection right
  after apply — keep it that way. The GUI must revert through its OWN
  connection (tested: works); the watchdog is only the crash fallback.
- `test()` is NOT side-effect-free on Hyprland: it makes Hyprland re-apply its
  config rules to all outputs (a protocol-disabled eDP-1 came back on).
  DECISION: displayctl skips test() when HYPRLAND_INSTANCE_SIGNATURE is set
  (local checks + hyprctl cross-check instead); other compositors keep it.
  Worth reporting upstream (see docs/hyprland-persistence.md).
- "succeeded" doesn't prove anything happened: always re-read and verify.
- kanshi: config at ~/.config/kanshi/config (profiles external / extend /
  laptop / undocked; "external" disables eDP-1). hyprland.lua STARTS IT AT
  LOGIN (`hl.exec_cmd("kanshi")`) — it runs after every boot until killed.
  eDP-1's "disabled" state came from kanshi via the protocol, not a Hyprland
  rule, so any rule re-evaluation turns eDP-1 back on (catch-all = enabled).
  kanshi uses the same protocol as displayctl; don't run both.
- After a boot the protocol once said eDP-1 enabled while hyprctl said
  disabled; applying from that view turned eDP-1 on. Not reproduced by
  killing kanshi mid-session; suspected startup race. displayctl.py now
  refuses to apply on Hyprland when the protocol and `hyprctl monitors all -j`
  disagree.
- A leftover test monitors.lua once made reverts look broken: check for stray
  monitor rules before debugging "revert didn't take".

## Stack
Python 3, PyGObject (GTK4 + libadwaita), pywayland.
Backend protocol: wlr-output-management-unstable-v1
(XML at /usr/share/wlr-protocols/unstable/). Venv: .venv (system site packages).
Files: backend.py (Wayland), displayctl.py (CLI), watchdog.py (revert
process), gui.py (GTK4 GUI; Wayland fd in the GLib main loop, one persistent
connection, redraws on every 'done').

## Next steps (as of 2026-09-23)
- Dev sessions: `pkill kanshi` first (it's autostarted and fights displayctl).
1. Phase 3 step 2: drag-to-arrange on the canvas (still no applying), then
   applying from the GUI with the 15 s keep/revert. Revert through the GUI's
   OWN connection; watchdog only as crash fallback.
2. Check the GUI's live refresh: keep it open and change a mode with
   displayctl.py in kitty; the canvas should update on the 'done' event
   (only tested in-memory so far).
3. Phase 4: rule-based dock switching in Lua on Hyprland (design in
   docs/hyprland-persistence.md; open points listed there).
4. Optional, later: startup-race test; report the test() side effect upstream.

## Rules
- Never edit ~/.config/hypr/hyprland.lua; the app owns only its own monitors.lua
- Any apply must be revertible; test before apply. Display changes go through
  displayctl.py (snapshot + watchdog), never ad-hoc scripts
- Generated monitors.lua must never disable eDP-1 (undocked boot = no screen);
  enabling/disabling outputs belongs to the dock daemon
- Explain code with comments — I'm learning
