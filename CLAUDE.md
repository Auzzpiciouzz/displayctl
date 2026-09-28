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
- The applying client is sent the head state from BEFORE its own commit and
  nothing after (its view is one change behind); watchers also got stale
  enable/disable state. Fresh reads are right. The GUI treats pushes only as
  "something changed" and re-reads on a fresh connection 300 ms after the
  last push; it also syncs during the hold and reconnects after every
  transaction;
  displayctl verifies its own apply (twice) before asking Keep.
- Hyprland sends `done` only to the client that applied a change; clients that
  only watch get the head updates without `done`. gui.py refreshes after any
  batch that arrives without `done`. A watching connection does NOT block
  other clients' applies.
- Hyprland ignores the serial in create_configuration (tested: a stale serial
  was applied, not cancelled). GUI keeps ONE connection; safety = live state
  + hyprctl cross-check before each apply (see "GUI apply strategy" in
  docs/hyprland-persistence.md).
- "succeeded" doesn't prove anything happened: always re-read and verify.
- kanshi: config at ~/.config/kanshi/config (profiles external / extend /
  laptop / undocked; "external" disables eDP-1). hyprland.lua STARTS IT AT
  LOGIN (`hl.exec_cmd("kanshi")`) — it runs after every boot until killed.
  eDP-1's "disabled" state came from kanshi via the protocol, not a Hyprland
  rule, so any rule re-evaluation turns eDP-1 back on (catch-all = enabled).
  kanshi uses the same protocol as displayctl; don't run both. It stays
  connected after applying, so while it runs every other tool's changes are
  silently ignored (see the hard rule under Rules).
  displayctl WARNS (never blocks) while kanshi runs: `set`/`persist`/`undo`
  print a warning; the GUI shows a banner, re-checked every 3 s
  (safety.kanshi_warning, exact match on /proc/<pid>/comm). kanshi's only
  unique features: Super+P (display-mode.sh -> `kanshictl switch
  external|extend|laptop`) and eDP-1 off at a docked login. Recommended
  2026-09-28: remove it from autostart together with L10.
- After a boot the protocol once said eDP-1 enabled while hyprctl said
  disabled; applying from that view turned eDP-1 on. Not reproduced by
  killing kanshi mid-session; suspected startup race. displayctl.py now
  refuses to apply on Hyprland when the protocol and `hyprctl monitors all -j`
  disagree.
- A leftover test monitors.lua once made reverts look broken: check for stray
  monitor rules before debugging "revert didn't take".
- Durable Keep (durable.py): Keep writes ~/.config/hypr/displayctl/monitors.lua
  (loaded by `pcall(dofile, ...)` at the end of hyprland.lua), then ONE
  `hyprctl reload`, then verifies. Hyprland does NOT auto-reload our dofile'd
  file; `hyprctl reload` re-applies rules but not the hyprland.start autostarts.
  Layouts now survive reloads. `displayctl.py persist` / `undo`.
- Hyprland Lua API facts (0.56.2, found from L11 run 2 on 2026-09-28):
  - `hl.dsp.reload_config` does NOT exist. The template called it from
    c586405 until the L11-run-2 fix: every rescue reload threw inside a pcall
    and never happened (the log showed "reload (...)" but never a "load:"
    after it). Reload from Lua with `hl.exec_cmd("hyprctl reload")`.
  - A runtime `hl.monitor({...})` (from a handler or timer) raises no
    error but is NOT applied until the next reload or hotplug. Only a reload
    changes outputs.
  - When the last real monitor goes, Hyprland adds a `FALLBACK` output with
    an empty description. eDP-1 while disabled is not in `hl.get_monitors()`.
  - `hyprctl eval` prints only "ok", never a return value. To see one, have
    the Lua write it to a file.
  - L11 run 1's "stale list" diagnosis is probably incomplete: its reload
    never ran either.
- The mock in durable.py (MOCK_DRIVER) must follow the REAL API. The old mock
  invented `hl.dsp.reload_config`, which is why U1 passed while the live file
  was broken. tests/test_durable.py REAL_HL / REAL_HL_DSP is the allow-list
  of names the template may call (comments are ignored). After a Hyprland
  update, regenerate it from the live session and paste the two lines in:
  ```
  hyprctl eval "local f=io.open('/tmp/hl-api.txt','w'); local function keys(t) local r={} for k in pairs(t) do r[#r+1]=tostring(k) end table.sort(r) return table.concat(r,' ') end; f:write('hl: ',keys(hl),'\n'); f:write('dsp: ',keys(hl.dsp),'\n'); f:close()"
  cat /tmp/hl-api.txt
  ```
  (It is read-only: it only lists names.) If a name the template uses has
  gone, the allow-list test fails. Fix the template and the mock together.
- Generated monitors.lua logs its own errors as "ERROR in <where>: ..." in
  $XDG_RUNTIME_DIR/displayctl-monitors.log (load, timers, event handlers). A
  syntax error can only be seen by hyprland.lua's pcall around dofile.

