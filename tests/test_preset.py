"""`displayctl.py preset external|extend` (the Super+P menu): the layouts
build_preset makes, and cmd_preset's flow. No display: heads are built from
backend.Head/Mode with no proxies, and the apply path is mocked.

Run: .venv/bin/python -m unittest discover -s tests
"""

import argparse
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import displayctl  # noqa: E402
from backend import Head, Mode  # noqa: E402


def mode(w, h, hz, preferred=False):
    m = Mode(None)
    m.width, m.height, m.refresh_mhz, m.preferred = w, h, round(hz * 1000), preferred
    return m


def head(name, modes, current=None, pos=(0, 0), scale=1.0, transform=0):
    """A Head like the real ones; `current` = index into modes, None = off."""
    h = Head(None)
    h.name, h.modes = name, modes
    h.enabled = current is not None
    h.current_mode = modes[current] if h.enabled else None
    h.position, h.scale, h.transform = pos, scale, transform
    return h


def laptop(on=True, pos=(0, 0)):
    # Real eDP-1: 144 Hz is preferred, 60 Hz also offered.
    return head("eDP-1", [mode(1920, 1080, 144.005, preferred=True), mode(1920, 1080, 60)],
                current=0 if on else None, pos=pos)


def samsung(current=0, pos=(0, 0)):
    # Preferred at 144 on purpose: best_mode must still pick 200 for that size.
    return head("DP-3", [mode(2560, 1440, 144, preferred=True), mode(2560, 1440, 200),
                         mode(1920, 1080, 240)], current=current, pos=pos)


def summary(desired):
    """{name: (enabled, WxH@Hz, (x, y))} for readable asserts."""
    out = {}
    for n, s in desired.items():
        m = s["mode"]
        out[n] = (s["enabled"], m and f"{m['width']}x{m['height']}@{m['refresh_mhz'] // 1000}",
                  tuple(s["position"]) if s["enabled"] else None)
    return out


class BuildPreset(unittest.TestCase):
    def test_external_from_extend(self):
        heads = [laptop(pos=(0, 0)), samsung(current=1, pos=(1920, 0))]
        self.assertEqual(summary(displayctl.build_preset(heads, "external")), {
            "eDP-1": (False, None, None),
            "DP-3": (True, "2560x1440@200", (0, 0)),   # keeps its mode, moves to 0,0
        })

    def test_extend_from_external_puts_laptop_left(self):
        heads = [laptop(on=False), samsung(current=1)]
        self.assertEqual(summary(displayctl.build_preset(heads, "extend")), {
            "eDP-1": (True, "1920x1080@144", (0, 0)),       # comes on at its best mode
            "DP-3": (True, "2560x1440@200", (1920, 0)),     # to its right
        })

    def test_head_turned_on_gets_best_mode_scale_1(self):
        heads = [laptop(), samsung(current=None)]
        d = displayctl.build_preset(heads, "external")
        self.assertEqual(summary(d)["DP-3"], (True, "2560x1440@200", (0, 0)))
        self.assertEqual((d["DP-3"]["scale"], d["DP-3"]["transform"]), (1.0, 0))

    def test_head_already_on_keeps_mode_scale_and_rotation(self):
        # DP-3 at 1080p@240, scale 1.5, rotated 90: logical width 1080 / 1.5 = 720.
        heads = [laptop(on=False), samsung(current=2), head("DP-4", [mode(1920, 1080, 60, True)])]
        heads[1].scale, heads[1].transform = 1.5, 1
        d = displayctl.build_preset(heads, "extend")
        self.assertEqual(summary(d)["DP-3"], (True, "1920x1080@240", (1920, 0)))
        self.assertEqual((d["DP-3"]["scale"], d["DP-3"]["transform"]), (1.5, 1))
        self.assertEqual(summary(d)["DP-4"], (True, "1920x1080@60", (1920 + 720, 0)))

    def test_no_external_monitor_refuses(self):
        for name in displayctl.PRESETS:
            with self.assertRaisesRegex(ValueError, "no external monitor"):
                displayctl.build_preset([laptop()], name)

    def test_never_everything_off(self):
        for name in displayctl.PRESETS:
            d = displayctl.build_preset([laptop(), samsung()], name)
            self.assertTrue(any(s["enabled"] for s in d.values()), name)

    def test_laptop_preset_is_not_offered(self):
        self.assertNotIn("laptop", displayctl.PRESETS)
        with self.assertRaises(ValueError):
            displayctl.build_preset([laptop(), samsung()], "laptop")
        with mock.patch.object(sys, "argv", ["displayctl.py", "preset", "laptop"]), \
             mock.patch("sys.stderr", new_callable=io.StringIO), \
             self.assertRaises(SystemExit):
            displayctl.main()


class CmdPreset(unittest.TestCase):
    def run_preset(self, name, heads):
        om = mock.MagicMock(heads=heads)
        args = argparse.Namespace(name=name, dry_run=False, auto_keep=False,
                                  auto_revert_after=None, self_revert_test=None)
        with mock.patch("displayctl.take_lock"), \
             mock.patch("displayctl.OutputManager", return_value=om), \
             mock.patch("displayctl.apply_and_confirm") as apply, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            displayctl.cmd_preset(args)
        om.close.assert_called()
        return apply, out.getvalue()

    def test_applies_the_whole_layout_through_the_safe_path(self):
        apply, text = self.run_preset("extend", [laptop(on=False), samsung(current=1)])
        apply.assert_called_once()
        desired = apply.call_args.args[2]
        self.assertEqual(set(desired), {"eDP-1", "DP-3"})     # every head, never a diff
        self.assertIn("eDP-1: disabled  ->  1920x1080@144.005Hz  pos 0,0", text)

    def test_already_there_does_nothing(self):
        apply, text = self.run_preset("external", [laptop(on=False), samsung(current=1)])
        apply.assert_not_called()
        self.assertIn("nothing to do", text)

    def test_main_routes_preset_and_warns_about_kanshi(self):
        with mock.patch.object(sys, "argv", ["displayctl.py", "preset", "external"]), \
             mock.patch("displayctl.cmd_preset") as cmd, \
             mock.patch("safety.kanshi_warning", return_value="kanshi is running (PID 7)"), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            displayctl.main()
        cmd.assert_called_once()
        self.assertIsNone(cmd.call_args.args[0].self_revert_test)
        self.assertIn("WARNING: kanshi", err.getvalue())


if __name__ == "__main__":
    unittest.main()
