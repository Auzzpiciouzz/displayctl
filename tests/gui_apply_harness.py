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
def fake_start_watchdog(lock, seconds, kill_pid=None):
    w = FakeWatchdog(); w.seconds = seconds; w.kill_pid = kill_pid; watchdogs.append(w); return w
safety.start_watchdog = fake_start_watchdog

applied = []            # (action, desired) the GUI tried to send
apply_results = []      # scripted results, popped per call ("succeeded" default)
def fake_run_configuration(desired, action):
    applied.append((action, json.loads(json.dumps(desired))))
    return apply_results.pop(0) if apply_results else "succeeded"

# Class-level: the GUI opens NEW connections after every transaction, and
# each of them must be fake too.
backend.OutputManager.run_configuration = lambda self, desired, action: fake_run_configuration(desired, action)

verify_results = []     # scripted verify_state results (problem lists)
def fake_verify(desired, settle=1.0):
    return (verify_results.pop(0) if verify_results else [], [])
backend.verify_state = fake_verify

real_disagreements = backend.hyprland_disagreements

import logging; logging.basicConfig(level=logging.DEBUG, format="%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S"); logging.getLogger("displayctl.gui").setLevel(logging.DEBUG)
app = gui.DisplayctlApp()
# Run as a separate instance even if the real GUI is open (same app ID):
# otherwise GTK would just activate the running GUI and exit.
from gi.repository import Gio
app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
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
def respond(r):
    """Like clicking a dialog button: emit the response AND close the dialog."""
    d = dialog(); d.emit("response", r); d.force_close()

# ---- scenarios -------------------------------------------------------------
def s_start():
    check("apply is faked at class level", backend.OutputManager.run_configuration.__name__ == "<lambda>")
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
    check("watchdog knows the GUI's PID (to kill it if hung)", wd.kill_pid == os.getpid())
    check("watchdog told 'applied'", wd.cmds == ["applied"], wd.cmds)
    check("busy: editing disabled", W().applier.busy and not W().details.get_sensitive())
    return False

def s_keep_dialog_shown():
    global om_before_keep; om_before_keep = app.om
    check("keep dialog shown", dialog() is not None)
    body = dialog().get_body()
    check("dialog has countdown + Hyprland note", "Reverting to the previous settings in" in body and "Hyprland reloads" in body, body)
    shot("t5-keep-dialog")
    respond("keep")
    wd = watchdogs[-1]
    check("Keep: watchdog stands down", wd.cmds == ["applied", "keep"], wd.cmds)
    check("Keep: pending cleared, not busy", W().pending == {} and not W().applier.busy)
    check("Keep: GUI reconnected (fresh connection, old one closed)",
          app.om is not om_before_keep and om_before_keep.display._ptr is None and W().om is app.om)
    return False

def s_apply_timeout():
    global om_before_timeout; om_before_timeout = app.om
    select("DP-3"); pick("refresh", "143.999 Hz")
    verify_results[:] = [[], [], []]   # apply verify, revert verify, revert re-verify
    W().apply_button.emit("clicked")
    return False
def s_after_timeout():
    wd = watchdogs[-1]
    reverted = applied[-1]
    check("timeout reverted through OUR connection with the snapshot",
          reverted[0] == "apply" and reverted[1]["DP-3"]["mode"]["refresh_mhz"] == 200000, reverted[1].get("DP-3"))
    check("timeout: watchdog told applied then keep (after verified revert)", wd.cmds == ["applied", "keep"], wd.cmds)
    check("timeout: not busy, pending cleared", not W().applier.busy and W().pending == {})
    check("verified revert: GUI reconnected", app.om is not om_before_timeout and om_before_timeout.display._ptr is None)
    return False

def s_apply_esc():
    select("DP-3"); pick("refresh", "143.999 Hz")
    verify_results[:] = [[], [], []]
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
    apply_results[:] = ["failed", "succeeded"]; verify_results[:] = [[], []]
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

def s_second_check_catches_late_change():
    # like the reported bug: revert verifies OK, but 2 s later the state is wrong
    select("DP-3"); pick("refresh", "143.999 Hz")
    verify_results[:] = [[], [], ["eDP-1: enabled True (want False)"]]
    global old_om2; old_om2 = app.om
    W().apply_button.emit("clicked")
    GLib.timeout_add(1300, lambda: (respond("revert"), False)[1])
    return False
def s_after_second_check():
    wd = watchdogs[-1]
    check("second check catches a late re-enable -> handover to watchdog",
          wd.cmds == ["applied", "revert"] and old_om2.display._ptr is None, wd.cmds)
    return False

def s_revert_fails_handover():
    # revert verify fails -> GUI must disconnect, hand over to watchdog, reconnect
    verify_results[:] = [[], ["DP-3: mode 2560x1440@143999 (want 2560x1440@200000)"]]
    old_om = app.om
    W().apply_button.emit("clicked")
    select("DP-3"); pick("refresh", "143.999 Hz")
    GLib.timeout_add(1300, lambda: (respond("revert"), False)[1])
    GLib.timeout_add(3500, lambda: after_handover(old_om))
    return False
