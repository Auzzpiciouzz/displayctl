# DRAFT: wlr-output-management deviations in Hyprland 0.56.2

Status: **draft, not filed.** Review, trim and re-test on the latest
Hyprland before opening an issue at hyprwm/Hyprland. Probably better as
several small issues than one big one.

## Environment

- Hyprland 0.56.2 (commit efb50993780079460b0cbed1363e2166a2de1d9f), Lua config
- Arch Linux, HP Omen 16: NVIDIA (open-dkms) primary + Intel iGPU
- Outputs: eDP-1 (laptop, 1920x1080@144), DP-3 (Samsung LS27FG53x,
  2560x1440@200)
- Config: only the catch-all rule
  `hl.monitor({ output = "", mode = "preferred", position = "auto", scale = 1 })`
- Protocol: zwlr_output_manager_v1 version 4. Observed with `WAYLAND_DEBUG=1`,
  a small pywayland client (displayctl, https://… — link if public) and
  `hyprctl monitors all -j` as a second view.
- No other output-management clients running (kanshi stopped).

**Note on the repro steps:** everything below was observed with displayctl
(a pywayland client) plus hyprctl. The `wlr-randr` commands are suggested
equivalents for maintainers and have NOT been verified yet; check each one
before filing.

Each item: what the protocol says, what happens, minimal repro.

---

## 1. `configuration.test()` has side effects

**Protocol:** test() "won't be applied, but will only be validated".

**Observed:** test() makes Hyprland re-apply its config monitor rules to all
outputs. An output disabled earlier through the protocol comes back on.
The test configuration itself is not applied.

**Repro:**
1. Disable eDP-1 through the protocol (e.g. `wlr-randr --output eDP-1 --off`).
   `hyprctl monitors all` shows `disabled: true`.
2. From any client, create a configuration (any content) and send `test()`.
3. eDP-1 is enabled again (catch-all rule); the Hyprland log shows
   `Modesetting eDP-1 …` right after the test.

## 2. Properties not set in a configuration are reset to the config rules

**Protocol (implied):** a head enabled with `enable_head` but without
`set_*` requests keeps its current properties.

**Observed:** on a commit that enables/disables another output, heads sent
as "enabled, no properties" are reset to what the config rules give.

**Repro:**
1. Set DP-3 to 144 Hz at 0,0 through the protocol.
2. Apply a configuration that enables eDP-1 (with mode/position) and
   includes DP-3 only as `enable_head(DP-3)` with no `set_*` requests.
3. DP-3 is now at its preferred 200 Hz and moved to an auto position
   (4480,0 here), per the catch-all rule.

## 3. While the last applying client is connected, other clients' configurations are ignored

**Protocol:** a configuration is answered `succeeded`, `failed` or
`cancelled`; `succeeded` means the changes were applied.

**Observed:** after client A applies a configuration and stays connected
(even idle), a configuration from client B is answered `succeeded` but
nothing changes (no modeset in the log; both the protocol state and hyprctl
unchanged). Once A disconnects, B's configurations work again. Same for
A's own later configurations: those do work.

**Repro:**
1. Client A: apply DP-3 = 144 Hz, keep the connection open.
2. Client B (e.g. `wlr-randr --output eDP-1 --on`, or any second client):
   enable eDP-1. Answer: `succeeded`. eDP-1 stays off.
3. Disconnect A; repeat step 2: eDP-1 turns on.

Consequence: long-running clients such as kanshi silently block every other
output-management client while they run.

## 4. The applying client is sent the state from before its own commit

**Protocol:** "If the current configuration has changed, events to describe
the changes will be sent followed by a wlr_output_manager.done event."

**Observed:** for the applying client, Hyprland sends `done` immediately
after `apply()`, then (together with `succeeded`) head events describing
the state *before* the commit, and nothing afterwards. The client's view
stays one change behind.

**Repro (WAYLAND_DEBUG=1 on the applying client):**
1. eDP-1 disabled. Apply a configuration enabling eDP-1.
2. Trace: `-> apply()`, `manager.done(N)`, then `head(eDP-1).enabled(0)`,
   `configuration.succeeded()`. No later `enabled(1)`.
3. A new connection (or hyprctl) shows eDP-1 enabled.
4. Disable it again: the applying client now receives `enabled(1)`.

## 5. Watching clients get no `done`, and stale enable/disable state

**Protocol:** `done` is sent after every batch of head changes, to every
bound manager.

**Observed:**
- A client that only watches (never applied) receives head events for
  another client's change but **no** `manager.done`.
- For enabling/disabling an output, those pushed head events describe the
  previous state (one change behind), like item 4. (A plain mode change
  was reported correctly.)

**Repro:** keep a watching client connected with WAYLAND_DEBUG=1; from
another client enable eDP-1: the watcher receives `enabled(0)` and no
`done`. Disable it: the watcher receives `enabled(1)`.

## 6. Serials are not checked

**Protocol:** `create_configuration(serial)` with an outdated serial must
be answered `cancelled`.

**Observed:** a configuration created with a stale serial (461, while the
current one was 465 after another client's change) is answered `succeeded`
and applied.

**Repro:**
1. Client A binds the manager, notes serial S.
2. Client B changes a mode (serial advances).
3. Client A: `create_configuration(S)` with some change, `apply()`:
   `succeeded`, and the change is applied.

---

## Not included (not reproduced)

- After a boot, the protocol once reported eDP-1 enabled while hyprctl said
  disabled (kanshi autostarted). Could not reproduce mid-session; possibly a
  startup race. Needs a clean repro before reporting.
