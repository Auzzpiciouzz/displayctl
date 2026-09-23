"""Wayland backend shared by displayctl.py and watchdog.py.

It does the same reading as list_outputs.py, plus the "write" half of
wlr-output-management-unstable-v1:

  manager.create_configuration(serial) -> configuration
      configuration.enable_head(head)  -> configuration_head (set_mode, set_position, ...)
      configuration.disable_head(head)
      configuration.test() or .apply()
  <- configuration.succeeded / failed / cancelled   (events, as always)

Protocol rules we have to respect (from the XML):
  - every head must be either enabled or disabled in a configuration
  - a configuration can be used once (test OR apply), then destroyed
  - each property may be set at most once per head configuration
  - properties we don't set stay as they are
"""

import json
import os
import time
from pathlib import Path

from pywayland.client import Display

from protocols.wlr_output_management_unstable_v1 import ZwlrOutputManagerV1

# wl_output.transform enum values <-> readable names.
TRANSFORMS = {
    0: "normal", 1: "90", 2: "180", 3: "270",
    4: "flipped", 5: "flipped-90", 6: "flipped-180", 7: "flipped-270",
}
TRANSFORM_BY_NAME = {name: value for value, name in TRANSFORMS.items()}

# Where snapshots, the lock file and the watchdog log live.
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "displayctl"
LAST_GOOD = CACHE_DIR / "last-good.json"


class WaylandError(RuntimeError):
    """The connection broke, or the compositor sent a protocol error."""


# --- Data model --------------------------------------------------------------


class Mode:
    """One zwlr_output_mode_v1, plus the proxy needed to refer to it later."""

    def __init__(self, proxy):
        self.proxy = proxy  # passed to set_mode() when we choose this mode
        self.width = 0
        self.height = 0
        self.refresh_mhz = 0  # milli-Hertz, as the protocol sends it
        self.preferred = False

    def __str__(self):
        pref = "  (preferred)" if self.preferred else ""
        return f"{self.width}x{self.height} @ {self.refresh_mhz / 1000:.3f} Hz{pref}"


class Head:
    """One zwlr_output_head_v1 (a monitor), plus its proxy."""

    def __init__(self, proxy):
        self.proxy = proxy  # passed to enable_head()/disable_head()
        self.name = ""
        self.description = ""
        self.make = ""
        self.model = ""
        self.serial = ""
        self.physical_size = (0, 0)
        self.enabled = False
        self.position = (0, 0)
        self.transform = 0
        self.scale = 1.0
        self.adaptive_sync = 0
        self.modes = []
        self.current_mode = None

    def find_mode(self, width, height, refresh_mhz=None):
        """Return the Mode matching WxH and the closest refresh rate.

        With refresh_mhz=None, the highest refresh rate for that size is
        chosen. Returns None when nothing matches within 1 Hz.
        """
        same_size = [m for m in self.modes if (m.width, m.height) == (width, height)]
        if not same_size:
            return None
        if refresh_mhz is None:
            return max(same_size, key=lambda m: m.refresh_mhz)
        best = min(same_size, key=lambda m: abs(m.refresh_mhz - refresh_mhz))
        # Rates like 143.999 vs 144 should match; 120 vs 144 should not.
        return best if abs(best.refresh_mhz - refresh_mhz) <= 1000 else None

    def preferred_mode(self):
        for mode in self.modes:
            if mode.preferred:
                return mode
        return self.modes[0] if self.modes else None

    def logical_size(self):
        """Size in layout coordinates: pixels / scale, swapped if rotated."""
        if not self.enabled or self.current_mode is None:
            return (0, 0)
        w = self.current_mode.width / self.scale
        h = self.current_mode.height / self.scale
        if self.transform % 2 == 1:  # 90 / 270 (and flipped variants)
            w, h = h, w
        return (round(w), round(h))

    def to_state(self):
        """This head's settings as a plain dict: what snapshots store."""
        mode = self.current_mode
        return {
            "name": self.name,
            "make": self.make,
            "model": self.model,
            "serial": self.serial,
            "enabled": self.enabled,
            "mode": None if mode is None else {
                "width": mode.width,
                "height": mode.height,
                "refresh_mhz": mode.refresh_mhz,
            },
            "position": list(self.position),
            "transform": self.transform,
            "scale": self.scale,
            "adaptive_sync": self.adaptive_sync,
        }


