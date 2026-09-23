#!/usr/bin/env python3
"""displayctl: change display settings safely from the command line.

  displayctl.py list
  displayctl.py set OUTPUT [--mode WxH[@Hz]] [--pos X,Y] [--scale S]
                           [--transform T] [--enable | --disable] [--dry-run]

How `set` stays safe:
  1. snapshot the current state -> ~/.cache/displayctl/last-good.json
  2. test()   the new configuration (the compositor validates it; nothing changes)
              -- skipped on Hyprland, where test() has side effects
  3. start watchdog.py, a separate process that will restore the snapshot
     unless we tell it "keep"
  4. apply()  the new configuration
  5. ask "Keep these settings?" for 15 seconds; anything but "y" reverts

If this process crashes or hangs during step 5, the watchdog still reverts.
"""

import argparse
import fcntl
import os
import re
import select
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

from backend import (
    CACHE_DIR, LAST_GOOD, TRANSFORM_BY_NAME, TRANSFORMS,
    OutputManager, WaylandError, desired_from_snapshot, hyprland_disagreements,
    on_hyprland, save_json, verify_state,
)

CONFIRM_SECONDS = 15
HERE = Path(__file__).resolve().parent
LOCK_FILE = CACHE_DIR / "lock"
WATCHDOG_LOG = CACHE_DIR / "watchdog.log"

# What each configuration result means, in plain words.
RESULT_MESSAGES = {
    "failed": "The compositor rejected this configuration.",
    "cancelled": (
        "The compositor cancelled: the outputs changed while we were working "
        "(monitor plugged/unplugged?). Nothing was changed. Run the command again."
    ),
}


def die(msg):
    print(f"displayctl: {msg}", file=sys.stderr)
    sys.exit(1)


# --- Formatting ------------------------------------------------------------------


def describe(state):
    """One-line summary of a head state dict (as made by Head.to_state())."""
    if not state["enabled"]:
        return "disabled"
    m = state["mode"]
    mode = f"{m['width']}x{m['height']}@{m['refresh_mhz'] / 1000:g}Hz" if m else "?"
    x, y = state["position"]
    return (
        f"{mode}  pos {x},{y}  scale {state['scale']:g}  "
        f"transform {TRANSFORMS.get(state['transform'], state['transform'])}"
    )


def cmd_list(args):
    with OutputManager() as om:
        for head in om.heads:
            print(f"=== {head.name} ===  {head.description}")
            print(f"  {describe(head.to_state())}")
            print(f"  modes ({len(head.modes)}):")
            for mode in head.modes:
                marker = "*" if mode is head.current_mode else " "
                print(f"    {marker} {mode}")
            print()


# --- Parsing option values ------------------------------------------------------


def parse_mode(text):
    """'2560x1440@144' -> (2560, 1440, 144000). The @Hz part is optional."""
    match = re.fullmatch(r"(\d+)x(\d+)(?:@(\d+(?:\.\d+)?))?(?:hz)?", text.lower())
    if not match:
        raise argparse.ArgumentTypeError("expected WxH or WxH@Hz, e.g. 2560x1440@144")
    w, h, hz = match.groups()
    return int(w), int(h), None if hz is None else round(float(hz) * 1000)


def parse_pos(text):
    """'1920,0' -> (1920, 0). Negative values are allowed."""
    match = re.fullmatch(r"(-?\d+),(-?\d+)", text)
    if not match:
        raise argparse.ArgumentTypeError("expected X,Y, e.g. 2560,0")
    return int(match.group(1)), int(match.group(2))


def parse_scale(text):
    scale = float(text)
    if scale <= 0:
        raise argparse.ArgumentTypeError("scale must be greater than 0")
    return scale


# --- Building the requested state --------------------------------------------------


