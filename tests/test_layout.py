"""Unit tests for layout.py. Run: .venv/bin/python -m unittest discover tests

They use backend.Head/Mode objects without a Wayland connection (proxy=None),
shaped like this machine's outputs, so no display is ever touched.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import layout  # noqa: E402
from backend import Head, Mode  # noqa: E402


def make_head(name, modes, current=None, enabled=True, position=(0, 0), scale=1.0, preferred=0):
    head = Head(proxy=None)
    head.name = name
    for i, (w, h, r) in enumerate(modes):
        m = Mode(proxy=None)
        m.width, m.height, m.refresh_mhz, m.preferred = w, h, r, i == preferred
        head.modes.append(m)
    head.enabled = enabled
    head.position = position
    head.scale = scale
    head.current_mode = head.modes[current] if (enabled and current is not None) else None
    return head


def machine():
    """DP-3 on at 2560x1440@200, eDP-1 off (like the real setup)."""
    dp3 = make_head("DP-3", [
        (2560, 1440, 200000), (2560, 1440, 143999), (2560, 1440, 59951),
        (1920, 1080, 144000), (1920, 1080, 60000), (1920, 1080, 59940),
        (1280, 720, 60000),
    ], current=0)
    edp = make_head("eDP-1", [(1920, 1080, 144005), (1920, 1080, 60031)],
                    enabled=False, preferred=0)
    return dp3, edp


class Choices(unittest.TestCase):
    def test_resolutions_unique_largest_first(self):
        dp3, _ = machine()
        self.assertEqual(layout.resolutions(dp3), [(2560, 1440), (1920, 1080), (1280, 720)])

    def test_refresh_filtered_by_resolution(self):
        dp3, _ = machine()
        self.assertEqual(layout.refresh_rates(dp3, 1920, 1080), [144000, 60000, 59940])
        self.assertEqual(layout.refresh_rates(dp3, 1280, 720), [60000])

    def test_odd_scale_and_flipped_rotation_are_kept_in_lists(self):
        s = {"scale": 1.6, "transform": 5}
        self.assertIn(1.6, layout.scales_for(s))
        self.assertEqual(layout.rotations_for(s), [0, 1, 2, 3, 5])


class Edits(unittest.TestCase):
    def setUp(self):
        self.dp3, self.edp = machine()
        self.heads = [self.dp3, self.edp]

    def test_resolution_keeps_closest_rate(self):
        s = layout.with_resolution(self.dp3.to_state(), self.dp3, 1920, 1080)
        self.assertEqual(s["mode"], {"width": 1920, "height": 1080, "refresh_mhz": 144000})

    def test_resolution_with_only_one_rate(self):
        s = layout.with_resolution(self.dp3.to_state(), self.dp3, 1280, 720)
        self.assertEqual(s["mode"]["refresh_mhz"], 60000)

    def test_refresh_must_exist_at_resolution(self):
        with self.assertRaises(ValueError):
            layout.with_refresh(self.dp3.to_state(), self.dp3, 180000)

    def test_edits_do_not_touch_live_state(self):
        live = self.dp3.to_state()
        layout.with_scale(live, 1.5)
        self.assertEqual(self.dp3.to_state(), live)
        self.assertEqual(live["scale"], 1.0)

    def test_pending_added_and_dropped_when_back_to_live(self):
        s = layout.with_refresh(self.dp3.to_state(), self.dp3, 143999)
        pending = layout.set_pending({}, self.dp3, s)
        self.assertIn("DP-3", pending)
        back = layout.with_refresh(s, self.dp3, 200000)
        self.assertEqual(layout.set_pending(pending, self.dp3, back), {})

    def test_sub_hertz_edit_is_still_pending(self):
        s = layout.with_resolution(self.dp3.to_state(), self.dp3, 1920, 1080)
        s = layout.with_refresh(s, self.dp3, 60000)
        pending = layout.set_pending({}, self.dp3, s)
        # pretend 60.000 is live, then choose 59.940: must count as a change
        self.dp3.current_mode = self.dp3.modes[4]
        s2 = layout.with_refresh(self.dp3.to_state(), self.dp3, 59940)
        self.assertTrue(layout.differs(self.dp3, s2))
        self.assertIn("DP-3", pending)

    def test_effective_uses_pending_over_live(self):
        s = layout.with_scale(self.dp3.to_state(), 2.0)
        eff = layout.effective(self.heads, {"DP-3": s})
        self.assertEqual(eff["DP-3"]["scale"], 2.0)
        self.assertFalse(eff["eDP-1"]["enabled"])

    def test_enable_places_right_of_others_with_preferred_mode(self):
        eff = layout.effective(self.heads, {})
        s = layout.with_enabled(eff, self.edp, True)
        self.assertEqual(s["mode"], {"width": 1920, "height": 1080, "refresh_mhz": 144005})
        self.assertEqual(s["position"], [2560, 0])
        self.assertEqual((s["scale"], s["transform"]), (1.0, 0))

    def test_enable_placement_uses_pending_scale_of_others(self):
        # DP-3 pending at scale 2 -> logical width 1280, so eDP-1 goes at 1280
        eff = layout.effective(self.heads, {"DP-3": layout.with_scale(self.dp3.to_state(), 2.0)})
        self.assertEqual(layout.with_enabled(eff, self.edp, True)["position"], [1280, 0])

    def test_cannot_turn_off_last_display(self):
        eff = layout.effective(self.heads, {})
        with self.assertRaises(layout.AllOutputsOff):
            layout.with_enabled(eff, self.dp3, False)

    def test_can_turn_off_one_of_two(self):
        eff = layout.effective(self.heads, {})
        eff["eDP-1"] = layout.with_enabled(eff, self.edp, True)
        off = layout.with_enabled(eff, self.dp3, False)
        self.assertFalse(off["enabled"])
        self.assertIsNone(off["mode"])

    def test_turning_off_then_on_again_is_not_pending(self):
        eff = layout.effective(self.heads, {})
        on = layout.with_enabled(eff, self.edp, True)
        pending = layout.set_pending({}, self.edp, on)
        eff = layout.effective(self.heads, pending)
        off = layout.with_enabled(eff, self.edp, False)
        self.assertEqual(layout.set_pending(pending, self.edp, off), {})

    def test_rotation_swaps_logical_size(self):
        s = layout.with_transform(self.dp3.to_state(), 1)
        self.assertEqual(layout.logical_size(s), (1440, 2560))

    def test_validate(self):
        eff = layout.effective(self.heads, {})
        self.assertEqual(layout.validate(eff), [])
        eff["DP-3"]["enabled"] = False
        self.assertTrue(layout.validate(eff))

    def test_prune_drops_unplugged(self):
        pending = {"DP-3": self.dp3.to_state(), "HDMI-A-1": {}}
        self.assertEqual(set(layout.prune(pending, self.heads)), {"DP-3"})


if __name__ == "__main__":
    unittest.main()
