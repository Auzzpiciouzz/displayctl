"""Safety pieces shared by displayctl.py (CLI) and gui.py.

  take_lock()       only one display change may be pending at a time
  start_watchdog()  launch watchdog.py, the separate revert process
  tell()            send it a command: "applied", "keep" or "revert"
  kanshi_warning()  a warning text while kanshi runs (it overrides us)

See watchdog.py for what the watchdog does with those commands.
"""

import fcntl
import subprocess
import sys
from pathlib import Path

from backend import CACHE_DIR, LAST_GOOD

HERE = Path(__file__).resolve().parent
LOCK_FILE = CACHE_DIR / "lock"
WATCHDOG_LOG = CACHE_DIR / "watchdog.log"


class LockBusy(Exception):
    """Another display change (or its watchdog) is still in progress."""


def take_lock():
    """Allow only one pending change at a time. Returns the open lock file.

    flock() holds the lock as long as the file stays open, in this process
    or in any child that inherits the file descriptor (the watchdog does). So
    a second change is refused until the watchdog has finished too.
    Raises LockBusy if someone else holds it.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise LockBusy("another display change is still pending (watchdog running)")
    return lock


def start_watchdog(lock, confirm_seconds, kill_pid=None):
    """Launch watchdog.py with a pipe to its stdin. Returns the Popen object.

    confirm_seconds: how long the caller may take to say "keep" or "revert"
    after "applied"; the watchdog adds its own grace period on top.
    kill_pid: the GUI passes its PID, so a hung GUI can be killed on the
    deadline (see watchdog.py).
    """
    log = open(WATCHDOG_LOG, "a")
    args = [sys.executable, str(HERE / "watchdog.py"), str(LAST_GOOD), str(confirm_seconds)]
    if kill_pid is not None:
        args.append(str(kill_pid))
    return subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        # New session: Ctrl+C / closing the terminal only hits the caller,
        # not the watchdog that has to clean up after it.
        start_new_session=True,
        # Hand over the lock so it stays held until the watchdog is done.
        pass_fds=(lock.fileno(),),
        cwd=HERE,
    )


def tell(watchdog, command):
    """Send one command line to the watchdog."""
    try:
        watchdog.stdin.write(command + "\n")
        watchdog.stdin.flush()
    except (BrokenPipeError, ValueError):
        pass  # it already exited (or the pipe is closed); its log says why


def kanshi_pids(proc_dir="/proc"):
    """PIDs of running kanshi processes, sorted.

    Reads /proc/<pid>/comm (the process name, exact match) instead of running
    pgrep: no subprocess, and no pattern that could match our own command
    line (see the pkill -f rule in CLAUDE.md).
    """
    pids = []
    for comm in Path(proc_dir).glob("[0-9]*/comm"):
        try:
            if comm.read_text().strip() == "kanshi":
                pids.append(int(comm.parent.name))
        except (OSError, ValueError):
            pass  # the process exited while we looked, or not a PID directory
    return sorted(pids)


def kanshi_warning(proc_dir="/proc", short=False):
    """A warning to show while kanshi runs, or None.

    kanshi applies its own profiles on every hotplug and stays connected to
    the output-management protocol; while it's connected, Hyprland silently
    ignores other clients' changes (CLAUDE.md, HARD RULE). So a change we
    make may not take effect, or kanshi may undo it later. We only warn:
    stopping it is the user's decision.
    """
    pids = kanshi_pids(proc_dir)
    if not pids:
        return None
    ids = " ".join(str(p) for p in pids)
    if short:  # one line, for the GUI banner
        return f"kanshi is running (PID {ids}): display changes may be ignored or undone. Stop it with: kill {ids}"
    return (f"kanshi is running (PID {ids}). It applies its own profiles on every plug/unplug,"
            f" and while it runs Hyprland may silently ignore this change or kanshi may undo it"
            f" later. To stop it: kill {ids}")
