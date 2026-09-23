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
@200`, `Modesetting eDP-1 @144`). Consequence: on Hyprland, `--dry-run` could change
the screen, and every `set` passed through the rule state between its
`test()` and `apply()`.

**Decision (2026-09-23):** displayctl skips `test()` when
`HYPRLAND_INSTANCE_SIGNATURE` is set. On Hyprland, validation is local
(mode exists on the head, scale > 0, not every output off) plus the
protocol-vs-hyprctl cross-check; `--dry-run` sends no configuration at all
(verified with a trace: zero configuration requests). Other compositors
still get `test()`.

**Worth reporting upstream** (hyprwm/Hyprland): the protocol says `test()`
must only validate; on 0.56.2 it re-applies config rules. The repro above
(disable an output via wlr-output-management, then send any test()) is
small enough for an issue.

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

### No 'done' for clients that only watch

With gui.py open (its own connection, not applying anything),
`displayctl.py set DP-3 --mode 2560x1440@144` worked (hyprctl: 143.99), and
the GUI's connection received the head updates (`enabled`, `position`,
`current_mode` = the 144 Hz mode, ...) but **no
`zwlr_output_manager_v1.done`**. The client that applies a change does get
`done`. The protocol requires `done` after every batch of changes, so this is
another Hyprland deviation (also worth reporting upstream).
- The GUI's open connection does NOT block other clients' applies.
- Workaround in gui.py: after dispatching a batch, if no `done` arrived,
  refresh anyway (the connection carries only output-management events and
  Hyprland sends each change as one burst). Verified: the GUI followed
  200 -> 144 -> 200 live.
- Consequence for later: a watching client's `serial` stays at its last
  `done`, so it's stale after changes made by others (kanshi, displayctl,
  hotplug). When the GUI applies, it may need a fresh serial, e.g. by
  reconnecting the manager, or Hyprland may not check serials at all.
  To test before the GUI applies anything.

### Stale serials are ignored

Test (`displayctl.py set DP-3 --mode 2560x1440@180 --stale-serial-test`):
connection A opened with serial 461; a separate displayctl run changed DP-3
to 144 Hz and kept it (fresh serial now 465); A then applied 180 Hz with
`create_configuration(461)`. Hyprland answered **succeeded** and the change
**took effect** (verified on a fresh connection); the watchdog reverted it
(verified), then DP-3 was restored to 200 Hz. The protocol says a stale serial
must be answered `cancelled`: Hyprland 0.56.2 doesn't check serials.

## GUI apply strategy (decided from the tests above)

**On Hyprland: keep the single persistent connection** for watching, applying
and reverting. A fresh connection per apply buys nothing, because serials
aren't checked. Instead the safety comes from:
1. the live-refreshed state (gui.py refreshes on `done` and on done-less
   update batches), so what the user edits starts from the current layout;
2. the protocol-vs-hyprctl cross-check right before every apply (as
   displayctl does); refuse and show both views if they disagree;
3. every configuration restating all enabled heads in full;
4. verify after apply and after revert on a fresh read.

Because Hyprland ignores serials, it will NOT stop the GUI from applying on
top of a change another client made a moment ago. Steps 1 and 2 are what
prevent that; don't drop them.

**Revert:** through the same connection that applied (tested: works; another
client's revert is ignored while it's connected). The watchdog, armed before
each apply, is only the crash fallback (the GUI's connection closes when it
dies, then the watchdog's revert works).

**Other compositors** (wlroots-based ones check serials): if an apply comes
back `cancelled`, re-read the state (a watcher there does get `done`, so the
serial is fresh after dispatching), show the user the new layout, and let
them retry. Don't retry automatically: the layout they edited may no longer
make sense.

### A GUI that hangs (not crashes)

If gui.py crashes or is closed, its connection closes and the watchdog's
revert works. If it HANGS with the connection still open, Hyprland would
ignore the watchdog's revert. So the GUI passes its PID to the watchdog, and
on its deadline the watchdog `kill -9`s it (after checking /proc that the PID
is still gui.py), waits for it to exit, then reverts.

### An idle applying client blocks everyone else (confirmed)

2026-09-23, nothing else connected, kanshi off, sampled fresh protocol reads
+ hyprctl every 0.3 s:
- (a) no GUI open: `displayctl.py set eDP-1 --enable` (same-connection
  revert after 5 s) worked: eDP-1 on, then off, stayed off.
- (b) GUI applied DP-3 144 Hz, pressed Keep and sat idle: the same command
  was answered "succeeded" but eDP-1 never came on (both views).
So while a client that applied the last change stays connected, other
clients' configurations are accepted and ignored. Matters for Phase 4: an
open GUI blocks kanshi, displayctl and a dock daemon. (Also: displayctl
doesn't verify its own apply, so it can't notice; it only verifies reverts.)

### Root cause of test 9: the applying client gets the OLD state

Traced through the real GUI path (hook: enable eDP-1 -> Apply -> Revert).
The displays did the right thing (eDP-1 on at +0.5 s, off after Revert,
stayed off). But on the GUI's own connection Hyprland sent, for each commit:
`done` immediately, then with `succeeded()` the head state from BEFORE the
commit (`enabled(0)` after enabling, `enabled(1)` after disabling), and
nothing afterwards. So the applying client's view is one change behind.
Fresh connections (verify, hyprctl, the CLI, the watchdog) see the truth.
That's why "verify after revert: ok" was followed by a GUI refresh showing
eDP-1 on. Not fixed yet (design decision pending).

### Crash and hang fallbacks (verified on real displays)

- Test 8, GUI `kill -9` during the Keep dialog: the watchdog saw the pipe
  close and restored 200 Hz within 0.3 s, verified twice.
- Hang, GUI `SIGSTOP`ped during the dialog: at the watchdog's deadline
  (35 s after "applied") it killed the frozen GUI, reverted, verified twice.
  First attempt failed: the watchdog identified the GUI by "gui.py" in its
  command line and refused to kill the test hook, and with the frozen
  client connected Hyprland ignored its revert (REVERT FAILED, recovered by
  hand). Fixed: it now identifies the process by PID + start time
  (/proc/PID/stat field 22), recorded when the watchdog starts.

## Phase 4 design: rule-based dock switching on Hyprland

Goal: on Hyprland the config rules are the single source of truth for
which outputs are on. Dock switching (the "auto dock profiles" goal) is
done by Lua in the config reacting to hotplug, not by a protocol client
(kanshi or a daemon) racing the rules.

Verified API (hyprland-wiki source, commit c04680a, 2026-09-18; the
installed 0.56.2 binary contains the same event names):

- `hl.on(name, fn)` subscribes to an event. Subscriptions are dropped and
  recreated on every config reload (LuaEventHandler.hpp).
- `monitor.added` (arg: Monitor): "a monitor is connected and ready".
- `monitor.removed` (arg: Monitor): "disconnected and removed".
- `monitor.layout_changed` (no args): any arrangement change, including a
  config reload with different rules.
- `config.reloaded` (no args): after the config is reloaded and applied.
- `hl.get_monitors({ all? })`, `hl.get_monitor(selector)` query monitors.
- Rules: `hl.monitor({ output = "DP-3", mode = "2560x1440@200",
  position = "0x0", scale = 1, transform = 0 })`;
  disable with `hl.monitor({ output = "eDP-1", disabled = true })`;
  catch-all `hl.monitor({ output = "", mode = "preferred", position =
  "auto", scale = 1 })`.
- Reload: `hyprctl reload`, or the `reload_config()` dispatcher. Hyprland
  also auto-reloads when a config file is saved (`disable_autoreload`).

Sketch (to be written in our monitors.lua, loaded by the user's dofile):

```lua
-- Pseudocode shape only; field names on Monitor still to be checked.
local function apply_dock_profile()
  local external = false
  for _, m in ipairs(hl.get_monitors({ all = true })) do
    if m.name ~= "eDP-1" then external = true end
  end
  -- docked: rules for the external layout, laptop panel per the chosen profile
  -- undocked: eDP-1 on, preferred (never a disable rule for eDP-1)
end
hl.on("monitor.added", apply_dock_profile)
hl.on("monitor.removed", apply_dock_profile)
```

Open points before implementing:
- Exact fields of the Lua Monitor object (name, description, make/model?).
- Whether calling `hl.monitor(...)` inside an event handler takes effect
  immediately or needs `reload_config()`, and whether that reload loops
  back into `monitor.layout_changed` / our handlers (guard against loops).
- The eDP-1 rule: disabling eDP-1 while docked must be conditional inside
  the handler, never a static rule, so an undocked boot always has a
  screen (hard rule above still applies).
- Does auto-reload fire when displayctl rewrites monitors.lua? If so, the
  write itself is the reload.
- kanshi stays for daily use until this exists; pkill it in dev sessions.
