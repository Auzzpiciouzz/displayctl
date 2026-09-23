"""Unit tests for drag-to-arrange logic in layout.py (docs/drag-to-arrange.md).

Run: .venv/bin/python -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import layout  # noqa: E402
from test_layout import machine  # noqa: E402


def two_on():
    """DP-3 at 0,0 and eDP-1 enabled to its right (effective states)."""
    dp3, edp = machine()
    states = layout.effective([dp3, edp], {})
    states["eDP-1"] = layout.with_enabled(states, edp, True)  # at 2560,0
    return dp3, edp, states


class Geometry(unittest.TestCase):
    def test_touch_needs_a_shared_edge_not_a_corner(self):
        self.assertTrue(layout.touches((0, 0, 10, 10), (10, 5, 10, 10)))
        self.assertFalse(layout.touches((0, 0, 10, 10), (10, 10, 10, 10)))  # corner only
        self.assertFalse(layout.touches((0, 0, 10, 10), (11, 0, 10, 10)))  # gap

    def test_overlap_excludes_touching(self):
        self.assertFalse(layout.overlaps((0, 0, 10, 10), (10, 0, 10, 10)))
        self.assertTrue(layout.overlaps((0, 0, 10, 10), (9, 0, 10, 10)))

    def test_problems_for_gap_and_overlap(self):
        _, _, s = two_on()
        self.assertEqual(layout.arrangement_problems(s), [])
        s["eDP-1"]["position"] = [2600, 0]
        self.assertTrue(any("float" in p for p in layout.arrangement_problems(s)))
        s["eDP-1"]["position"] = [2000, 0]
        self.assertTrue(any("overlap" in p for p in layout.arrangement_problems(s)))

    def test_validate_reports_arrangement(self):
        _, _, s = two_on()
        s["eDP-1"]["position"] = [3000, 0]
        self.assertTrue(layout.validate(s))


class Snapping(unittest.TestCase):
    def test_raw_position_in_the_middle_snaps_to_a_legal_spot(self):
        _, _, s = two_on()
        pos = layout.snap_position(s, "eDP-1", (1000, 300))
        self.assertEqual(layout.arrangement_problems(layout._placed(s, "eDP-1", pos)), [])

    def test_snaps_left_of_and_aligns_top(self):
        _, _, s = two_on()
        self.assertEqual(layout.snap_position(s, "eDP-1", (-2500, 40)), (-1920, 0))

    def test_aligns_bottom_and_centre_within_threshold(self):
        _, _, s = two_on()
        # DP-3 is 1440 high, eDP-1 1080: bottom-aligned y = 360, centred y = 180
        self.assertEqual(layout.snap_position(s, "eDP-1", (2600, 330)), (2560, 360))
        self.assertEqual(layout.snap_position(s, "eDP-1", (2600, 200)), (2560, 180))
        self.assertEqual(layout.snap_position(s, "eDP-1", (2600, 280), threshold=10), (2560, 280))

    def test_below_with_free_x(self):
        _, _, s = two_on()
        self.assertEqual(layout.snap_position(s, "eDP-1", (500, 2000)), (500, 1440))

    def test_never_corner_only(self):
        _, _, s = two_on()
        pos = layout.snap_position(s, "eDP-1", (2700, 1600), threshold=0)
        r_dp3 = layout.rect(s["DP-3"])
        r_edp = layout.rect(layout._placed(s, "eDP-1", pos)["eDP-1"])
        self.assertTrue(layout.touches(r_dp3, r_edp))

    def test_uses_logical_size_with_rotation_and_scale(self):
        dp3, edp, s = two_on()
        s["DP-3"] = layout.with_transform(s["DP-3"], 1)       # 1440 x 2560 logical
        self.assertEqual(layout.snap_position(s, "eDP-1", (1500, 0)), (1440, 0))
        s["DP-3"] = layout.with_scale(layout.with_transform(s["DP-3"], 0), 1.25)  # 2048 x 1152
        self.assertEqual(layout.snap_position(s, "eDP-1", (2100, 0)), (2048, 0))


class Anchoring(unittest.TestCase):
    def test_anchor_is_monitor_at_origin(self):
        _, _, s = two_on()
        self.assertEqual(layout.anchor_name(s), "DP-3")

    def test_dragging_the_anchor_moves_the_others(self):
        _, _, s = two_on()
        # drag DP-3 to the right of eDP-1: DP-3 stays at 0,0, eDP-1 goes left
        pos = layout.snap_position(s, "DP-3", (1920 + 2560, 0))
        new = layout.moved(s, "DP-3", pos, "DP-3")
        self.assertEqual(new["DP-3"]["position"], [0, 0])
        self.assertEqual(new["eDP-1"]["position"], [-1920, 0])
        self.assertEqual(layout.arrangement_problems(new), [])

    def test_moving_the_other_keeps_anchor(self):
        _, _, s = two_on()
        pos = layout.snap_position(s, "eDP-1", (100, -2000))
        new = layout.moved(s, "eDP-1", pos, "DP-3")
        self.assertEqual(new["DP-3"]["position"], [0, 0])
        self.assertEqual(new["eDP-1"]["position"][1], -1080)


class Reflow(unittest.TestCase):
    def test_neighbour_follows_width_change(self):
        _, _, before = two_on()
        after = dict(before)
        after["DP-3"] = layout.with_scale(before["DP-3"], 2.0)  # 1280 x 720
        new = layout.reflow_after_resize(before, after, "DP-3")
        self.assertEqual(new["eDP-1"]["position"], [1280, 0])
        self.assertEqual(layout.arrangement_problems(new), [])

    def test_neighbour_below_follows_height_change(self):
        _, _, s = two_on()
        s["eDP-1"]["position"] = [0, 1440]
        after = dict(s)
        after["DP-3"] = layout.with_transform(s["DP-3"], 1)  # 1440 x 2560
        new = layout.reflow_after_resize(s, after, "DP-3")
        self.assertEqual(new["eDP-1"]["position"], [0, 2560])


if __name__ == "__main__":
    unittest.main()
