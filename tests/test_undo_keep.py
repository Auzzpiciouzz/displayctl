"""Undo of a permanent Keep: `displayctl.py undo --swap-only` and the GUI's
KeepUndoer (Undo Keep button / toast). No Hyprland, no display: the file
swaps, reloads and verifications are mocked, and the GUI dialog is a stub.

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
import gui  # noqa: E402


class SwapOnly(unittest.TestCase):
    """displayctl.swap_only(): swap, reload, verify; swap back on failure."""

    def run_swap(self, verify_results):
        with mock.patch("durable.undo") as undo, \
             mock.patch("durable.reload_hyprland") as reload, \
             mock.patch("durable.load_json", return_value={}), \
             mock.patch("durable.verify_durable", side_effect=verify_results), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = 0
            try:
                displayctl.swap_only()
            except SystemExit as e:
                code = e.code
        return code, undo.call_count, reload.call_count, out.getvalue() + err.getvalue()

    def test_swap_verified(self):
        code, undos, reloads, text = self.run_swap([[]])
        self.assertEqual((code, undos, reloads), (0, 1, 1))
        self.assertIn("reloaded and verified", text)

    def test_swap_not_verified_swaps_back(self):
        code, undos, reloads, text = self.run_swap([["DP-3 at 1920,0, expected 0,0"], []])
        self.assertEqual((code, undos, reloads), (1, 2, 2))
        self.assertIn("Swapped back", text)

    def test_neither_verifies(self):
        code, undos, reloads, text = self.run_swap([["a"], ["b"]])
        self.assertEqual((code, undos, reloads), (1, 2, 2))
        self.assertIn("DIDN'T VERIFY EITHER", text)

    def test_nothing_to_undo(self):
        with mock.patch("durable.undo", side_effect=FileNotFoundError("nothing to undo")), \
             mock.patch("durable.reload_hyprland") as reload, \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                displayctl.swap_only()
        reload.assert_not_called()

    def test_cmd_undo_swap_only_takes_the_lock_and_never_asks(self):
        args = argparse.Namespace(swap_only=True, auto_keep=False, auto_revert_after=None)
        lock = mock.Mock()
        with mock.patch("displayctl.on_hyprland", return_value=True), \
             mock.patch("durable.dofile_line_present", return_value=True), \
             mock.patch("displayctl.take_lock", return_value=lock), \
             mock.patch("displayctl.swap_only") as swap, \
             mock.patch("displayctl.confirm") as confirm:
            displayctl.cmd_undo(args)
        swap.assert_called_once()
        confirm.assert_not_called()
        lock.close.assert_called_once()


class Dialog:
    """Stands in for Adw.AlertDialog."""
    def __init__(self):
        self.body, self.closed = None, False

    def set_body(self, text):
        self.body = text

    def force_close(self):
        self.closed = True


class KeepUndoerTest(unittest.TestCase):
    """The GUI flow, with `undo --swap-only` results scripted."""

    def make(self, swap_results, applier_busy=False):
        window = mock.Mock()
        window.applier.busy = applier_busy
        undoer = gui.KeepUndoer(window)
        undoer.swaps = 0
        results = list(swap_results)

        def fake_swap(done):
            undoer.swaps += 1
            done(*results.pop(0))
        undoer._swap = fake_swap
        undoer._present_question = Dialog
        self.addCleanup(lambda: undoer._countdown_id and gui.GLib.source_remove(undoer._countdown_id))
        return undoer, window

    def test_keep_swaps_once(self):
        undoer, window = self.make([(True, "ok")])
        undoer.start()
        self.assertTrue(undoer.busy)
        self.assertIn("15 s", undoer.dialog.body)
        undoer._decide(keep=True)
        self.assertEqual(undoer.swaps, 1)
        self.assertFalse(undoer.busy)
        window.toast.assert_called_with("Previous layout kept (Undo Keep again to redo)")

    def test_revert_swaps_back(self):
        undoer, window = self.make([(True, "ok"), (True, "ok")])
        undoer.start()
        undoer._decide(keep=False)
        self.assertEqual(undoer.swaps, 2)
        self.assertFalse(undoer.busy)
        window.toast.assert_called_with("Back to the layout before the undo")

    def test_timeout_reverts_and_a_late_click_does_nothing(self):
        undoer, window = self.make([(True, "ok"), (True, "ok")])
        undoer.start()
        dialog = undoer.dialog
        for _ in range(gui.CONFIRM_SECONDS):
            undoer._tick()
        self.assertTrue(dialog.closed)
        self.assertEqual(undoer.swaps, 2)
        undoer._decide(keep=True)          # a click that raced the timeout
        self.assertEqual(undoer.swaps, 2)

    def test_failed_first_swap_alerts_and_asks_nothing(self):
        undoer, window = self.make([(False, "Swapped back; the layout is as it was before (verified).")])
        undoer.start()
        self.assertIsNone(undoer.dialog)
        self.assertFalse(undoer.busy)
        window.alert.assert_called_once()
        self.assertIn("Swapped back", window.alert.call_args[0][1])

    def test_failed_swap_back_alerts(self):
        undoer, window = self.make([(True, "ok"), (False, "SWAP BACK DIDN'T VERIFY EITHER")])
        undoer.start()
        undoer._decide(keep=False)
        window.alert.assert_called_once()
        self.assertFalse(undoer.busy)

    def test_not_while_an_apply_is_running(self):
        undoer, window = self.make([], applier_busy=True)
        undoer.start()
        self.assertEqual(undoer.swaps, 0)
        self.assertFalse(undoer.busy)

    def test_available(self):
        for hypr, line, prev, want in [(True, True, True, True), (False, True, True, False),
                                       (True, False, True, False), (True, True, False, False)]:
            with self.subTest(hypr=hypr, line=line, prev=prev), \
                 mock.patch("backend.on_hyprland", return_value=hypr), \
                 mock.patch("durable.dofile_line_present", return_value=line), \
                 mock.patch("durable.undo_available", return_value=prev):
                self.assertEqual(gui.KeepUndoer.available(), want)


class UndoButtonState(unittest.TestCase):
    def state(self, pending=None, applier_busy=False, undo_busy=False, available=True):
        window = mock.Mock(pending=pending or {})
        window.applier.busy, window.undoer.busy = applier_busy, undo_busy
        with mock.patch.object(gui.KeepUndoer, "available", return_value=available):
            gui.MainWindow.update_actions(window)
        return window.undo_keep_button.set_sensitive.call_args[0][0]

    def test_enabled_only_when_idle_with_nothing_pending_and_a_prev(self):
        self.assertTrue(self.state())
        self.assertFalse(self.state(pending={"DP-3": {}}))
        self.assertFalse(self.state(applier_busy=True))
        self.assertFalse(self.state(undo_busy=True))
        self.assertFalse(self.state(available=False))


if __name__ == "__main__":
    unittest.main()
