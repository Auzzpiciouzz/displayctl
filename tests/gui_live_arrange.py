"""Supervised LIVE test of drag-to-arrange on the REAL gui.py (real applies).

THIS CHANGES YOUR DISPLAYS. Runs manual tests 2-7 of the drag-to-arrange
list with synthetic drags / button presses. Expects the baseline:
DP-3 on at 0,0 (2560x1440@200), eDP-1 off; kanshi and other output tools off.

After every step that can touch the displays, both views (a fresh protocol
read and `hyprctl monitors all -j`) must match what that step should
produce. On any mismatch it STOPS: presses Revert if a Keep dialog is open,
then quits (closing the connection, so the watchdog can act too).

Run:  .venv/bin/python tests/gui_live_arrange.py SCREENSHOT_DIR
Prints one line per check and a summary table.
"""

import json
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib  # noqa: E402

import backend  # noqa: E402
import gui  # noqa: E402
import layout  # noqa: E402

OUT = sys.argv[1]
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s.%(msecs)03d %(message)s",
                    datefmt="%H:%M:%S")
GLib.log_set_writer_func(gui.drop_vk_suboptimal)

app = gui.DisplayctlApp()
app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
W = lambda: app.window  # noqa: E731
results = []  # (test, check, ok, detail)
current_test = ["-"]


class Unexpected(Exception):
    pass


