"""Drive gui.py's editing + Apply logic with FAKE apply/watchdog/verify.

Nothing here can change a display: every call that would send a
configuration or spawn the real watchdog is replaced before use.

Needs a live Hyprland session (it opens the real GUI, reads the real state,
and uses hyprctl + grim for window screenshots). Not part of the unittest suite.
Run:  .venv/bin/python tests/gui_apply_harness.py /tmp/some-dir
It prints PASS/FAIL per check and saves screenshots into that directory.
"""
import json, os, subprocess, sys, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')
from gi.repository import Adw, GLib, Gtk
import gui, backend, safety, layout

S = sys.argv[1]
gui.CONFIRM_SECONDS = 3  # short countdown for the timeout test
results = []

def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""), flush=True)

# ---- fakes ----------------------------------------------------------------
class FakeWatchdog:
    def __init__(self): self.cmds = []; self.stdin = self
    def write(self, s): self.cmds += [c for c in s.split("\n") if c]
    def flush(self): pass
    def poll(self): return 0
watchdogs = []
def fake_start_watchdog(lock, seconds):
    w = FakeWatchdog(); w.seconds = seconds; watchdogs.append(w); return w
safety.start_watchdog = fake_start_watchdog

applied = []            # (action, desired) the GUI tried to send
apply_results = []      # scripted results, popped per call ("succeeded" default)
def fake_run_configuration(desired, action):
    applied.append((action, json.loads(json.dumps(desired))))
    return apply_results.pop(0) if apply_results else "succeeded"

verify_results = []     # scripted verify_state results (problem lists)
def fake_verify(desired, settle=1.0):
    return (verify_results.pop(0) if verify_results else [], [])
backend.verify_state = fake_verify

real_disagreements = backend.hyprland_disagreements

import logging; logging.basicConfig(level=logging.DEBUG, format="%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S"); logging.getLogger("displayctl.gui").setLevel(logging.DEBUG)
app = gui.DisplayctlApp()
W = lambda: app.window

def geo():
    for c in json.loads(subprocess.check_output(["hyprctl", "clients", "-j"])):
        if c.get("class") == gui.APP_ID:
            (x, y), (w, h) = c["at"], c["size"]; return f"{x},{y} {w}x{h}"
def shot(name): subprocess.run(["grim", "-g", geo(), f"{S}/{name}.png"], check=True)

def select(name):
    boxes = {n: (x, y, w, h) for n, x, y, w, h in W().canvas._hit_boxes}
    x, y, w, h = boxes[name]; W().canvas._on_click(None, 1, x + w / 2, y + h / 2)
def settle():
    """Run pending main-loop work (edits are handled from idle callbacks)."""
    ctx = GLib.MainContext.default()
    for _ in range(50):
        if not ctx.pending(): break
        ctx.iteration(False)
def pick(key, label):
    row = W().details.combos[key]; model = row.get_model()
    labels = [model.get_string(i) for i in range(model.get_n_items())]
    row.set_selected(labels.index(label)); settle()
def use(on):
    W().details.use_row.set_active(on); settle()
def labels(key):
    m = W().details.combos[key].get_model(); return [m.get_string(i) for i in range(m.get_n_items())]
def dialog(): return W().applier.dialog

# ---- scenarios -------------------------------------------------------------
def s_start():
    app.om.run_configuration = fake_run_configuration   # BEFORE anything can apply
    live = {h.name: h.to_state() for h in app.om.heads}
    check("starts with no pending edits, Apply/Reset disabled",
          not W().pending and not W().apply_button.get_sensitive() and not W().reset_button.get_sensitive())
    select("DP-3")
    check("panel shows DP-3 settings", W().details.get_visible_child_name() == "details")
    check("resolution list is unique", len(labels("resolution")) == len(set(labels("resolution"))))
    check("refresh list is for 2560x1440", labels("refresh") == ["200 Hz", "179.999 Hz", "143.999 Hz", "120 Hz", "59.951 Hz"],
          labels("refresh"))
    pick("resolution", "1920 × 1080")
    s = W().pending.get("DP-3")
    check("resolution edit is pending", s and s["mode"]["width"] == 1920)
    check("refresh list refiltered for 1920x1080", "119.879 Hz" in labels("refresh") and "179.999 Hz" not in labels("refresh") or "180 Hz" in labels("refresh"),
          labels("refresh"))
    check("refresh kept closest to 200 Hz", s["mode"]["refresh_mhz"] == 200000, s["mode"])
    pick("scale", "150%"); pick("rotation", "90°")
    s = W().pending["DP-3"]
    check("scale + rotation pending", s["scale"] == 1.5 and s["transform"] == 1)
    check("live state untouched", {h.name: h.to_state() for h in app.om.heads} == live)
    check("Apply/Reset enabled", W().apply_button.get_sensitive() and W().reset_button.get_sensitive())
    GLib.timeout_add(600, lambda: (shot("t1-preview"), False)[1]); return False

