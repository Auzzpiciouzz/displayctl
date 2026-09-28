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
    """The generated Lua, run for real under each interpreter (first load)."""

    def each(self):
        self.assertTrue(INTERPRETERS, "no local Lua interpreter")
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                yield lua

    def test_a_docked_turns_panel_off_and_registers_handlers(self):
        for lua in self.each():
            r = durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua)
            self.assertIn("mode=2560x1440@200.000 output=DP-3 position=0x0 scale=1 transform=0", r.rules)
            self.assertTrue(r.panel_disabled_at_load())
            self.assertEqual(r.handlers, ["monitor.added", "monitor.removed"])
            self.assertEqual((r.reloads, r.loads), (0, 1))   # stable: nothing to do

    def test_b_undocked_leaves_panel_on(self):
        for lua in self.each():
            r = durable.run_mock(gen(docked_edp_off()), [], lua=lua)
            self.assertFalse(r.panel_disabled_at_load())
            self.assertTrue(r.panel_on)

    def test_c_other_external_leaves_panel_on(self):
        other = {"name": "DP-3", "description": "Dell Inc. U2720Q ABC123"}  # same port, other monitor
        for lua in self.each():
            self.assertFalse(durable.run_mock(gen(docked_edp_off()), [other], lua=lua).panel_disabled_at_load())

    def test_d_errors_and_junk_leave_panel_on(self):
        for lua in self.each():
            for bad in ("error", "nil", "junk", "destroyed"):
                with self.subTest(case=bad):
                    r = durable.run_mock(gen(docked_edp_off()), bad, lua=lua)
                    self.assertFalse(r.panel_disabled_at_load())
                    self.assertTrue(r.panel_on)

    def test_e_panel_kept_on_gets_normal_rule_and_no_guard(self):
        for lua in self.each():
            r = durable.run_mock(gen(both_on()), [DP3, EDP], lua=lua)
            self.assertIn("mode=1920x1080@144.005 output=eDP-1 position=0x0 scale=1 transform=0", r.rules)
            self.assertIn("mode=2560x1440@200.000 output=DP-3 position=1920x0 scale=1 transform=0", r.rules)
            self.assertFalse(r.panel_disabled_at_load())
            self.assertEqual(r.handlers, [])

    def test_f_awkward_descriptions_are_escaped_and_still_match(self):
        weird = 'Evil "Monitor" \\ with\nnewline\tand tab'
        mon = {"name": "DP-3", "description": weird}
        for lua in self.each():
            text = gen(docked_edp_off(), {"DP-3": weird, "eDP-1": BOE})
            self.assertTrue(durable.run_mock(text, [mon], lua=lua).panel_disabled_at_load())
            self.assertFalse(durable.run_mock(text, [DP3], lua=lua).panel_disabled_at_load())

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
            r = durable.run_mock(gen(layout), [], lua=lua)
            self.assertIn("disabled=true output=DP-3", r.rules)
            self.assertFalse(r.panel_disabled_at_load())
            self.assertEqual(r.handlers, [])
        for layout in (docked_edp_off(), both_on(), layout):
            self.assertNotIn('hl.monitor({ output = "eDP-1", disabled = true })', gen(layout))

    def test_scale_and_rotation_in_rules(self):
        layout = {"DP-3": state("DP-3", True, 2560, 1440, 143999, pos=(0, 0), scale=1.25, transform=1),
                  "eDP-1": state("eDP-1", False)}
        r = durable.run_mock(gen(layout), [DP3], lua=INTERPRETERS[0])
        self.assertIn("mode=2560x1440@143.999 output=DP-3 position=0x0 scale=1.25 transform=1", r.rules)


def undock(slow=0, **kw):
    return durable.run_mock(gen(docked_edp_off()), [DP3], event={
        "name": "monitor.removed", "monitor": DP3, "after": [], "at": 1, "slow": slow}, **kw)