def after_handover(old_om):
    wd = watchdogs[-1]
    check("handover: watchdog told 'revert' after the GUI disconnected",
          wd.cmds == ["applied", "revert"] and old_om.display._ptr is None, wd.cmds)
    check("handover: reconnected with a new connection", app.om is not old_om and W().om is app.om)
    check("handover: not busy afterwards", not W().applier.busy)
    return False

# ---- drag-to-arrange (Phase 3 step 4) ----
class FakeGesture:
    def __init__(self): self.state = None
    def set_state(self, st): self.state = st
def center(name):
    boxes = {n: (x, y, w, h) for n, x, y, w, h in W().canvas._hit_boxes}
    x, y, w, h = boxes[name]; return x + w / 2, y + h / 2
def redraw():
    """Wait until the canvas has actually drawn again (hit boxes up to date).
    A real user can't start a drag before the next frame; the harness can."""
    import time
    c = W().canvas; before = c._view; c._view = None; c.queue_draw()
    ctx = GLib.MainContext.default(); end = time.monotonic() + 1.0
    while c._view is None and time.monotonic() < end:
        ctx.iteration(False)
    if c._view is None: c._view = before
def drag(name, to_logical, finish=True):
    """Drag `name` so its top-left would land at to_logical (before snapping)."""
    redraw()
    c = W().canvas; g = FakeGesture()
    x, y = center(name); c._on_drag_begin(g, x, y)
    if g.state is not None: return g
    factor = c._drag["view"][0]; sx, sy = c._drag["start"]
    dx, dy = (to_logical[0] - sx) * factor, (to_logical[1] - sy) * factor
    c._on_drag_update(g, dx / 2, dy / 2); c._on_drag_update(g, dx, dy)
    if finish: c._on_drag_end(g, dx, dy)
    return g
def pos(name): return tuple(W().effective()[name]["position"])

toasts = []
log_lines = []
class ListHandler(__import__("logging").Handler):
    def emit(self, record): log_lines.append(record.getMessage())
__import__("logging").getLogger("displayctl.gui").addHandler(ListHandler())

def box(name):
    return next(tuple(round(v) for v in b[1:]) for b in W().canvas._hit_boxes if b[0] == name)

def s_drag_disabled_denied():
    W().clear_pending()
    real_toast = W().toast
    W().toast = lambda text: (toasts.append(text), real_toast(text))
    g = drag("eDP-1", (0, 2000))
    check("disabled monitor can't be dragged", g.state is not None and W().pending == {})
    return False

def s_single_monitor_hint():
    # only DP-3 is on: dragging it must do nothing but show the hint (once)
    redraw(); n = len(toasts); before = box("eDP-1")
    c = W().canvas; g = FakeGesture(); x, y = center("DP-3")
    c._on_drag_begin(g, x, y)
    c._on_drag_update(g, 2, 1)                # tiny: still a click, no hint
    hint_after_tiny = len(toasts) > n
    for d in (40, 120, 300): c._on_drag_update(g, d, 0)
    c._on_drag_end(g, 300, 0)
    check("single monitor: no hint for a tiny move (a click)", not hint_after_tiny)
    check("single monitor: drag refused with one hint",
          toasts[n:] == ["Turn on another display to arrange them."] and W().pending == {}, toasts[n:])
    redraw()
    check("single monitor: nothing moved", box("eDP-1") == before)
    return False

def s_disabled_row_fixed():
    # fixed-row code path: fake an in-progress drag of DP-3 far to the right
    redraw(); c = W().canvas; before = box("eDP-1")
    c._drag = {"name": "DP-3", "start": (0, 0), "view": c._view, "preview": (6000, 0),
               "frames": 0, "disabled_row": c._disabled_row()}
    redraw_keep = c._view; c.queue_draw()
    ctx = GLib.MainContext.default()
    for _ in range(30): ctx.iteration(False)
    during = box("eDP-1")
    c._drag = None
    check("disabled row stays put while another monitor is dragged", during == before, (before, during))
    return False

def s_drag_left():
    select("eDP-1"); use(True)                         # pending: eDP-1 on at 2560,0
    check("enabled eDP-1 placed right of DP-3", pos("eDP-1") == (2560, 0))
    redraw(); dp3_before = box("DP-3")
    drag("eDP-1", (-2400, 30), finish=False)          # far left, a bit low
    redraw_frames = [l for l in log_lines if l.startswith("redraw")]
    ctx = GLib.MainContext.default()
    for _ in range(30): ctx.iteration(False)
    check("while dragging eDP-1, DP-3 stays still on screen", box("DP-3") == dp3_before,
          (dp3_before, box("DP-3")))
    n_redraw = sum(1 for l in log_lines if l.startswith("redraw"))
    W().canvas._on_drag_update(FakeGesture(), -3000, 20); W().canvas._on_drag_update(FakeGesture(), -3100, 25)
    for _ in range(30): ctx.iteration(False)
    check("no per-frame 'redraw' log lines during a drag",
          sum(1 for l in log_lines if l.startswith("redraw")) == n_redraw)
    c = W().canvas; factor = c._drag["view"][0]; sx, sy = c._drag["start"]
    c._on_drag_end(FakeGesture(), (-2400 - sx) * factor, (30 - sy) * factor)
    check("drag end logs a frame summary", any("frames drawn" in l for l in log_lines))
    check("dragged left: eDP-1 normalised to 0,0, DP-3 to 1920,0 (no negatives)",
          pos("eDP-1") == (0, 0) and pos("DP-3") == (1920, 0), (pos("eDP-1"), pos("DP-3")))
    check("layout legal after drag", layout.arrangement_problems(W().effective()) == [])
    return False