# --- Connection ----------------------------------------------------------------


class OutputManager:
    """A live connection to the compositor's output manager.

    Creating one connects, binds zwlr_output_manager_v1 and waits for the
    first 'done', so self.heads is complete when __init__ returns.
    """

    def __init__(self):
        self.display = Display()
        self.display.connect()
        self.manager = None
        self.manager_version = 0  # the version we bound (pywayland doesn't track it)
        self.heads = []
        self.serial = None  # from the latest 'done'; needed to create configurations

        registry = self.display.get_registry()
        registry.dispatcher["global"] = self._on_global
        self.roundtrip()
        if self.manager is None:
            self.close()
            raise WaylandError("Compositor does not support zwlr_output_manager_v1")
        while self.serial is None:
            self.roundtrip()

    def close(self):
        """Disconnect, and make pywayland forget this connection's objects.

        pywayland keeps a class-level map of C pointer -> Python proxy for
        every interface ("registry", not the Wayland registry), and
        disconnect() doesn't remove entries from it. With a second connection
        in the same process, new objects can land on the same C addresses and
        collide with those stale entries, which crashed the interpreter at
        exit (segfault during garbage collection).
        """
        if self.display._ptr is None:
            return  # already closed
        # Every proxy created on this connection, collected before
        # disconnect() destroys them.
        ours = {id(obj): obj for obj in list(self.display._children)}
        self.display.disconnect()
        for interface in {obj.interface for obj in ours.values()}:
            stale = [key for key, obj in interface.registry.items() if id(obj) in ours]
            for key in stale:
                del interface.registry[key]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def roundtrip(self):
        # libwayland returns -1 instead of raising when the connection dies or
        # the compositor sends a protocol error. Turn that into an exception,
        # otherwise a "loop until X" could spin forever.
        if self.display.roundtrip() == -1:
            raise WaylandError("Wayland connection failed (protocol error?)")

    def _adopt(self, proxy):
        """Make a proxy the compositor just created belong to OUR connection.

        pywayland bug: for objects created by an event (every head and mode),
        it picks "the first open connection it knows" instead of the
        connection the event arrived on. With two connections open in one
        process, the second one's heads/modes get filed under the first.
        Closing the second frees them; closing the first then destroys them
        again -> segfault. Re-filing each new proxy here prevents that.
        """
        if proxy._display is not self.display:
            proxy._display._children.discard(proxy)
            proxy._display = self.display
            self.display._children.add(proxy)

    def head(self, name):
        for head in self.heads:
            if head.name == name:
                return head
        return None

    # --- event handlers (same pattern as list_outputs.py) ---

    def _on_global(self, registry, name, interface, version):
        if interface == ZwlrOutputManagerV1.name:
            self.manager_version = min(version, ZwlrOutputManagerV1.version)
            self.manager = registry.bind(name, ZwlrOutputManagerV1, self.manager_version)
            self.manager.dispatcher["head"] = self._on_head
            self.manager.dispatcher["done"] = self._on_done

    def _on_done(self, manager, serial):
        self.serial = serial

    def _on_head(self, manager, proxy):
        self._adopt(proxy)
        head = Head(proxy)
        self.heads.append(head)

        def on_size(_, w, h):
            head.physical_size = (w, h)

        def on_position(_, x, y):
            head.position = (x, y)

        def on_current_mode(_, mode_proxy):
            head.current_mode = next((m for m in head.modes if m.proxy is mode_proxy), None)

        def on_enabled(_, enabled):
            head.enabled = bool(enabled)
            if not enabled:
                head.current_mode = None

        def on_finished(_):
            # The monitor was unplugged: forget it.
            if head in self.heads:
                self.heads.remove(head)

        d = proxy.dispatcher
        d["name"] = lambda _, v: setattr(head, "name", v)
        d["description"] = lambda _, v: setattr(head, "description", v)
        d["make"] = lambda _, v: setattr(head, "make", v)
        d["model"] = lambda _, v: setattr(head, "model", v)
        d["serial_number"] = lambda _, v: setattr(head, "serial", v)
        d["physical_size"] = on_size
        d["enabled"] = on_enabled
        d["position"] = on_position
        d["transform"] = lambda _, v: setattr(head, "transform", v)
        d["scale"] = lambda _, v: setattr(head, "scale", v)
        d["adaptive_sync"] = lambda _, v: setattr(head, "adaptive_sync", v)
        d["mode"] = lambda _, mode_proxy: self._on_mode(head, mode_proxy)
        d["current_mode"] = on_current_mode
        d["finished"] = on_finished

    def _on_mode(self, head, proxy):
        self._adopt(proxy)
        mode = Mode(proxy)
        head.modes.append(mode)

        def on_size(_, w, h):
            mode.width = w
            mode.height = h

        def on_finished(_):
            if mode in head.modes:
                head.modes.remove(mode)

        d = proxy.dispatcher
        d["size"] = on_size
        d["refresh"] = lambda _, r: setattr(mode, "refresh_mhz", r)
        d["preferred"] = lambda _: setattr(mode, "preferred", True)
        d["finished"] = on_finished

    # --- snapshots ---

    def snapshot(self):
        """Current state of every head, as JSON-ready data."""
        return {"version": 1, "heads": [h.to_state() for h in self.heads]}

    # --- configuring ---

    def run_configuration(self, desired, action):
        """Send a configuration built from `desired`, then test() or apply() it.

        desired: {head name: state dict like Head.to_state()}. Heads missing
        from `desired` (e.g. plugged in after a snapshot) keep their current
        state.
        action: "test" or "apply".

        Returns "succeeded", "failed" or "cancelled".
        """
        config = self.manager.create_configuration(self.serial)
        result = []

        # The answer comes back as one of three events, as usual.
        config.dispatcher["succeeded"] = lambda _: result.append("succeeded")
        config.dispatcher["failed"] = lambda _: result.append("failed")
        config.dispatcher["cancelled"] = lambda _: result.append("cancelled")

        for head in self.heads:
            self._configure_head(config, head, desired.get(head.name))

        if action == "test":
            config.test()
        else:
            config.apply()

        while not result:
            self.roundtrip()
        config.destroy()

        # After a successful apply the compositor sends the new head state
        # followed by 'done'. One more roundtrip brings self.heads up to date.
        self.roundtrip()
        return result[0]

    def _configure_head(self, config, head, state):
        """Add one head to `config`, with EVERY property set explicitly.

        The protocol says unset properties stay as they are, but Hyprland
        doesn't follow that: on a commit it re-applies its config rules to
        any head whose properties we left out. (Tested: enabling eDP-1 while
        sending DP-3 as "enabled, unchanged" reset DP-3 from 144 Hz at 0,0 to
        200 Hz at 4480,0.) So we never send a diff; we always describe each
        enabled head completely.
        """
        if state is None:
            # Not mentioned: keep the head as it is, by restating its
            # current settings in full.
            state = head.to_state()

        if not state["enabled"]:
            config.disable_head(head.proxy)
            return

        # enable_head() returns a new configuration_head object; its set_*
        # requests describe how this head should look.
        ch = config.enable_head(head.proxy)

        want = state["mode"]
        if want is not None:
            mode = head.find_mode(want["width"], want["height"], want["refresh_mhz"])
            if mode is not None:
                ch.set_mode(mode.proxy)
            else:
                # Not in the head's list (e.g. restoring an old snapshot):
                # ask for it by numbers instead.
                ch.set_custom_mode(want["width"], want["height"], want["refresh_mhz"])

        x, y = state["position"]
        ch.set_position(x, y)
        ch.set_transform(state["transform"])
        ch.set_scale(state["scale"])
        # set_adaptive_sync only exists from protocol version 4.
        if self.manager_version >= 4 and "adaptive_sync" in state:
            ch.set_adaptive_sync(state["adaptive_sync"])


