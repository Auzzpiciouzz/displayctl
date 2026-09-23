"""Safety pieces shared by displayctl.py (CLI) and gui.py.

  take_lock()       only one display change may be pending at a time
  start_watchdog()  launch watchdog.py, the separate revert process
  tell()            send it a command: "applied", "keep" or "revert"

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