def build_desired(om, args):
    """Return {head name: state}: every head as it is now, with the target changed."""
    desired = {h.name: h.to_state() for h in om.heads}
    head = om.head(args.output)
    if head is None:
        die(f"no output named {args.output!r} (have: {', '.join(desired)})")
    state = desired[head.name]

    if args.disable:
        if any((args.mode, args.pos, args.scale, args.transform)):
            die("--disable can't be combined with other settings")
        state["enabled"] = False
        state["mode"] = None
        return desired

    if not head.enabled and not args.enable:
        die(f"{head.name} is disabled; add --enable to turn it on")

    if args.enable and not head.enabled:
        # Sensible defaults for a head coming back on: its preferred mode,
        # scale 1, placed to the right of everything that's already on.
        state["enabled"] = True
        preferred = head.preferred_mode()
        state["mode"] = {
            "width": preferred.width, "height": preferred.height,
            "refresh_mhz": preferred.refresh_mhz,
        }
        right_edge = max(
            (h.position[0] + h.logical_size()[0] for h in om.heads if h.enabled),
            default=0,
        )
        state["position"] = [right_edge, 0]
        state["scale"] = 1.0
        state["transform"] = 0

    if args.mode:
        w, h, refresh_mhz = args.mode
        mode = head.find_mode(w, h, refresh_mhz)
        if mode is None:
            rates = sorted({m.refresh_mhz / 1000 for m in head.modes if (m.width, m.height) == (w, h)})
            if rates:
                die(f"{head.name} has no {w}x{h} mode at that rate; available: "
                    + ", ".join(f"{r:g}" for r in rates) + " Hz")
            sizes = sorted({(m.width, m.height) for m in head.modes}, reverse=True)
            die(f"{head.name} has no {w}x{h} mode; available: "
                + ", ".join(f"{a}x{b}" for a, b in sizes))
        state["mode"] = {"width": mode.width, "height": mode.height,
                         "refresh_mhz": mode.refresh_mhz}
    if args.pos:
        state["position"] = list(args.pos)
    if args.scale:
        state["scale"] = args.scale
    if args.transform:
        state["transform"] = TRANSFORM_BY_NAME[args.transform]
    return desired


# --- Safety pieces -------------------------------------------------------------------