def check(name, ok, detail=""):
    results.append((current_test[0], name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'} [{current_test[0]}] {name}" + (f"  [{detail}]" if not ok else ""),
          flush=True)
    return ok


# --- the real displays --------------------------------------------------------

BASE = {"DP-3": ("on", (0, 0), (2560, 1440, 200)), "eDP-1": ("off",)}


def real_state():
    """{name: ('on', (x, y), (w, h, hz)) or ('off',)} if BOTH views agree."""
    hy = {m["name"]: m for m in json.loads(subprocess.check_output(["hyprctl", "monitors", "all", "-j"]))}
    proto = {}
    for n, s in backend.read_states().items():
        proto[n] = (("on", tuple(s["position"]), (s["mode"]["width"], s["mode"]["height"],
                                                   round(s["mode"]["refresh_mhz"] / 1000)))
                    if s["enabled"] else ("off",))
    hypr = {n: (("off",) if m["disabled"] else
                ("on", (m["x"], m["y"]), (m["width"], m["height"], round(m["refreshRate"]))))
            for n, m in hy.items()}
    if proto != hypr:
        raise Unexpected(f"protocol and hyprctl disagree: {proto} vs {hypr}")
    return proto


def expect_real(what, expected):
    got = real_state()
    if got != expected:
        check(f"real displays: {what}", False, f"got {got}, expected {expected}")
        raise Unexpected(f"{what}: got {got}, expected {expected}")
    check(f"real displays: {what}", True)


# --- GUI helpers ----------------------------------------------------------------

def settle(rounds=50):
    ctx = GLib.MainContext.default()
    for _ in range(rounds):
        if not ctx.pending():
            break
        ctx.iteration(False)


def redraw():
    c = W().canvas
    before = c._view
    c._view = None
    c.queue_draw()
    ctx = GLib.MainContext.default()
    end = time.monotonic() + 1.0
    while c._view is None and time.monotonic() < end:
        ctx.iteration(False)
    if c._view is None:
        c._view = before


def box(name):
    return next(tuple(round(v) for v in b[1:]) for b in W().canvas._hit_boxes if b[0] == name)


def center(name):
    x, y, w, h = box(name)
    return x + w / 2, y + h / 2


def select(name):
    redraw()
    x, y = center(name)
    W().canvas._on_click(None, 1, x, y)
    settle()


def use(on):
    W().details.use_row.set_active(on)
    settle()


def pick(key, label):
    row = W().details.combos[key]
    model = row.get_model()
    labels = [model.get_string(i) for i in range(model.get_n_items())]
    row.set_selected(labels.index(label))
    settle()


class G:
    state = None

    def set_state(self, s):
        self.state = s


def drag(name, to_logical, still=None):
    """Drag `name` toward to_logical; check `still` doesn't move meanwhile."""
    redraw()
    c = W().canvas
    still_before = box(still) if still else None
    x, y = center(name)
    g = G()
    c._on_drag_begin(g, x, y)
    if g.state is not None:
        raise Unexpected(f"drag of {name} was refused")
    factor = c._drag["view"][0]
    sx, sy = c._drag["start"]
    dx, dy = (to_logical[0] - sx) * factor, (to_logical[1] - sy) * factor
    for f in (0.3, 0.6, 1.0):
        c._on_drag_update(g, dx * f, dy * f)
    settle(200)
    if still:
        check(f"{still} stayed still while dragging {name}", box(still) == still_before,
              (still_before, box(still)))
    c._on_drag_end(g, dx, dy)
    settle()
    redraw()


def pos(name):
    return tuple(W().effective()[name]["position"])


def all_non_negative():
    st = W().effective()
    return all(min(s["position"]) >= 0 for s in st.values() if s["enabled"])


def shot(name):
    for c in json.loads(subprocess.check_output(["hyprctl", "clients", "-j"])):
        if c.get("pid") == os.getpid():
            (x, y), (w, h) = c["at"], c["size"]
            subprocess.run(["grim", "-g", f"{x},{y} {w}x{h}", f"{OUT}/{name}.png"], check=False)
            return


def reset():
    W().reset_button.emit("clicked")
    settle()
    redraw()


def enable_edp():
    select("eDP-1")
    use(True)
    redraw()


# --- the sequence (a generator: yields = "wait this long / until this") ----------

def wait_until(cond, timeout, step=0.1):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            return False
        yield step
    return True


def respond(r):
    d = W().applier.dialog
    d.emit("response", r)
    d.force_close()


def sequence():
    current_test[0] = "pre"
    expect_real("baseline before tests", BASE)

    # Test 2: drag eDP-1 left / below / above DP-3 (no apply)
    current_test[0] = "2"
    for label, raw, exp in [
        ("left", (-2400, 30), {"eDP-1": (0, 0), "DP-3": (1920, 0)}),
        ("below", (500, 2000), {"DP-3": (0, 0), "eDP-1": (500, 1440)}),
        ("above", (500, -2000), {"eDP-1": (500, 0), "DP-3": (0, 1080)}),
    ]:
        enable_edp()
        check("eDP-1 on (pending) at 2560,0", pos("eDP-1") == (2560, 0), pos("eDP-1"))
        drag("eDP-1", raw, still="DP-3")
        got = {n: pos(n) for n in exp}
        check(f"eDP-1 {label}: positions {exp}", got == exp, got)
        check(f"eDP-1 {label}: all positions >= 0, legal",
              all_non_negative() and not layout.arrangement_problems(W().effective()))
        yield 0.3
        shot(f"live-t2-{label}")
        reset()
        check("Reset clears pending", W().pending == {})
    expect_real("unchanged after test 2 (nothing applied)", BASE)

    # Test 3: drag DP-3 past eDP-1
    current_test[0] = "3"
    enable_edp()
    drag("DP-3", (2560 + 1920 + 100, 0), still="eDP-1")
    got = {n: pos(n) for n in ("eDP-1", "DP-3")}
    check("after release: eDP-1 0,0 and DP-3 1920,0", got == {"eDP-1": (0, 0), "DP-3": (1920, 0)}, got)
    yield 0.3
    shot("live-t3")
    reset()
    expect_real("unchanged after test 3", BASE)

    # Test 4: scale 200% moves the neighbour
    current_test[0] = "4"
    enable_edp()
    select("DP-3")
    pick("scale", "200%")
    redraw()
    check("DP-3 at 200%: eDP-1 moves to 1280,0", pos("eDP-1") == (1280, 0), pos("eDP-1"))
    yield 0.3
    shot("live-t4")
    reset()
    expect_real("unchanged after test 4", BASE)

    LEFT = {"DP-3": ("on", (1920, 0), (2560, 1440, 200)), "eDP-1": ("on", (0, 0), (1920, 1080, 144))}

    # Test 5: apply eDP-1 left + Keep; then eDP-1 off + Keep
    current_test[0] = "5"
    enable_edp()
    drag("eDP-1", (-2400, 0))
    W().apply_button.emit("clicked")
    ok = yield from wait_until(lambda: W().applier.dialog is not None, 10)
    check("Keep dialog shown", ok)
    expect_real("eDP-1 on at 0,0, DP-3 at 1920,0 (during hold)", LEFT)
    respond("keep")
    yield from wait_until(lambda: not W().applier.busy, 5)
    yield 1.5  # reconnect + fresh read
    expect_real("after Keep: still eDP-1 0,0 / DP-3 1920,0", LEFT)
    shot("live-t5-kept-left")
    select("eDP-1")
    use(False)
    check("turning eDP-1 off puts DP-3 back at 0,0 (pending)", pos("DP-3") == (0, 0), pos("DP-3"))
    W().apply_button.emit("clicked")
    ok = yield from wait_until(lambda: W().applier.dialog is not None, 10)
    check("Keep dialog shown", ok)
    expect_real("eDP-1 off, DP-3 at 0,0 (during hold)", BASE)
    respond("keep")
    yield from wait_until(lambda: not W().applier.busy, 5)
    yield 1.5
    expect_real("after Keep: baseline, DP-3 back at 0,0", BASE)
    shot("live-t5-back")

    # Test 6: Revert button, then Esc
    for how in ("revert", "esc"):
        current_test[0] = f"6-{how}"
        enable_edp()
        drag("eDP-1", (-2400, 0))
        W().apply_button.emit("clicked")
        ok = yield from wait_until(lambda: W().applier.dialog is not None, 10)
        check("Keep dialog shown", ok)
        expect_real("applied (during hold)", LEFT)
        if how == "revert":
            respond("revert")
        else:
            W().applier.dialog.close()  # what Esc does: the close response = revert
        ok = yield from wait_until(lambda: not W().applier.busy, 15)
        check("revert finished", ok)
        yield 1.5
        expect_real(f"after {how}: back to baseline", BASE)

    # Test 7: eDP-1 below, Apply, let the 15 s timeout revert
    current_test[0] = "7"
    enable_edp()
    drag("eDP-1", (500, 2000))
    W().apply_button.emit("clicked")
    ok = yield from wait_until(lambda: W().applier.dialog is not None, 10)
    check("Keep dialog shown", ok)
    shown = time.monotonic()
    expect_real("applied below (during hold)",
                {"DP-3": ("on", (0, 0), (2560, 1440, 200)), "eDP-1": ("on", (500, 1440), (1920, 1080, 144))})
    # _decided flips when the countdown runs out (the revert starts then;
    # the dialog object is only cleared after the revert is verified).
    ok = yield from wait_until(lambda: W().applier._decided, 25)
    took = time.monotonic() - shown
    check("timeout reverted after ~15 s", ok and 14.5 <= took <= 17, f"{took:.1f} s")
    yield from wait_until(lambda: not W().applier.busy, 15)
    yield 1.5
    expect_real("after timeout: back to baseline", BASE)


def run():
    gen = sequence()

    def tick():
        try:
            delay = next(gen)
        except StopIteration:
            finish(False)
            return GLib.SOURCE_REMOVE
        except Unexpected as e:
            print(f"UNEXPECTED: {e}. Stopping.", flush=True)
            finish(True)
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(int(delay * 1000), tick)
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(1500, tick)


def finish(aborted):
    if aborted and W().applier.dialog is not None:
        print("Keep dialog open: pressing Revert before quitting", flush=True)
        respond("revert")
        GLib.timeout_add(6000, app.quit)
    else:
        app.quit()
    passed = sum(ok for *_, ok, _d in results)
    print(f"\n{passed}/{len(results)} checks passed{' (ABORTED)' if aborted else ''}", flush=True)


app.connect("activate", lambda *_: run())
sys.exit(app.run([]))
