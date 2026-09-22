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
- While the client that applied a change is still connected, a revert applied
  by another client (the watchdog) is answered "succeeded" but not carried
  out. displayctl.py closes its connection right after apply — keep it that way.
- "succeeded" doesn't prove anything happened: always re-read and verify.
- kanshi: config at ~/.config/kanshi/config (profiles external / extend /
  laptop / undocked; "external" disables eDP-1). Not running as of 2026-09-23.
  eDP-1's "disabled" state came from kanshi via the protocol, not a Hyprland
  rule, so any rule re-evaluation turns eDP-1 back on (catch-all = enabled).
  kanshi uses the same protocol as displayctl; don't run both.
- A leftover test monitors.lua once made reverts look broken: check for stray
  monitor rules before debugging "revert didn't take".

## Stack
Python 3, PyGObject (GTK4 + libadwaita), pywayland.
Backend protocol: wlr-output-management-unstable-v1
(XML at /usr/share/wlr-protocols/unstable/). Venv: .venv (system site packages).

## Next steps (as of 2026-09-23)
1. Run test C (crash test) with the early-close change, in kitty:
   `displayctl.py set DP-3 --mode 2560x1440@144`, then during the countdown
   `pkill -9 -f displayctl.py` from another terminal. Expect watchdog.log to
   show "displayctl exited without confirming -> reverting" and "revert
   verified" immediately; check `hyprctl monitors` is back at 200 Hz.
2. Test whether a GUI can revert from its own (still-connected) connection —
   see the "GUI + watchdog conflict" open question in
   docs/hyprland-persistence.md. The answer decides the GUI's revert design.
3. Then start Phase 3: the GTK4 / libadwaita GUI.

## Rules
- Never edit ~/.config/hypr/hyprland.lua; the app owns only its own monitors.lua
- Any apply must be revertible; test before apply. Display changes go through
  displayctl.py (snapshot + watchdog), never ad-hoc scripts
- Generated monitors.lua must never disable eDP-1 (undocked boot = no screen);
  enabling/disabling outputs belongs to the dock daemon
- Explain code with comments — I'm learning