## Stack
Python 3, PyGObject (GTK4 + libadwaita), pywayland.
Backend protocol: wlr-output-management-unstable-v1
(XML at /usr/share/wlr-protocols/unstable/). Venv: .venv (system site packages).
Files: backend.py (Wayland), displayctl.py (CLI), watchdog.py (revert
process), safety.py (lock + watchdog helpers, shared by CLI and GUI),
layout.py (GUI edit + arrange logic, no GTK/Wayland; tests in tests/, run
`.venv/bin/python -m unittest discover -s tests`), gui.py (GTK4 GUI; Wayland
fd in the GLib main loop, one persistent connection that watches, applies
and reverts; `--debug` logs fd wakeups, refreshes, redraws, apply steps).
GTK gotcha: never rebuild a widget from inside its own change signal (hung
GTK); gui.py handles edits from GLib.idle_add.

## Next steps (as of 2026-09-23)
- Dev sessions: `pkill kanshi` first (it's autostarted and blocks other tools),
  and don't use nwg-displays alongside (it's another output-management tool).
1. DONE 2026-09-23: drag-to-arrange tests 2–8 passed live on the real GUI
   (tests/gui_live_arrange.py, 41/41 checks + SIGINT test; DP-3 returns to
   0,0 when eDP-1 is turned off; 15 s timeout measured 15.006 s).
2. Durable Keep: implemented; U1/U2 + L1-L9 passed; L11 PASSED 2026-09-28
   (run 3, c5fcfab live, kanshi stopped, coredumpctl clean). Already wired
   in: GUI Keep -> persist_async() (gui.py), CLI Keep -> make_durable()
   (displayctl.py). The live file has the guarded eDP-1 disable + undock
   handlers (Samsung, 1440p@200 at 0,0). Keeps with eDP-1 off are OK again.
   L11 run 3 log summary (two unplug/replug cycles, 13:50 and 13:52):
   - Unplug: event carried the description; "remembered as gone"; the direct
     enable had no effect (as expected on 0.56.2); 1 s later "reload 1/10"
     -> "load: monitors [FALLBACK=] -> panel on" -> "event monitor.added
     eDP-1". Panel on about 1-2 s after the unplug; no further reloads.
   - Gone-memory expiry (second cycle, replugged after 50 s): the re-check at
     +31 s saw eDP-1 on ("real monitors on 1") and correctly did nothing.
   - Replug: "reload count reset (monitor DP-3 added/removed)", 1 s later
     "reload 1/10" -> "load: ... -> panel OFF" -> eDP-1 removed. Docked
     layout back, one reload.
   - No ERROR lines. Each cycle: 1 reload out, 1 reload back.
   - "event monitor.removed FALLBACK" appears TWICE per cycle. Hyprland
     emits it twice. Our handlers aren't doubled: every other event is
     logged once, and run 2 shows the same double with a single load and no
     reloads. Harmless: only duplicate re-checks.
   History: L11 run 1 (2026-09-23) and run 2 (2026-09-28, afb4275) FAILED.
   Run 2 (panel stayed black after the unplug). Root
   cause: the rescue reload called the nonexistent hl.dsp.reload_config (see
   "Hyprland Lua API facts"). Fixed to `hyprctl reload`, with errors now
   logged. Mock and tests follow the real API. Reload cap: at most 10
   file-started reloads in a row (count in $XDG_RUNTIME_DIR/
   displayctl-reload-count, since every reload wipes the Lua state), then the
   log says "giving up after 10 reloads". Any real or unidentifiable monitor
   added/removed resets the count. The panel's own events and FALLBACK/
   HEADLESS do NOT reset it, because they happen because of our reloads.
   The cap counts every reload the file starts, not only panel rescues:
   normal dock/undock uses 1-2, and each plug event resets it. Manual
   `hyprctl reload` isn't counted. To clear the count by hand:
   `rm "$XDG_RUNTIME_DIR/displayctl-reload-count"`. 75 tests pass.
   Still to do: L10 (startup, with kanshi commented out).
3. Then Phase 4: the dock daemon (rule-based dock switching in Lua on
   Hyprland; design in docs/hyprland-persistence.md), replacing kanshi.
- Later: docs/upstream-issue.md is a DRAFT; review before filing.
- Later: test i3 in tests/test_durable.py disables the "gone" memory by
  text-replacing `and not is_gone` in the generated Lua (it asserts the
  text exists, but it's fragile). Replace it with a proper test-only flag
  in GUARD_TEMPLATE.

## Rules
- HARD RULE on Hyprland: connect -> apply -> verify -> disconnect. Never stay
  connected after applying. While the client that applied the last change is
  connected, Hyprland ignores every other client's changes ("succeeded", no
  effect) and sends that client stale state. displayctl closes right after
  apply; the GUI reconnects after every Keep/Revert. kanshi VIOLATES this (it
  stays connected after applying), so it blocks all other tools while it
  runs; that likely explains the first night's "revert didn't take" failures.
- Never edit ~/.config/hypr/hyprland.lua; the app owns only its own monitors.lua
- Never `pkill -f`/`pgrep -f` with a pattern that can match your own shell
  command line (it killed the test shell twice): use exact PIDs.
- Any apply must be revertible; test before apply. Display changes go through
  displayctl.py (snapshot + watchdog), never ad-hoc scripts
- Generated monitors.lua must never disable eDP-1 UNCONDITIONALLY (undocked
  boot = no screen). The only allowed disable is the guarded one: off only
  while a kept-with monitor is connected, with the undock handlers (L11
  passed). Rule-based dock switching beyond that belongs to the dock daemon.
- Explain code with comments — I'm learning