def s_guard_all_off():
    W().clear_pending(); select("DP-3")
    use(False)          # the only display on
    check("turning off the last display is refused", "DP-3" not in W().pending)
    check("switch put back on", W().details.use_row.get_active())
    GLib.timeout_add(500, lambda: (shot("t2-refused-toast"), False)[1]); return False

def s_enable_edp_then_off_dp3():
    select("eDP-1"); use(True)
    s = W().pending.get("eDP-1")
    check("enabling eDP-1 is pending with preferred mode at 2560,0",
          s and s["enabled"] and s["position"] == [2560, 0] and s["mode"]["refresh_mhz"] == 144005, s)
    select("DP-3"); use(False)
    check("now DP-3 may be turned off", W().pending.get("DP-3", {}).get("enabled") is False)
    GLib.timeout_add(500, lambda: (shot("t3-swap-preview"), False)[1])
    GLib.timeout_add(700, s_reset); return False
def s_reset():
    W().reset_button.emit("clicked")
    check("Reset clears everything", W().pending == {} and not W().apply_button.get_sensitive())
    return False

def s_crosscheck_refuses():
    select("DP-3"); pick("refresh", "143.999 Hz")
    backend.hyprland_disagreements = lambda heads: ["eDP-1: protocol says enabled=True, hyprctl says enabled=False"]
    n_applied, n_wd = len(applied), len(watchdogs)
    W().apply_button.emit("clicked")
    backend.hyprland_disagreements = real_disagreements
    check("cross-check mismatch refuses: nothing applied, no watchdog",
          len(applied) == n_applied and len(watchdogs) == n_wd and not W().applier.busy)
    GLib.timeout_add(400, lambda: (shot("t4-refused-crosscheck"), False)[1]); return False

def s_close_alert():
    # close the refusal alert (it's the window's visible dialog)
    d = W().get_visible_dialog()
    if d: d.force_close()
    return False

def s_apply_keep():
    verify_results[:] = [[]]
    W().apply_button.emit("clicked")
    wd = watchdogs[-1]
    check("apply sent ONE full config with every head", applied[-1][0] == "apply" and set(applied[-1][1]) == {"DP-3", "eDP-1"})
    check("watchdog armed with extended deadline", wd.seconds == gui.CONFIRM_SECONDS + gui.WATCHDOG_EXTRA_SECONDS)
    check("watchdog told 'applied'", wd.cmds == ["applied"], wd.cmds)
    check("busy: editing disabled", W().applier.busy and not W().details.get_sensitive())
    return False

def s_keep_dialog_shown():
    check("keep dialog shown", dialog() is not None)
    body = dialog().get_body()
    check("dialog has countdown + Hyprland note", "Reverting to the previous settings in" in body and "Hyprland reloads" in body, body)
    shot("t5-keep-dialog")
    dialog().emit("response", "keep"); dialog().force_close() if dialog() else None
    wd = watchdogs[-1]
    check("Keep: watchdog stands down", wd.cmds == ["applied", "keep"], wd.cmds)
    check("Keep: pending cleared, not busy", W().pending == {} and not W().applier.busy)
    return False

def s_apply_timeout():
    select("DP-3"); pick("refresh", "143.999 Hz")
    verify_results[:] = [[], []]   # apply verify ok, revert verify ok
    W().apply_button.emit("clicked")
    return False
