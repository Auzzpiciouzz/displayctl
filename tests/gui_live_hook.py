"""Drive the REAL gui.py (real apply, real watchdog) without clicking.

THIS CHANGES YOUR DISPLAYS. It's for supervised tests only.

  gui_live_hook.py OUTPUT REFRESH_HZ apply-keep   apply, press Keep, stay open idle
  gui_live_hook.py OUTPUT REFRESH_HZ apply-hold   apply, leave the Keep dialog open
  gui_live_hook.py OUTPUT 0 enable-revert         switch OUTPUT on, apply, press Revert

It runs until killed (SIGTERM for a normal close; tests also use SIGKILL and
SIGSTOP). Debug log goes to stderr, as with `gui.py --debug`.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib  # noqa: E402

import gui  # noqa: E402

output, refresh_hz, action = sys.argv[1], float(sys.argv[2]), sys.argv[3]
assert action in ("apply-keep", "apply-hold", "enable-revert")

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s.%(msecs)03d %(message)s",
                    datefmt="%H:%M:%S")
GLib.log_set_writer_func(gui.drop_vk_suboptimal)
log = logging.getLogger("hook")

app = gui.DisplayctlApp()
app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)


def settle():
    ctx = GLib.MainContext.default()
    for _ in range(50):
        if not ctx.pending():
            break
        ctx.iteration(False)


def start():
    w = app.window
    boxes = {n: (x, y, bw, bh) for n, x, y, bw, bh in w.canvas._hit_boxes}
    x, y, bw, bh = boxes[output]
    w.canvas._on_click(None, 1, x + bw / 2, y + bh / 2)
    if action == "enable-revert":
        w.details.use_row.set_active(True)
        settle()
        log.info("hook: pending %s, pressing Apply (pid %d)", sorted(w.pending), os.getpid())
        w.apply_button.emit("clicked")
        GLib.timeout_add(3000, press_revert)
        return False
    row = w.details.combos["refresh"]
    rates = w.details._choices["refresh"]
    target = min(rates, key=lambda r: abs(r / 1000 - refresh_hz))
    row.set_selected(rates.index(target))
    settle()
    log.info("hook: pending %s, pressing Apply (pid %d)", sorted(w.pending), os.getpid())
    w.apply_button.emit("clicked")
    if action == "apply-keep":
        GLib.timeout_add(2500, press_keep)
    return False


def press_keep():
    d = app.window.applier.dialog
    if d is None:
        log.info("hook: no dialog yet, waiting")
        return True
    log.info("hook: pressing Keep")
    d.emit("response", "keep")
    d.force_close()
    log.info("hook: kept; staying open and idle")
    return False


def press_revert():
    d = app.window.applier.dialog
    if d is None:
        log.info("hook: no dialog yet, waiting")
        return True
    log.info("hook: pressing Revert")
    d.emit("response", "revert")
    d.force_close()
    return False


app.connect("activate", lambda *_: GLib.timeout_add(1500, start))
sys.exit(app.run([]))
