#!/usr/bin/env python3
"""Revert watchdog: restores a snapshot unless told to keep the new settings.

displayctl.py starts this as a SEPARATE PROCESS before applying anything, and
talks to it through a pipe connected to our stdin. One word per line:

  applied   the change is live; start the confirmation countdown
  keep      the user confirmed; exit without touching anything
  revert    restore the snapshot now

Because it is a separate process (in its own session, so Ctrl+C in the
terminal doesn't reach it), it still reverts when displayctl.py goes wrong:

  displayctl.py crashes or is killed -> the pipe closes (EOF)   -> revert now
  displayctl.py hangs                 -> no message by deadline -> revert

Usage (normally only called by displayctl.py / gui.py):
  watchdog.py SNAPSHOT.json CONFIRM_SECONDS [KILL_PID]

KILL_PID (the GUI passes its own PID): on Hyprland, a revert from another
client is ignored while the client that applied the change is still
connected. A GUI that HANGS keeps its connection open, so on a deadline
the watchdog kill -9s it first; its connection then closes and the revert
works.
Output goes to ~/.cache/displayctl/watchdog.log.
"""

import json
import os
import select
import signal
import sys
import time

from backend import OutputManager, WaylandError, desired_from_snapshot, verify_state

# If displayctl.py never even reports "applied" (it hung while applying),
# give up waiting after this long and revert.
APPLY_TIMEOUT = 60
# Extra time after displayctl.py's own countdown ends, so that normally
# displayctl.py decides and this deadline is only a fallback.
GRACE = 5


def log(msg):
    # stdout is redirected to the log file by displayctl.py.
    print(f"[{time.strftime('%H:%M:%S')}] watchdog: {msg}", flush=True)


# Bytes read from the pipe but not yet split into lines.
_pending = b""


def read_command(deadline):
    """Wait for one line on stdin until `deadline`.

    Returns the command, "EOF" if the pipe closed, or None on timeout.

    We keep our own buffer and use os.read() instead of sys.stdin.readline():
    readline() may pull several lines into Python's hidden buffer at once,
    and select() only watches the pipe itself, so it wouldn't notice that a
    second command was already waiting in that buffer.
    """
    global _pending
    fd = sys.stdin.fileno()
    while b"\n" not in _pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        # select() waits until the pipe has data (or EOF) or the timeout passes.
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            return None
        chunk = os.read(fd, 1024)
        if not chunk:  # zero bytes means the other end closed the pipe
            return "EOF"
        _pending += chunk
    line, _pending = _pending.split(b"\n", 1)
    return line.decode().strip()


def apply_snapshot(desired):
    """Send the whole snapshot as one configuration. Retries on cancel."""
    for attempt in range(1, 4):
        try:
            # A fresh connection each time: new serial, up-to-date heads.
            # (run_configuration always sends every property, never a diff.)
            with OutputManager() as om:
                result = om.run_configuration(desired, "apply")
        except WaylandError as e:
            log(f"  apply attempt {attempt}: {e}")
            result = "error"
        log(f"  apply attempt {attempt}: {result}")
        if result == "succeeded":
            return True
        time.sleep(0.5)
    return False


def verify(desired):
    """Check the revert really took effect (see backend.verify_state)."""
    problems, missing = verify_state(desired)
    for name in missing:
        log(f"  verify: {name} is no longer connected, skipping")
    return problems


def revert(snapshot):
    """Restore the snapshot, check it worked, retry once if not."""
    desired = desired_from_snapshot(snapshot)
    for round_ in (1, 2):
        log(f"revert round {round_}")
        if not apply_snapshot(desired):
            log("REVERT FAILED: compositor would not accept the snapshot")
            return False
        try:
            problems = verify(desired)
        except WaylandError as e:
            problems = [f"could not re-read outputs: {e}"]
        if not problems:
            # Check once more a bit later: enabling/disabling outputs can make
            # Hyprland re-apply its config rules a moment afterwards.
            time.sleep(2)
            try:
                problems = verify(desired)
            except WaylandError as e:
                problems = [f"could not re-read outputs: {e}"]
            if not problems:
                log("revert verified (checked twice): outputs match the snapshot")
                return True
            log("revert looked fine, but a second check 2 s later disagrees:")
        for p in problems:
            log(f"  verify mismatch: {p}")
    log("!!! REVERT FAILED: outputs still don't match the snapshot after 2 tries !!!")
    return False


def kill_hung_client(pid):
    """kill -9 the (hung) GUI so its Wayland connection closes.

    Checks /proc first that the PID still belongs to gui.py: PIDs get
    reused, and we must never kill an unrelated process.
    """
    if pid is None:
        return
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        log(f"client {pid} is already gone")
        return
    if "gui.py" not in cmdline:
        log(f"not killing {pid}: it's not gui.py any more ({cmdline.strip()!r})")
        return
    log(f"client {pid} didn't answer in time: kill -9 so its connection closes")
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as e:
        log(f"kill failed: {e}")
        return
    # Wait (up to 5 s) until it's really gone, so the compositor has seen the
    # connection close before we revert.
    for _ in range(50):
        if not os.path.exists(f"/proc/{pid}"):
            break
        time.sleep(0.1)
    time.sleep(0.3)


def main():
    snapshot_path, confirm_seconds = sys.argv[1], float(sys.argv[2])
    kill_pid = int(sys.argv[3]) if len(sys.argv) > 3 else None
    with open(snapshot_path) as f:
        snapshot = json.load(f)
    log(f"started, snapshot {snapshot_path}")

    # Phase 1: wait for "applied" (or a reason to revert straight away).
    deadline = time.monotonic() + APPLY_TIMEOUT
    while True:
        cmd = read_command(deadline)
        if cmd == "applied":
            break
        if cmd == "keep":
            # displayctl.py's apply was cancelled, so nothing changed.
            log("nothing was applied, exiting")
            return 0
        if cmd == "revert":
            log("revert requested before confirmation")
            return 0 if revert(snapshot) else 1
        if cmd == "EOF":
            log("displayctl exited before confirming -> reverting")
            return 0 if revert(snapshot) else 1
        if cmd is None:
            log("no 'applied' before timeout -> reverting")
            kill_hung_client(kill_pid)
            return 0 if revert(snapshot) else 1
        log(f"ignoring unknown command {cmd!r}")

    # Phase 2: the change is live. Wait for the user's decision.
    log("change applied, waiting for confirmation")
    deadline = time.monotonic() + confirm_seconds + GRACE
    while True:
        cmd = read_command(deadline)
        if cmd == "keep":
            log("confirmed, keeping new settings")
            return 0
        if cmd == "revert":
            log("not confirmed -> reverting")
        elif cmd == "EOF":
            log("displayctl exited without confirming -> reverting")
        elif cmd is None:
            log("confirmation deadline passed -> reverting")
            kill_hung_client(kill_pid)
        else:
            log(f"ignoring unknown command {cmd!r}")
            continue
        return 0 if revert(snapshot) else 1


if __name__ == "__main__":
    sys.exit(main())