def take_lock():
    """Allow only one pending change at a time.

    flock() holds the lock as long as the file stays open, in this process
    or in any child that inherits the file descriptor (the watchdog does). So
    a second `set` is refused until the watchdog has finished too.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        die("another display change is still pending (watchdog running); try again shortly")
    return lock


def start_watchdog(lock, confirm_seconds):
    """Launch watchdog.py with a pipe to its stdin. Returns the Popen object."""
    log = open(WATCHDOG_LOG, "a")
    return subprocess.Popen(
        [sys.executable, str(HERE / "watchdog.py"), str(LAST_GOOD), str(confirm_seconds)],
        stdin=subprocess.PIPE,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        # New session: Ctrl+C / closing the terminal only hits displayctl.py,
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
    except BrokenPipeError:
        pass  # it already exited; its log says why


def wait_for_revert(watchdog):
    print("Reverting...")
    try:
        code = watchdog.wait(timeout=30)
    except subprocess.TimeoutExpired:
        die(f"watchdog is still working; see {WATCHDOG_LOG}")
    if code == 0:
        print("Reverted to the previous settings.")
    else:
        die(f"REVERT FAILED (see {WATCHDOG_LOG}). Snapshot: {LAST_GOOD}")


def confirm(seconds):
    """Countdown prompt. True only if the user presses 'y' in time.

    The terminal goes into "cbreak" mode so a single key press is read
    immediately (no Enter needed). Without a terminal we can't ask, so the
    answer is "no" and the change reverts.
    """
    if not sys.stdin.isatty():
        print("No terminal to confirm on: reverting.")
        return False
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        termios.tcflush(fd, termios.TCIFLUSH)  # ignore keys pressed earlier
        for remaining in range(seconds, 0, -1):
            print(f"\rKeep these display settings? [y/N] reverting in {remaining:2d}s ",
                  end="", flush=True)
            ready, _, _ = select.select([fd], [], [], 1)
            if ready:
                key = os.read(fd, 1).decode(errors="ignore")
                print(key)
                return key.lower() == "y"
        print()
        return False
    finally:
        # Always give the terminal back its normal settings.
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


# --- set ---------------------------------------------------------------------------


def cmd_set(args):
    lock = take_lock()
    om = OutputManager()
    try:
        desired = build_desired(om, args)
        target = args.output
        before = om.head(target).to_state()
        after = desired[target]

        if describe(before) == describe(after):
            print(f"{target} is already {describe(after)}; nothing to do.")
            return
        if not any(s["enabled"] for s in desired.values()):
            die("refusing to disable every output")

        print(f"{target}:")
        print(f"  now: {describe(before)}")
        print(f"  new: {describe(after)}")

        # 0. Guard (Hyprland only): the protocol and hyprctl must agree on
        #    the current state. We restate every head in full, so a wrong
        #    view would be applied for real. Never guess which is right.
        problems = hyprland_disagreements(om.heads)
        if problems:
            print("REFUSING TO APPLY: the protocol and hyprctl disagree:", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            die("fix the mismatch first (see CLAUDE.md findings); nothing was changed")

        # 1. Snapshot, before anything else touches the displays.
        snapshot = om.snapshot()
        save_json(LAST_GOOD, snapshot)
        print(f"Saved current state to {LAST_GOOD}")

        # 2. Test: the compositor checks the configuration but changes nothing.
        #    EXCEPT on Hyprland (0.56.2): there test() makes it re-apply its
        #    config rules to all outputs, which can switch a disabled
        #    output back on. So on Hyprland we rely on our own checks
        #    (build_desired: mode exists, scale > 0, not everything off)
        #    plus the hyprctl cross-check above. See
        #    docs/hyprland-persistence.md, "test() is not side-effect-free".
        if on_hyprland():
            print("test() skipped on Hyprland: it re-applies Hyprland's config "
                  "rules as a side effect. Local checks and hyprctl cross-check passed.")
        else:
            result = om.run_configuration(desired, "test")
            if result != "succeeded":
                die(f"test {result}. {RESULT_MESSAGES[result]} Nothing was changed.")
            print("Test passed.")
        if args.dry_run:
            print("--dry-run: stopping before apply.")
            return

        # 3. Start the watchdog BEFORE applying, so it exists if we crash mid-apply.
        # The watchdog's deadline must outlast our own wait, however long it is.
        wait_seconds = args.auto_revert_after or args.self_revert_test or CONFIRM_SECONDS
        watchdog = start_watchdog(lock, max(CONFIRM_SECONDS, wait_seconds))

        # 4. Apply.
        try:
            result = om.run_configuration(desired, "apply")
        except WaylandError as e:
            print(f"apply: {e}")
            tell(watchdog, "revert")
            wait_for_revert(watchdog)
            sys.exit(1)

        if result == "cancelled":
            print(RESULT_MESSAGES["cancelled"])
            tell(watchdog, "keep")  # nothing was applied, nothing to undo
            sys.exit(1)
        if result == "failed":
            # The compositor should have undone any partial change itself.
            # Restore the snapshot anyway, to be sure.
            print(f"apply failed. {RESULT_MESSAGES['failed']}")
            tell(watchdog, "revert")
            wait_for_revert(watchdog)
            sys.exit(1)

        # 5. It's live. Ask the user, with the watchdog as a backup.
        tell(watchdog, "applied")
        if args.self_revert_test is not None:
            # Experiment: this must run BEFORE the early close below, because
            # it's about reverting while the applying connection is still open.
            self_revert_test(om, watchdog, snapshot, args)
            return
        # Close our Wayland connection now. REQUIRED, not tidiness: on
        # Hyprland, while the client that applied a change stays connected,
        # a revert applied by another client (the watchdog) is answered
        # "succeeded" but not carried out. Tested: 144 -> 180 -> revert
        # worked with this close and failed without it.
        om.close()
        if args.auto_keep:
            # Testing aid: confirm without a terminal, like pressing "y".
            print("--auto-keep: keeping")
            keep = True
        elif args.auto_revert_after is not None:
            # Testing aid: no prompt, just hold the change for a while and
            # then revert, like a user who waited and pressed "n".
            print(f"--auto-revert-after: holding for {args.auto_revert_after}s, then reverting")
            time.sleep(args.auto_revert_after)
            keep = False
        else:
            try:
                keep = confirm(CONFIRM_SECONDS)
            except KeyboardInterrupt:
                print()
                keep = False
        if keep:
            tell(watchdog, "keep")
            watchdog.wait(timeout=10)
            # Re-read on a fresh connection: our old one is closed, and its
            # state was from just after apply (possibly before all the
            # compositor's update events had arrived).
            with OutputManager() as fresh:
                print(f"Kept. {target}: {describe(fresh.head(target).to_state())}")
        else:
            tell(watchdog, "revert")
            wait_for_revert(watchdog)
    finally:
        om.close()


def self_revert_test(om, watchdog, snapshot, args):
    """Experiment for the GUI design: can a client that stays connected
    revert its own change? (docs/hyprland-persistence.md, "GUI + watchdog
    conflict".)

    The change is live and the watchdog is armed. We keep `om` (the
    connection that applied the change) open, wait, then revert either:
      via="same":   through `om` itself
      via="helper": through a second connection in this process, while
                    `om` stays open
    and verify on a fresh connection. If the revert didn't take, we close
    `om` and let the watchdog revert, as the backup.
    """
    via = args.self_revert_via
    print(f"--self-revert-test: holding {args.self_revert_test}s, "
          f"then reverting via the {via} connection (applying connection stays open)")
    time.sleep(args.self_revert_test)

    desired = desired_from_snapshot(snapshot)
    if via == "same":
        # Catch up on events first, so om has the latest serial; a stale
        # serial would get the configuration cancelled.
        om.roundtrip()
        result = om.run_configuration(desired, "apply")
    else:
        with OutputManager() as helper:
            result = helper.run_configuration(desired, "apply")
    print(f"self-revert apply ({via} connection): {result}")

    problems, _ = verify_state(desired)
    if not problems:
        print(f"RESULT: self-revert via the {via} connection WORKED (verified).")
        print("Restored by: displayctl (self-revert). Telling the watchdog to stand down.")
        tell(watchdog, "keep")  # display is already restored; nothing left to undo
        watchdog.wait(timeout=10)
        return

    for p in problems:
        print(f"  verify mismatch: {p}")
    print(f"RESULT: self-revert via the {via} connection did NOT take.")
    print("Closing the applying connection and handing over to the watchdog...")
    om.close()
    tell(watchdog, "revert")
    wait_for_revert(watchdog)  # exits with an error if the watchdog fails too
    print("Restored by: watchdog (backup), after the applying connection closed.")


# --- Command line ----------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Wayland display settings (wlr-output-management).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show outputs and their modes (read-only)")

    p = sub.add_parser("set", help="change one output, with test + 15s auto-revert")
    p.add_argument("output", help="output name, e.g. DP-3 or eDP-1")
    p.add_argument("--mode", type=parse_mode, help="WxH or WxH@Hz, e.g. 2560x1440@144")
    p.add_argument("--pos", type=parse_pos, help="X,Y in layout coordinates, e.g. 2560,0")
    p.add_argument("--scale", type=parse_scale, help="e.g. 1, 1.25, 1.5, 2")
    p.add_argument("--transform", choices=list(TRANSFORM_BY_NAME), help="rotation / flip")
    onoff = p.add_mutually_exclusive_group()
    onoff.add_argument("--enable", action="store_true", help="turn the output on")
    onoff.add_argument("--disable", action="store_true", help="turn the output off")
    p.add_argument("--dry-run", action="store_true",
                   help="snapshot and test() only; never apply")
    # Hidden (help=SUPPRESS): for reproducing delayed reverts without a terminal.
    p.add_argument("--auto-revert-after", type=float, metavar="SECONDS",
                   help=argparse.SUPPRESS)
    p.add_argument("--auto-keep", action="store_true", help=argparse.SUPPRESS)
    # Hidden: experiment for the GUI revert design (see self_revert_test).
    p.add_argument("--self-revert-test", type=float, metavar="SECONDS",
                   help=argparse.SUPPRESS)
    p.add_argument("--self-revert-via", choices=["same", "helper"], default="same",
                   help=argparse.SUPPRESS)

    args = parser.parse_args()
    try:
        if args.command == "list":
            cmd_list(args)
        else:
            cmd_set(args)
    except WaylandError as e:
        die(str(e))


if __name__ == "__main__":
    main()