def state_mismatches(want, have):
    """Compare two head state dicts. Returns a list of differences ([] = match).

    Refresh rates may differ by up to 1 Hz (e.g. 143999 vs 144000 mHz).
    """
    problems = []
    if want["enabled"] != have["enabled"]:
        problems.append(f"enabled {have['enabled']} (want {want['enabled']})")
        return problems  # nothing else is comparable
    if not want["enabled"]:
        return problems
    wm, hm = want["mode"], have["mode"]
    if wm and hm:
        if (wm["width"], wm["height"]) != (hm["width"], hm["height"]) or abs(
            wm["refresh_mhz"] - hm["refresh_mhz"]
        ) > 1000:
            problems.append(
                f"mode {hm['width']}x{hm['height']}@{hm['refresh_mhz']} "
                f"(want {wm['width']}x{wm['height']}@{wm['refresh_mhz']})"
            )
    if list(want["position"]) != list(have["position"]):
        problems.append(f"position {have['position']} (want {want['position']})")
    if want["transform"] != have["transform"]:
        problems.append(f"transform {have['transform']} (want {want['transform']})")
    if abs(want["scale"] - have["scale"]) > 1e-6:
        problems.append(f"scale {have['scale']} (want {want['scale']})")
    return problems


def verify_state(desired, settle=1.0):
    """Re-read the outputs on a NEW connection and compare with `desired`.

    Returns (problems, missing): problems is a list of differences ([] means
    the outputs match), missing lists heads in `desired` that aren't
    connected any more (skipped, not counted as problems).

    "succeeded" only means the compositor accepted a request; this checks
    what it actually did. `settle` gives a mode switch a moment to finish.
    """
    time.sleep(settle)
    problems, missing = [], []
    with OutputManager() as om:
        for name, want in desired.items():
            head = om.head(name)
            if head is None:
                missing.append(name)
                continue
            for p in state_mismatches(want, head.to_state()):
                problems.append(f"{name}: {p}")
    return problems, missing