class U1Undock(unittest.TestCase):
    """Undock/dock with a simulated clock. The live L11 failure: after the
    unplug, hl.get_monitors still listed the Samsung for a while, the reload
    saw it, and the panel was turned off again (black screen)."""

    def test_i_undock_panel_on_at_once_even_with_a_stale_list(self):
        for lua in INTERPRETERS:
            for slow in (0, 1, 3, 8, 20):
                with self.subTest(lua=lua, slow=slow):
                    # 40 s: past the gone memory's expiry (31 s), whose re-check
                    # must leave the panel on (the Samsung really is gone).
                    r = undock(slow, lua=lua, run_for=40)
                    self.assertTrue(r.panel_on)
                    self.assertEqual(r.black, 0.0)          # on at the event itself
                    self.assertGreaterEqual(r.direct, 1)
                    self.assertLessEqual(r.reloads, 2)      # no loop
                    self.assertEqual(r.pending, 0)          # everything settled

    def test_i2_direct_enable_ineffective_reload_still_rescues(self):
        for lua in INTERPRETERS:
            for slow in (0, 3, 8):
                with self.subTest(lua=lua, slow=slow):
                    r = undock(slow, lua=lua, direct_works=False)
                    self.assertTrue(r.panel_on)
                    self.assertLessEqual(r.black, 1.5)      # rescued by the reload ~1 s later
                    self.assertLessEqual(r.reloads, 2)

    def test_i3_old_behaviour_would_have_failed(self):
        # Sanity check of the model: with the "gone" memory disabled (as in the
        # old file), a stale list makes the reload turn the panel off again.
        text = gen(docked_edp_off())
        self.assertIn("and not is_gone", text)      # else the replace below is a silent no-op
        text = text.replace("and not is_gone", "")
        r = durable.run_mock(text, [DP3], event={"name": "monitor.removed", "monitor": DP3,
                                                 "after": [], "at": 1, "slow": 3},
                             lua=INTERPRETERS[0], direct_works=False)
        self.assertGreater(r.black, 1.5)

    def test_j_dock_turns_panel_off_again(self):
        for lua in INTERPRETERS:
            for slow in (0, 2):
                with self.subTest(lua=lua, slow=slow):
                    r = durable.run_mock(gen(docked_edp_off()), [], lua=lua, event={
                        "name": "monitor.added", "monitor": DP3, "after": [DP3], "at": 1, "slow": slow})
                    self.assertFalse(r.panel_on)            # docked layout again
                    self.assertEqual(r.black, 0.0)
                    self.assertLessEqual(r.reloads, 2)

    def test_k_panel_own_events_are_ignored(self):
        for lua in INTERPRETERS:
            for name in ("monitor.added", "monitor.removed"):
                with self.subTest(lua=lua, event=name):
                    r = durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua, event={
                        "name": name, "monitor": EDP, "after": [DP3], "at": 1})
                    self.assertEqual((r.reloads, r.panel_on), (0, False))

    def test_l_no_change_no_reload(self):
        dell = {"name": "HDMI-A-1", "description": "Dell Inc. U2720Q ABC123"}
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                r = durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua, event={
                    "name": "monitor.added", "monitor": dell, "after": [DP3, dell], "at": 1})
                self.assertEqual((r.reloads, r.loads), (0, 1))
                r = durable.run_mock(gen(docked_edp_off()), [], lua=lua, event={
                    "name": "monitor.added", "monitor": dell, "after": [dell], "at": 1})
                self.assertEqual(r.reloads, 0)

    def test_m_rate_limit_postpones_but_panel_still_on_at_once(self):
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                r = undock(3, lua=lua, stamp_age=1)     # a reload 1 s before the load
                self.assertTrue(r.panel_on)
                self.assertEqual(r.black, 0.0)          # the direct enable doesn't wait
                self.assertTrue(r.reload_at and min(r.reload_at) >= 4.0, r.reload_at)

    def test_n_without_timers(self):
        for lua in INTERPRETERS:
            for slow in (0, 3):
                with self.subTest(lua=lua, slow=slow):
                    r = undock(slow, lua=lua, timers=False)
                    self.assertTrue(r.panel_on)
                    self.assertEqual(r.black, 0.0)

    def test_o_unidentifiable_removed_monitor_turns_panel_on(self):
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                r = durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua, event={
                    "name": "monitor.removed", "monitor": "destroyed", "after": [], "at": 1, "slow": 3})
                self.assertTrue(r.panel_on)
                self.assertEqual(r.black, 0.0)

    def test_o2_unidentifiable_removal_survives_stale_list_and_failed_direct_enable(self):
        # Worst case: the event gives no description (or no readable object at
        # all), the direct enable doesn't work, and get_monitors stays stale.
        # The rescue reload must still leave the panel on: the handler
        # remembers the port, or "*" if even the name is unreadable. Before
        # this fix the panel stayed black for slow >= ~4 s.
        for lua in INTERPRETERS:
            for monitor in ("destroyed", {"name": "DP-3"}):
                for slow in (3, 8, 20):
                    with self.subTest(lua=lua, monitor=str(monitor), slow=slow):
                        r = durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua,
                                             direct_works=False, event={
                            "name": "monitor.removed", "monitor": monitor,
                            "after": [], "at": 1, "slow": slow})
                        self.assertTrue(r.panel_on)
                        self.assertLessEqual(r.black, 1.5)
                        self.assertLessEqual(r.reloads, 2)

    def test_o3_port_and_wildcard_memory_cleared_on_replug(self):
        lua = INTERPRETERS[0]
        for key in ("port:DP-3", "*"):
            with self.subTest(key=key):
                # Remembered, Samsung listed: the load keeps the panel on (safe side).
                r = durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua, gone={key: 5})
                self.assertFalse(r.panel_disabled_at_load())
                # Replug clears it: docked layout again.
                r = durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua, gone={key: 5}, event={
                    "name": "monitor.added", "monitor": DP3, "after": [DP3], "at": 1})
                self.assertFalse(r.panel_on)

    def test_o4_wildcard_expiry_restores_docked_layout_without_another_event(self):
        # An unreadable unplug set "*" 5 s before this load; then an unreadable
        # replug arrives (it can't clear "*", it can't tell it's the Samsung).
        # While "*" lasts the panel stays on next to the Samsung (safe side).
        # When "*" expires (25 s after the load) a re-check must notice the
        # Samsung and restore the docked layout (panel off) with no new event.
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                r = durable.run_mock(gen(docked_edp_off()), [], lua=lua, gone={"*": 5},
                                     run_for=40, event={
                    "name": "monitor.added", "monitor": "destroyed", "after": [DP3], "at": 1})
                self.assertFalse(r.panel_disabled_at_load())         # "*" still valid at load
                self.assertFalse(r.panel_on)                        # docked again at the end
                self.assertTrue(r.reload_at and min(r.reload_at) >= 25.0, r.reload_at)
                self.assertLessEqual(max(r.reload_at), 28.0)         # soon after expiry
                self.assertEqual(r.black, 0.0)
                self.assertEqual(r.pending, 0)

    def test_p_never_zero_screens_even_without_an_event(self):
        # The monitor vanishes and NO event arrives: the check after the load
        # sees no real monitor on and turns the panel on.
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                r = durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua, event={
                    "name": "monitor.silent", "monitor": DP3, "after": [], "at": 1})
                self.assertTrue(r.panel_on)
                self.assertLessEqual(r.black, 1.6)

    def test_q_gone_memory_expires_and_is_cleared_on_replug(self):
        lua = INTERPRETERS[0]
        self.assertTrue(durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua,
                                         gone={SAMSUNG: 40}).panel_disabled_at_load())   # expired
        r = durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua, gone={SAMSUNG: 5})
        self.assertFalse(r.panel_disabled_at_load())                                   # still gone
        r = durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua, gone={SAMSUNG: 5}, event={
            "name": "monitor.added", "monitor": DP3, "after": [DP3], "at": 1})
        self.assertFalse(r.panel_on)            # replug clears it -> docked layout again

    def test_r_log_records_loads_events_and_decisions(self):
        r = undock(3, lua=INTERPRETERS[0])
        for needle in ("load: monitors", "event monitor.removed DP-3", "ON directly",
                       "check (1 s after removal)", "reload 1/10 (", "-> panel on"):
            self.assertIn(needle, r.log)


