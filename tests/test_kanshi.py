"""The kanshi warning (safety.kanshi_warning), in the CLI and the GUI.

kanshi overrides displayctl (CLAUDE.md, HARD RULE), so both warn while it
runs. No real processes are touched: a fake /proc in a temp directory.

Run: .venv/bin/python -m unittest discover -s tests
"""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import displayctl  # noqa: E402
import safety  # noqa: E402


def fake_proc(tmp, processes):
    """Make tmp look like /proc: {pid: comm}; non-PID entries are added too."""
    for pid, comm in processes.items():
        d = Path(tmp) / str(pid)
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
    (Path(tmp) / "self").mkdir()           # not a PID: must be skipped
    (Path(tmp) / "999999").mkdir()         # exited: no comm file
    return tmp


class KanshiDetection(unittest.TestCase):
    def test_finds_only_exact_kanshi(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_proc(tmp, {73906: "kanshi", 12: "kanshictl", 13: "bash", 14: "kanshi-extra",
                            15: "kanshi"})
            self.assertEqual(safety.kanshi_pids(tmp), [15, 73906])
            text = safety.kanshi_warning(tmp)
            self.assertIn("kanshi is running (PID 15 73906)", text)
            self.assertIn("kill 15 73906", text)
            short = safety.kanshi_warning(tmp, short=True)
            self.assertNotIn("\n", short)
            self.assertIn("kill 15 73906", short)

    def test_no_warning_without_kanshi(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_proc(tmp, {13: "bash"})
            self.assertEqual(safety.kanshi_pids(tmp), [])
            self.assertIsNone(safety.kanshi_warning(tmp))


class CliWarning(unittest.TestCase):
    def test_changing_commands_warn_list_doesnt(self):
        with mock.patch("safety.kanshi_warning", return_value="kanshi is running (PID 7)"):
            for command in ("set", "persist", "undo"):
                out = io.StringIO()
                self.assertTrue(displayctl.warn_if_kanshi(command, out), command)
                self.assertIn("WARNING: kanshi is running (PID 7)", out.getvalue())
            out = io.StringIO()
            self.assertFalse(displayctl.warn_if_kanshi("list", out))
            self.assertEqual(out.getvalue(), "")

    def test_silent_without_kanshi(self):
        with mock.patch("safety.kanshi_warning", return_value=None):
            out = io.StringIO()
            self.assertFalse(displayctl.warn_if_kanshi("set", out))
            self.assertEqual(out.getvalue(), "")

    def test_main_warns_before_running_the_command_and_still_runs_it(self):
        calls = []
        with mock.patch("safety.kanshi_warning", return_value="kanshi is running (PID 7)"), \
             mock.patch.object(sys, "argv", ["displayctl.py", "persist"]), \
             mock.patch("displayctl.cmd_persist", side_effect=lambda a: calls.append("persist")), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            displayctl.main()
        self.assertEqual(calls, ["persist"])                  # a warning, not a refusal
        self.assertIn("WARNING: kanshi is running", err.getvalue())


class GuiBanner(unittest.TestCase):
    """MainWindow.check_kanshi with a stub banner (no display needed)."""

    class Banner:
        def __init__(self):
            self.title, self.revealed = None, False

        def set_title(self, t):
            self.title = t

        def set_revealed(self, r):
            self.revealed = r

    def test_banner_follows_kanshi(self):
        import gui
        window = mock.Mock(kanshi_banner=self.Banner())
        with mock.patch("safety.kanshi_warning", return_value="kanshi is running (PID 7): ..."):
            self.assertTrue(gui.MainWindow.check_kanshi(window))   # keeps the timer running
        self.assertTrue(window.kanshi_banner.revealed)
        self.assertIn("PID 7", window.kanshi_banner.title)
        with mock.patch("safety.kanshi_warning", return_value=None):
            gui.MainWindow.check_kanshi(window)
        self.assertFalse(window.kanshi_banner.revealed)             # gone once kanshi stops


if __name__ == "__main__":
    unittest.main()
