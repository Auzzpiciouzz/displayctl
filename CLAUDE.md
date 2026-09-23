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
  config rules to all outputs (a protocol-disabled eDP-1 came back on). So
  `--dry-run` can change the screen, and each `set` briefly passes through
  the rule state between test() and apply().
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

## Next steps (as of 2026-09-23)
1. Decide whether displayctl skips `test()` on Hyprland (it re-applies config
   rules; see docs/hyprland-persistence.md). Until then, avoid `--dry-run`
   when eDP-1 is disabled: it turns it back on.
2. Decide kanshi's future: it's autostarted by hyprland.lua and fights
   displayctl. (You edit hyprland.lua; displayctl never does.)
3. Optional: test the startup-race hypothesis for the protocol/hyprctl
   mismatch (needs a Hyprland restart with kanshi autostart on).
4. Start Phase 3: the GTK4 / libadwaita GUI. Revert design is settled: the
   GUI reverts through its own connection; watchdog = crash fallback only.

## Rules
- Never edit ~/.config/hypr/hyprland.lua; the app owns only its own monitors.lua
- Any apply must be revertible; test before apply. Display changes go through
  displayctl.py (snapshot + watchdog), never ad-hoc scripts
- Generated monitors.lua must never disable eDP-1 (undocked boot = no screen);
  enabling/disabling outputs belongs to the dock daemon
- Explain code with comments — I'm learning