FALLBACK = {"name": "FALLBACK", "description": ""}

# Names in `hl` and `hl.dsp` on the real Hyprland 0.56.2, dumped live on
# 2026-09-28 with `hyprctl eval` (pairs(hl), pairs(hl.dsp)).
REAL_HL = set("""animation bind clear_crashed_lockscreen config curve define_submap device
dispatch dsp env exec_cmd exec_scheduled_prop_refresh_immediately gesture get_active_monitor
get_active_special_workspace get_active_window get_active_workspace get_config
get_current_submap get_cursor_pos get_last_window get_last_workspace get_layers
get_loaded_plugins get_monitor get_monitor_at get_monitor_at_cursor get_monitors
get_urgent_window get_window get_windows get_workspace get_workspace_windows get_workspaces
is_key_down layer_rule layout monitor notification on permission plugin timer unbind version
window_rule workspace_rule""".split())
REAL_HL_DSP = set("""cursor dpms event exec_cmd exec_raw exit focus force_idle
force_renderer_reload global group layout no_op pass release_input_capture send_key_state
send_shortcut submap window workspace""".split())


class U1L11Run2(unittest.TestCase):
    """L11 run 2 (2026-09-28, afb4275 live): after the unplug only Hyprland's
    FALLBACK output (empty description) was listed, eDP-1 was off (so not
    listed), the direct enable had no effect, and six "reload" lines were
    never followed by a "load:" line."""

    def test_template_calls_only_functions_that_exist(self):
        import re
        text = re.sub(r"--[^\n]*", "", durable.GUARD_TEMPLATE)   # code only, not comments
        used_dsp = set(re.findall(r"\bhl\.dsp\.(\w+)", text))
        used_hl = set(re.findall(r"\bhl\.(\w+)", text))
        self.assertEqual(used_dsp - REAL_HL_DSP, set(), "hl.dsp.* names that don't exist")
        self.assertEqual(used_hl - REAL_HL, set(), "hl.* names that don't exist")

    def test_load_with_only_fallback_listed_does_not_throw(self):
        # The exact state at 13:18:57: list = [FALLBACK=""], eDP-1 absent,
        # the Samsung remembered as gone 1 s ago.
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                r = durable.run_mock(gen(docked_edp_off()), [FALLBACK], lua=lua,
                                     panel_start=False, gone={SAMSUNG: 1},
                                     direct_works=False, run_for=10)
                self.assertIn("load: monitors [FALLBACK=] -> panel on", r.log)
                self.assertNotIn("ERROR in load", r.log)
                self.assertFalse(r.panel_disabled_at_load())

    def test_run2_sequence_panel_comes_back(self):
        # The full run-2 sequence: docked, Samsung unplugged, only FALLBACK
        # left, runtime hl.monitor has no effect (as seen live).
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                r = durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua, direct_works=False,
                                     run_for=40, event={"name": "monitor.removed", "monitor": DP3,
                                                        "after": [FALLBACK], "at": 1})
                self.assertNotIn("ERROR", r.log)
                self.assertGreaterEqual(r.loads, 2, "the rescue reload never re-ran the file")
                self.assertTrue(r.panel_on)
                self.assertLessEqual(r.black, 1.5)

    def run2(self, lua, slow=0, **kw):
        return durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua, direct_works=False,
                                event={"name": "monitor.removed", "monitor": DP3,
                                       "after": [FALLBACK], "at": 1, "slow": slow}, **kw)

    def test_run2_reloads_are_bounded_over_90_s(self):
        # Reloads now really happen, so the throttle/postpone logic runs for
        # real. The run-2 unplug must settle, not keep reloading.
        for lua in INTERPRETERS:
            for slow in (0, 3, 8, 20):
                with self.subTest(lua=lua, slow=slow):
                    r = self.run2(lua, slow, run_for=90)
                    self.assertLessEqual(r.reloads, 2)
                    self.assertEqual(r.loads, r.reloads + 1)   # every reload re-ran the file
                    self.assertEqual(r.pending, 0)             # nothing left scheduled
                    self.assertTrue(r.panel_on)

    def test_mock_reload_destroys_old_timers_and_reruns_the_load(self):
        # Like the real `hyprctl reload`: a fresh load, and the old load's
        # timers never fire again (they'd still say "loaded with panel OFF").
        r = self.run2(INTERPRETERS[0], run_for=90)
        lines = r.log.splitlines()
        first_reload = next(i for i, l in enumerate(lines) if l.split("] ", 1)[1].startswith("reload 1/10 ("))
        after_reload = lines[first_reload + 1:]
        self.assertIn("load: monitors [", after_reload[0])
        self.assertIn("-> panel on", after_reload[0])
        self.assertFalse([l for l in after_reload if "loaded with panel OFF" in l])

    def test_worst_case_stops_after_10_reloads(self):
        # If even a reload can't bring the panel back, the "never zero screens"
        # rule keeps asking for reloads. They stay MIN_GAP (5 s) apart, and
        # after 10 the file gives up (and schedules nothing more).
        for lua in INTERPRETERS:
            with self.subTest(lua=lua):
                r = self.run2(lua, run_for=300, reload_fixes=False)
                self.assertEqual(r.reloads, 10)
                self.assertEqual(r.loads, 11)
                self.assertIn("reload 10/10 (", r.log)
                self.assertNotIn("reload 11/10", r.log)
                self.assertIn("giving up after 10 reloads", r.log)
                gaps = [b - a for a, b in zip(r.reload_at, r.reload_at[1:])]
                self.assertGreaterEqual(min(gaps), 4.5)   # RELOAD_AT is rounded to 0.1 s
                self.assertEqual(r.pending, 0)

    def test_giving_up_blocks_reloads_until_an_event(self):
        # Count already at the cap, no event: the rescue is refused.
        r = durable.run_mock(gen(docked_edp_off()), [FALLBACK], lua=INTERPRETERS[0],
                             panel_start=False, reload_count=10, run_for=30, direct_works=False)
        self.assertEqual(r.reloads, 0)
        self.assertIn("giving up after 10 reloads", r.log)

    def test_real_monitor_event_resets_the_count(self):
        # Count at the cap, then the Samsung (identifiable or not) is unplugged:
        # a fresh set of attempts, so the panel is rescued.
        for monitor in (DP3, "destroyed"):
            for lua in INTERPRETERS:
                with self.subTest(lua=lua, monitor=str(monitor)):
                    r = durable.run_mock(gen(docked_edp_off()), [DP3], lua=lua, direct_works=False,
                                         reload_count=10, run_for=40, event={
                                             "name": "monitor.removed", "monitor": monitor,
                                             "after": [FALLBACK], "at": 1})
                    self.assertIn("reload count reset", r.log)
                    self.assertIn("reload 1/10 (", r.log)
                    self.assertTrue(r.panel_on)
                    self.assertLessEqual(r.black, 1.5)

    def test_panel_and_fallback_events_do_not_reset_the_count(self):
        # These outputs come and go BECAUSE of our reloads; resetting on them
        # could turn the cap into an endless loop.
        for monitor in (FALLBACK, EDP):
            for name in ("monitor.added", "monitor.removed"):
                with self.subTest(monitor=monitor["name"], event=name):
                    r = durable.run_mock(gen(docked_edp_off()), [FALLBACK], lua=INTERPRETERS[0],
                                         panel_start=False, reload_count=10, run_for=30, direct_works=False,
                                         event={"name": name, "monitor": monitor,
                                                "after": [FALLBACK], "at": 1})
                    self.assertEqual(r.reloads, 0)
                    self.assertNotIn("reload count reset", r.log)
                    self.assertIn("giving up after 10 reloads", r.log)

    def test_errors_in_callbacks_are_logged(self):
        # A callback that throws must leave a line in the log, not vanish.
        text = gen(docked_edp_off()).replace(
            'recheck("1 s after removal")', 'error("boom")')
        r = undock(0, lua=INTERPRETERS[0], run_for=5, direct_works=False)
        self.assertNotIn("boom", r.log)
        r = durable.run_mock(text, [DP3], lua=INTERPRETERS[0], run_for=5, event={
            "name": "monitor.removed", "monitor": DP3, "after": [], "at": 1})
        self.assertIn("ERROR in timer:", r.log)
        self.assertIn("boom", r.log)


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

    def test_self_check_rejects_the_l11_run2_file(self):
        # The afb4275 reload call: under real-hardware conditions the undock
        # check sees the logged ERROR and the black screen.
        data = durable.kept_json(docked_edp_off(), DESC)
        text = gen(docked_edp_off()).replace('hl.exec_cmd("hyprctl reload")',
                                             'hl.dispatch(hl.dsp.reload_config())')
        problems = durable.self_check(text, data)
        self.assertTrue(any("logged an error" in p and "reload_config" in p for p in problems), problems)
        self.assertTrue(any(p.startswith("undock") and "panel on=False" in p for p in problems), problems)

    def test_self_check_rejects_a_file_whose_replug_keeps_the_panel_on(self):
        data = durable.kept_json(docked_edp_off(), DESC)
        line = "  if want ~= PANELS_OFF then request_reload(reason) end\n"
        text = gen(docked_edp_off())
        self.assertIn(line, text)
        problems = durable.self_check(text.replace(line, ""), data)
        self.assertIn("replug: the laptop panel stays on (docked layout not restored)", problems)

    def test_keeping_the_same_layout_again_leaves_prev_alone(self):
        a, b = gen(docked_edp_off()), gen(both_on())
        self.assertTrue(durable.keep(a, durable.kept_json(docked_edp_off(), DESC), self.dir))
        self.assertTrue(durable.keep(b, durable.kept_json(both_on(), DESC), self.dir))
        # b again, generated a day later: only the timestamps differ.
        later = datetime.datetime(2026, 9, 24, 9, 30)
        b2 = durable.generate_lua(both_on(), DESC, now=later)
        self.assertNotEqual(b, b2)
        self.assertFalse(durable.keep(b2, durable.kept_json(both_on(), DESC, now=later), self.dir))
        self.assertEqual((self.dir / "monitors.lua").read_text(), b2)
        self.assertEqual((self.dir / "monitors.lua.prev").read_text(), a)   # Undo still goes to a
        self.assertEqual(durable.load_json(self.dir, prev=True)["states"], docked_edp_off())

    def test_failed_persist_restores_all_four_files(self):
        # Before: current = a, .prev = placeholder. A persist of b whose
        # verification fails must leave exactly that, NOT b in .prev (a later
        # Undo would load the rejected file).
        a = gen(docked_edp_off())
        durable.keep(a, durable.kept_json(docked_edp_off(), DESC), self.dir)
        before = {p.name: p.read_text() for p in self.dir.iterdir()}
        with mock.patch("durable.dofile_line_present", return_value=True), \
             mock.patch("durable.hyprland_descriptions", return_value=DESC), \
             mock.patch("durable.reload_hyprland") as reload, \
             mock.patch("durable.verify_durable", side_effect=[["DP-3 at 1920,0, expected 0,0"], []]):
            problems = durable.persist(both_on(), self.dir, log=lambda *_: None)
        self.assertEqual(reload.call_count, 2)
        self.assertIn("previous rules restored and verified", problems)
        self.assertEqual({p.name: p.read_text() for p in self.dir.iterdir()}, before)

    def test_failed_persist_on_first_keep_removes_the_new_files(self):
        with mock.patch("durable.dofile_line_present", return_value=True), \
             mock.patch("durable.hyprland_descriptions", return_value=DESC), \
             mock.patch("durable.reload_hyprland"), \
             mock.patch("durable.verify_durable", side_effect=[["mismatch"], []]):
            durable.persist(docked_edp_off(), self.dir, log=lambda *_: None)
        self.assertEqual(self.files(), [])

    def test_self_check_refuses_without_lua(self):
        with mock.patch("durable.shutil.which", return_value=None):
            self.assertTrue(durable.self_check(gen(docked_edp_off()), durable.kept_json(docked_edp_off(), DESC)))


if __name__ == "__main__":
    unittest.main()