def s_after_timeout():
    wd = watchdogs[-1]
    reverted = applied[-1]
    check("timeout reverted through OUR connection with the snapshot",
          reverted[0] == "apply" and reverted[1]["DP-3"]["mode"]["refresh_mhz"] == 200000, reverted[1].get("DP-3"))
    check("timeout: watchdog told applied then keep (after verified revert)", wd.cmds == ["applied", "keep"], wd.cmds)
    check("timeout: not busy, pending cleared", not W().applier.busy and W().pending == {})
    return False

def s_apply_esc():
    select("DP-3"); pick("refresh", "143.999 Hz")
    verify_results[:] = [[], []]
    W().apply_button.emit("clicked"); return False
def s_press_esc():
    d = dialog(); d.close()   # like Esc: triggers the close response ("revert")
    return False
def s_after_esc():
    wd = watchdogs[-1]
    check("Esc reverts: revert applied, watchdog applied+keep", applied[-1][1]["DP-3"]["mode"]["refresh_mhz"] == 200000 and wd.cmds == ["applied", "keep"], wd.cmds)
    return False

def s_apply_failed():
    select("DP-3"); pick("refresh", "143.999 Hz")
    apply_results[:] = ["failed", "succeeded"]; verify_results[:] = [[]]
    n = len(applied); W().apply_button.emit("clicked")
    check("failed apply triggers an immediate self-revert", len(applied) == n + 2 and applied[-1][1]["DP-3"]["mode"]["refresh_mhz"] == 200000)
    return False
def s_after_failed():
    wd = watchdogs[-1]
    check("failed: watchdog never told 'applied', stands down after verified revert", wd.cmds == ["keep"], wd.cmds)
    return False

def s_apply_cancelled():
    select("DP-3"); pick("refresh", "143.999 Hz")
    apply_results[:] = ["cancelled"]; n = len(applied)
    W().apply_button.emit("clicked")
    wd = watchdogs[-1]
    check("cancelled: nothing reverted, watchdog 'keep', edits kept for retry",
          len(applied) == n + 1 and wd.cmds == ["keep"] and "DP-3" in W().pending and not W().applier.busy, wd.cmds)
    d = W().get_visible_dialog(); d and d.force_close()
    return False

def s_revert_fails_handover():
    # revert verify fails -> GUI must disconnect, hand over to watchdog, reconnect
    verify_results[:] = [[], ["DP-3: mode 2560x1440@143999 (want 2560x1440@200000)"]]
    old_om = app.om
    W().apply_button.emit("clicked")
    GLib.timeout_add(1300, lambda: (dialog().emit("response", "revert"), False)[1])
    GLib.timeout_add(3500, lambda: after_handover(old_om))
    return False
def after_handover(old_om):
    wd = watchdogs[-1]
    check("handover: watchdog told 'revert' after the GUI disconnected",
          wd.cmds == ["applied", "revert"] and old_om.display._ptr is None, wd.cmds)
    check("handover: reconnected with a new connection", app.om is not old_om and W().om is app.om)
    check("handover: not busy afterwards", not W().applier.busy)
    app.om.run_configuration = fake_run_configuration  # keep the new connection fake too
    return False

def s_end():
    print(f"\n{sum(ok for _, ok in results)}/{len(results)} checks passed", flush=True)
    app.quit(); return False

steps = [(1500, s_start), (1200, s_guard_all_off), (1200, s_enable_edp_then_off_dp3),
         (1200, s_crosscheck_refuses), (900, s_close_alert),
         (500, s_apply_keep), (1600, s_keep_dialog_shown),
         (600, s_apply_timeout), (8000, s_after_timeout),
         (600, s_apply_esc), (1800, s_press_esc), (1800, s_after_esc),
         (600, s_apply_failed), (1800, s_after_failed),
         (600, s_apply_cancelled),
         (800, s_revert_fails_handover), (5000, s_end)]

def run(i=0):
    delay, fn = steps[i]
    def go():
        try: fn()
        except Exception:
            traceback.print_exc(); check(f"{fn.__name__} raised", False)
        if i + 1 < len(steps): run(i + 1)
        else: pass
        return False
    GLib.timeout_add(delay, go)

app.connect("activate", lambda *_: run())
sys.exit(app.run([]))
