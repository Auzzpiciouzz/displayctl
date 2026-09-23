"""U1 + U2: durable Keep (durable.py). No Hyprland, no display changes.

U1 runs the GENERATED Lua under every local interpreter (Lua 5.5, 5.4,
LuaJIT) against a mock `hl` (durable.MOCK_DRIVER). U2 tests the file
handling in a temp directory.

Run: .venv/bin/python -m unittest discover -s tests
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import durable  # noqa: E402

SAMSUNG = "Samsung Electric Company LS27FG53x H9DL300352"
BOE = "BOE 0x0AAE"
DESC = {"DP-3": SAMSUNG, "eDP-1": BOE}
DP3 = {"name": "DP-3", "description": SAMSUNG}
EDP = {"name": "eDP-1", "description": BOE}
INTERPRETERS = [i for i in ("lua", "lua5.4", "luajit") if shutil.which(i)]


def state(name, on, w=0, h=0, hz=0, pos=(0, 0), scale=1.0, transform=0):
    return {"name": name, "enabled": on,
            "mode": {"width": w, "height": h, "refresh_mhz": hz} if on else None,
            "position": list(pos), "transform": transform, "scale": scale, "adaptive_sync": 0}


def docked_edp_off():
    return {"DP-3": state("DP-3", True, 2560, 1440, 200000), "eDP-1": state("eDP-1", False)}


def both_on():
    return {"DP-3": state("DP-3", True, 2560, 1440, 200000, pos=(1920, 0)),
            "eDP-1": state("eDP-1", True, 1920, 1080, 144005, pos=(0, 0))}


def gen(states, desc=DESC):
    return durable.generate_lua(states, desc, now=datetime.datetime(2026, 9, 23, 12, 0))


def edp_off(rules):
    return any("output=eDP-1" in r and "disabled=true" in r for r in rules)


class U1Generated(unittest.TestCase):
    """The generated Lua, run for real under each interpreter."""

    def each(self):
        self.assertTrue(INTERPRETERS, "no local Lua interpreter")
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                yield lua

    def test_a_docked_turns_panel_off_and_registers_handlers(self):
        for lua in self.each():
            rules, handlers, _, _ = durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua)
            self.assertIn("mode=2560x1440@200.000 output=DP-3 position=0x0 scale=1 transform=0", rules)
            self.assertTrue(edp_off(rules))
            self.assertEqual(handlers, ["monitor.added", "monitor.removed"])

    def test_b_undocked_leaves_panel_on(self):
        for lua in self.each():
            rules, _, _, _ = durable.run_mock(gen(docked_edp_off()), [], lua=lua)
            self.assertFalse(edp_off(rules))

    def test_c_other_external_leaves_panel_on(self):
        other = {"name": "DP-3", "description": "Dell Inc. U2720Q ABC123"}  # same port, other monitor
        for lua in self.each():
            rules, _, _, _ = durable.run_mock(gen(docked_edp_off()), [other], lua=lua)
            self.assertFalse(edp_off(rules))

    def test_d_errors_and_junk_leave_panel_on(self):
        for lua in self.each():
            for bad in ("error", "nil", "junk", "destroyed"):
                with self.subTest(case=bad):
                    rules, _, _, _ = durable.run_mock(gen(docked_edp_off()), bad, lua=lua)
                    self.assertFalse(edp_off(rules))

    def test_e_panel_kept_on_gets_normal_rule_and_no_guard(self):
        for lua in self.each():
            rules, handlers, _, _ = durable.run_mock(gen(both_on()), [DP3, EDP], lua=lua)
            self.assertIn("mode=1920x1080@144.005 output=eDP-1 position=0x0 scale=1 transform=0", rules)
            self.assertIn("mode=2560x1440@200.000 output=DP-3 position=1920x0 scale=1 transform=0", rules)
            self.assertFalse(edp_off(rules))
            self.assertEqual(handlers, [])

    def test_f_awkward_descriptions_are_escaped_and_still_match(self):
        weird = 'Evil "Monitor" \\ with\nnewline\tand tab'
        mon = {"name": "DP-3", "description": weird}
        for lua in self.each():
            text = gen(docked_edp_off(), {"DP-3": weird, "eDP-1": BOE})
            rules, _, _, _ = durable.run_mock(text, [mon], lua=lua)
            self.assertTrue(edp_off(rules))
            rules, _, _, _ = durable.run_mock(text, [DP3], lua=lua)   # real Samsung: no match
            self.assertFalse(edp_off(rules))

    def test_g_syntax_check_with_luac(self):
        luac = shutil.which("luac")
        if not luac:
            self.skipTest("no luac")
        layouts = [docked_edp_off(), both_on(),
                   {"DP-3": state("DP-3", False), "eDP-1": state("eDP-1", True, 1920, 1080, 60031)},
                   {"DP-3": state("DP-3", True, 2560, 1440, 143999, scale=1.25, transform=1),
                    "eDP-1": state("eDP-1", False)}]
        for layout in layouts:
            with tempfile.NamedTemporaryFile("w", suffix=".lua") as f:
                f.write(gen(layout))
                f.flush()
                r = subprocess.run([luac, "-p", f.name], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_h_external_off_is_plain_disable_and_panel_never_unguarded(self):
        layout = {"DP-3": state("DP-3", False), "eDP-1": state("eDP-1", True, 1920, 1080, 144005)}
        for lua in self.each():
            rules, handlers, _, _ = durable.run_mock(gen(layout), [], lua=lua)
            self.assertIn("disabled=true output=DP-3", rules)
            self.assertFalse(edp_off(rules))
            self.assertEqual(handlers, [])
        # Across all layouts, "eDP-1 disabled" can only ever come from the guard.
        for layout in (docked_edp_off(), both_on(), layout):
            self.assertNotIn('hl.monitor({ output = "eDP-1", disabled = true })', gen(layout))

    def test_scale_and_rotation_in_rules(self):
        layout = {"DP-3": state("DP-3", True, 2560, 1440, 143999, pos=(0, 0), scale=1.25, transform=1),
                  "eDP-1": state("eDP-1", False)}
        rules, _, _, _ = durable.run_mock(gen(layout), [DP3], lua=INTERPRETERS[0])
        self.assertIn("mode=2560x1440@143.999 output=DP-3 position=0x0 scale=1.25 transform=1", rules)


class U1Handlers(unittest.TestCase):
    """Undock/dock re-check and reload-loop safety."""

    def run_event(self, loaded_with, event, lua, **kw):
        _, _, reloads, out = durable.run_mock(gen(docked_edp_off()), loaded_with, event=event,
                                              lua=lua, **kw)
        pending = int(next(l.split()[1] for l in out.splitlines() if l.startswith("TIMERS_PENDING")))
        return reloads, pending

    def test_i_undock_reloads_once(self):
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                r, p = self.run_event([DP3], {"name": "monitor.removed", "monitor": DP3, "after": []}, lua)
                self.assertEqual((r, p), (1, 0))

    def test_j_dock_reloads_once(self):
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                r, _ = self.run_event([], {"name": "monitor.added", "monitor": DP3, "after": [DP3]}, lua)
                self.assertEqual(r, 1)

    def test_k_panel_own_events_are_ignored(self):
        for lua in INTERPRETERS:
            for name in ("monitor.added", "monitor.removed"):
                with self.subTest(lua=lua, event=name):
                    r, p = self.run_event([DP3], {"name": name, "monitor": EDP, "after": [DP3]}, lua)
                    self.assertEqual((r, p), (0, 0))

    def test_l_no_change_no_reload(self):
        # After a reload the answer equals what that load applied: an unrelated
        # monitor coming while still docked must not reload (no loop).
        dell = {"name": "HDMI-A-1", "description": "Dell Inc. U2720Q ABC123"}
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                r, _ = self.run_event([DP3], {"name": "monitor.added", "monitor": dell,
                                              "after": [DP3, dell]}, lua)
                self.assertEqual(r, 0)
                r, _ = self.run_event([], {"name": "monitor.added", "monitor": dell, "after": [dell]}, lua)
                self.assertEqual(r, 0)   # undocked, still undocked: nothing to do

    def test_m_rate_limit_postpones_but_never_drops(self):
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                ev = {"name": "monitor.removed", "monitor": DP3, "after": []}
                r, p = self.run_event([DP3], ev, lua, stamp_age=1)   # reloaded 1 s ago
                self.assertEqual(r, 0)
                self.assertGreaterEqual(p, 1, "the check must stay scheduled")
                r, _ = self.run_event([DP3], ev, lua, stamp_age=60)  # long ago: go
                self.assertEqual(r, 1)

    def test_n_without_timers_checks_immediately(self):
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                r, _ = self.run_event([DP3], {"name": "monitor.removed", "monitor": DP3, "after": []},
                                      lua, timers=False)
                self.assertEqual(r, 1)

    def test_o_destroyed_event_object_is_handled(self):
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                r, _ = self.run_event([DP3], {"name": "monitor.removed", "monitor": "destroyed",
                                              "after": []}, lua)
                self.assertEqual(r, 1)   # treated as external, re-checked, undocked -> reload


class U2Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "displayctl"

    def tearDown(self):
        self.tmp.cleanup()

    def files(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_first_keep_creates_placeholder_prev(self):
        text, data = gen(docked_edp_off()), durable.kept_json(docked_edp_off(), DESC)
        durable.keep(text, data, self.dir)
        self.assertEqual(self.files(), ["monitors.json", "monitors.json.prev",
                                        "monitors.lua", "monitors.lua.prev"])
        self.assertEqual((self.dir / "monitors.lua").read_text(), text)
        self.assertEqual((self.dir / "monitors.lua.prev").read_text(), durable.PLACEHOLDER_LUA)
        self.assertIsNone(durable.load_json(self.dir, prev=True)["states"])

    def test_second_keep_moves_first_to_prev_and_undo_swaps(self):
        a, b = gen(docked_edp_off()), gen(both_on())
        durable.keep(a, durable.kept_json(docked_edp_off(), DESC), self.dir)
        durable.keep(b, durable.kept_json(both_on(), DESC), self.dir)
        self.assertEqual((self.dir / "monitors.lua").read_text(), b)
        self.assertEqual((self.dir / "monitors.lua.prev").read_text(), a)
        durable.undo(self.dir)
        self.assertEqual((self.dir / "monitors.lua").read_text(), a)
        self.assertEqual(durable.load_json(self.dir)["states"], docked_edp_off())
        durable.undo(self.dir)   # undo of undo = redo
        self.assertEqual((self.dir / "monitors.lua").read_text(), b)
        self.assertEqual(durable.load_json(self.dir)["states"], both_on())

    def test_undo_without_prev_refuses(self):
        with self.assertRaises(FileNotFoundError):
            durable.undo(self.dir)

    def test_json_round_trip(self):
        data = durable.kept_json(docked_edp_off(), DESC)
        durable.keep(gen(docked_edp_off()), data, self.dir)
        got = durable.load_json(self.dir)
        self.assertEqual(got, json.loads(json.dumps(data)))
        self.assertEqual(got["kept_with"], [SAMSUNG])
        self.assertEqual(got["guarded_panels"], ["eDP-1"])

    def test_atomic_write_leaves_no_temp_and_survives_a_failed_rename(self):
        target = self.dir / "monitors.lua"
        durable.atomic_write(target, "old\n")
        with mock.patch("durable.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                durable.atomic_write(target, "new\n")
        self.assertEqual(target.read_text(), "old\n")
        self.assertEqual(self.files(), ["monitors.lua"])   # no stray .tmp

    def test_self_check_passes_good_and_catches_unsafe(self):
        data = durable.kept_json(docked_edp_off(), DESC)
        self.assertEqual(durable.self_check(gen(docked_edp_off()), data), [])
        unsafe = gen(docked_edp_off()) + '\nhl.monitor({ output = "eDP-1", disabled = true })\n'
        problems = durable.self_check(unsafe, data)
        self.assertTrue(any("unsafe" in p for p in problems), problems)
        broken = "this is not lua ("
        self.assertTrue(durable.self_check(broken, data))

    def test_self_check_refuses_without_lua(self):
        with mock.patch("durable.shutil.which", return_value=None):
            self.assertTrue(durable.self_check(gen(docked_edp_off()), durable.kept_json(docked_edp_off(), DESC)))


if __name__ == "__main__":
    unittest.main()
