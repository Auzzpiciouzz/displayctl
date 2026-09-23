"""Supervised LIVE test L7: GUI Keep makes the layout durable (real displays).

THIS CHANGES YOUR DISPLAYS AND WRITES ~/.config/hypr/displayctl/monitors.lua.
Expects the baseline (DP-3 on at 0,0 @200, eDP-1 off), kanshi off, and the
user's dofile line in hyprland.lua.

Steps: eDP-1 on + dragged left of DP-3 -> Apply -> Keep -> wait for the
GUI's background `displayctl.py persist` -> check its result, the files and
both views -> one plain `hyprctl reload` -> the layout must stay.
Stops (Revert if a dialog is open) on anything unexpected.

Run:  .venv/bin/python tests/gui_live_durable.py
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib  # noqa: E402

import durable  # noqa: E402
import gui  # noqa: E402

# Reuse the live-arrange helpers (they only define functions until run()).
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location(
    "live", os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_live_arrange.py"))
live_src = open(spec.origin).read().split("\napp.connect(")[0]  # everything but the run hook
live = {"__name__": "live", "__file__": spec.origin}
sys.argv = [sys.argv[0], "/tmp"]  # gui_live_arrange reads OUT from argv
exec(compile(live_src, spec.origin, "exec"), live)

app = live["app"]
W, check, expect_real, BASE = live["W"], live["check"], live["expect_real"], live["BASE"]
LEFT = {"DP-3": ("on", (1920, 0), (2560, 1440, 200)), "eDP-1": ("on", (0, 0), (1920, 1080, 144))}


def sequence():
    live["current_test"][0] = "L7"
    expect_real("baseline before L7", BASE)
    prev_before = (durable.DIR / "monitors.lua").read_text()
    live["enable_edp"]()
    live["drag"]("eDP-1", (-2400, 0))
    W().apply_button.emit("clicked")
    ok = yield from live["wait_until"](lambda: W().applier.dialog is not None, 10)
    check("Keep dialog shown", ok)
    expect_real("eDP-1 left of DP-3 (during hold)", LEFT)
    live["respond"]("keep")
    ok = yield from live["wait_until"](lambda: isinstance(app.persist_result, tuple), 30)
    check("background persist finished", ok, app.persist_result)
    success, out = app.persist_result
    check("persist succeeded and verified", success and "Made permanent" in out, out.strip())
    lua = (durable.DIR / "monitors.lua").read_text()
    check("new rules: eDP-1 at 0x0, DP-3 at 1920x0",
          'output = "eDP-1", mode = "1920x1080@144.005", position = "0x0"' in lua
          and 'output = "DP-3", mode = "2560x1440@200.000", position = "1920x0"' in lua)
    check("no guard in the new file (eDP-1 is on)", "KEPT_WITH" not in lua)
    check(".prev holds the previous (baseline) rules",
          (durable.DIR / "monitors.lua.prev").read_text() == prev_before)
    yield 1.0
    expect_real("after Keep + persist", LEFT)
    subprocess.run(["hyprctl", "reload"], check=True, capture_output=True)
    yield 3.0
    expect_real("after a plain hyprctl reload: the kept layout STAYS", LEFT)
    errors = durable.config_errors()
    check("no Hyprland config errors", errors == "", errors)


live["sequence"] = sequence
app.connect("activate", lambda *_: live["run"]())
sys.exit(app.run([]))