def hyprland_disagreements(heads):
    """On Hyprland, compare the protocol's view of each head with hyprctl's.

    Returns None when not running under Hyprland, otherwise a list of
    human-readable disagreements ([] = both views agree).

    Why: after a boot, Hyprland once reported eDP-1 as enabled through
    wlr-output-management while hyprctl (and the screen) said disabled.
    Since every configuration restates all enabled heads, applying from
    that wrong view switched eDP-1 on. We can't know which view is right,
    so the caller refuses to apply instead of guessing.
    """
    if "HYPRLAND_INSTANCE_SIGNATURE" not in os.environ:
        return None
    import subprocess  # only needed on Hyprland
    try:
        out = subprocess.run(
            ["hyprctl", "monitors", "all", "-j"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        hypr = {m["name"]: m for m in json.loads(out)}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return [f"could not read hyprctl monitors: {e}"]

    problems = []
    for head in heads:
        m = hypr.get(head.name)
        if m is None:
            problems.append(f"{head.name}: in the protocol but not in hyprctl")
            continue
        hypr_enabled = not m["disabled"]
        if head.enabled != hypr_enabled:
            problems.append(
                f"{head.name}: protocol says enabled={head.enabled}, "
                f"hyprctl says enabled={hypr_enabled}"
            )
            continue
        if not head.enabled:
            continue  # hyprctl keeps showing a stale mode for disabled outputs
        mode = head.current_mode
        if mode is None:
            problems.append(f"{head.name}: protocol reports no current mode")
            continue
        if (mode.width, mode.height) != (m["width"], m["height"]) or abs(
            mode.refresh_mhz / 1000 - m["refreshRate"]
        ) > 1:
            problems.append(
                f"{head.name}: protocol mode {mode.width}x{mode.height}@"
                f"{mode.refresh_mhz / 1000:.2f}, hyprctl {m['width']}x{m['height']}@"
                f"{m['refreshRate']:.2f}"
            )
        if tuple(head.position) != (m["x"], m["y"]):
            problems.append(
                f"{head.name}: protocol position {head.position[0]},{head.position[1]}, "
                f"hyprctl {m['x']},{m['y']}"
            )
    for name in hypr.keys() - {h.name for h in heads}:
        problems.append(f"{name}: in hyprctl but not in the protocol")
    return problems


def desired_from_snapshot(snapshot):
    """Turn snapshot data into the {name: state} dict run_configuration wants."""
    return {h["name"]: h for h in snapshot["heads"]}


def save_json(path, data):
    """Write JSON atomically: a crash mid-write never leaves a broken file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)  # rename is atomic on the same filesystem