def s_drag_preview_shot():
    drag("eDP-1", (800, 1700), finish=False)          # mid-drag: below DP-3
    p = W().canvas._drag["preview"]
    check("mid-drag preview is legal (below DP-3)", p[1] == 1440 and layout.arrangement_problems(
        layout._placed(W().effective(), "eDP-1", p)) == [], p)
    W().canvas.queue_draw()
    GLib.timeout_add(400, lambda: (shot("t6-drag-preview"), False)[1])
    GLib.timeout_add(700, lambda: (W().canvas._on_drag_end(FakeGesture(), 0, 0), False)[1])
    return False

def s_drag_anchor():
    W().clear_pending(); select("eDP-1"); use(True)   # eDP-1 at 2560,0 pending
    redraw(); edp_before = box("eDP-1")
    drag("DP-3", (1920 + 2560 + 100, 0), finish=False)  # drag the 0,0 monitor past eDP-1
    ctx = GLib.MainContext.default()
    for _ in range(30): ctx.iteration(False)
    check("while dragging DP-3 (at 0,0), eDP-1 stays still on screen", box("eDP-1") == edp_before,
          (edp_before, box("eDP-1")))
    c = W().canvas; factor = c._drag["view"][0]; sx, sy = c._drag["start"]
    c._on_drag_end(FakeGesture(), (1920 + 2560 + 100 - sx) * factor, 0)
    check("after release: eDP-1 at 0,0, DP-3 at 1920,0",
          pos("eDP-1") == (0, 0) and pos("DP-3") == (1920, 0), (pos("DP-3"), pos("eDP-1")))
    GLib.timeout_add(400, lambda: (shot("t7-after-anchor-drag"), False)[1])
    return False

def s_reflow_on_scale():
    W().clear_pending(); select("eDP-1"); use(True)
    select("DP-3"); pick("scale", "200%")             # DP-3 1280x720 logical
    check("scale change reflows neighbour (eDP-1 -> 1280,0)", pos("eDP-1") == (1280, 0), pos("eDP-1"))
    return False

def s_apply_after_drag():
    W().clear_pending(); select("eDP-1"); use(True); drag("eDP-1", (-2400, 0))
    verify_results[:] = [[]]
    n = len(applied); W().apply_button.emit("clicked")
    sent = applied[-1][1] if len(applied) > n else {}
    check("Apply sends the dragged positions (full config)",
          sent.get("eDP-1", {}).get("position") == [0, 0] and sent.get("DP-3", {}).get("position") == [1920, 0],
          {k: v.get("position") for k, v in sent.items()})
    return False
def s_keep_after_drag():
    d = dialog(); d and d.emit("response", "keep"); d and d.force_close()
    check("drag apply kept, not busy", not W().applier.busy)
    return False

def s_illegal_layout_refused():
    W().clear_pending(); select("eDP-1"); use(True)
    st = dict(W().pending["eDP-1"]); st["position"] = [3000, 0]    # a gap, injected directly
    W().pending["eDP-1"] = st; W().refresh()
    n = len(applied); W().apply_button.emit("clicked")
    check("gap in layout: Apply refused, nothing sent", len(applied) == n and not W().applier.busy)
    d = W().get_visible_dialog()
    check("refusal explains the arrangement", d is not None and "touch" in d.get_body(), d and d.get_body())
    GLib.timeout_add(300, lambda: (shot("t8-refused-gap"), d and d.force_close(), W().clear_pending(), False)[-1])
    return False

def s_end():
    print(f"\n{sum(ok for _, ok in results)}/{len(results)} checks passed", flush=True)
    app.quit(); return False

steps = [(1500, s_start), (1200, s_guard_all_off), (1200, s_enable_edp_then_off_dp3),
         (1200, s_crosscheck_refuses), (900, s_close_alert),
         (500, s_apply_keep), (1600, s_keep_dialog_shown),
         (600, s_apply_timeout), (10000, s_after_timeout),
         (600, s_apply_esc), (1800, s_press_esc), (3800, s_after_esc),
         (600, s_apply_failed), (3800, s_after_failed),
         (600, s_apply_cancelled),
         (800, s_second_check_catches_late_change), (6500, s_after_second_check),
         (800, s_revert_fails_handover),
         (5000, s_drag_disabled_denied), (600, s_single_monitor_hint), (1000, s_disabled_row_fixed),
         (600, s_drag_left), (900, s_drag_preview_shot),
         (1500, s_drag_anchor), (1200, s_reflow_on_scale),
         (900, s_apply_after_drag), (1800, s_keep_after_drag),
         (900, s_illegal_layout_refused), (1500, s_end)]

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
