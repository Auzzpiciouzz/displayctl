"""Editing logic for the GUI, kept free of GTK and Wayland so it can be tested.

Vocabulary
----------
state     a dict like backend.Head.to_state(): enabled, mode, position,
          transform, scale, adaptive_sync (+ identity fields)
live      the state the compositor reports right now (head.to_state())
pending   {head name: state} for heads the user has edited but not applied
effective what the layout WOULD be: live state with pending edits on top.
          The canvas draws this, and Apply sends exactly this.

Every function returns new dicts; nothing is modified in place, so a
"pending" state never accidentally changes the live one.
"""

import copy

SCALES = [1.0, 1.25, 1.5, 2.0]
# The rotations offered in the GUI (wl_output.transform values). Flipped
# variants exist too; they're only shown if a monitor is already using one.
ROTATIONS = [0, 1, 2, 3]


def logical_size(state):
    """Size in layout coordinates: pixels / scale, swapped if rotated."""
    if not state["enabled"] or state["mode"] is None:
        return (0, 0)
    w = state["mode"]["width"] / state["scale"]
    h = state["mode"]["height"] / state["scale"]
    if state["transform"] % 2 == 1:  # 90 / 270 (and flipped variants)
        w, h = h, w
    return (round(w), round(h))


def effective(heads, pending):
    """{name: state} for every head: pending edit if any, else live state."""
    return {
        h.name: copy.deepcopy(pending[h.name]) if h.name in pending else h.to_state()
        for h in heads
    }


EDITABLE = ("enabled", "mode", "position", "transform", "scale")


def differs(head, state):
    """True if `state` is different from what the head has now.

    Exact comparison on purpose: backend.state_mismatches allows 1 Hz of
    tolerance (fine for verifying 143.999 vs 144), but here 60.000 -> 59.940
    Hz is a real edit the user chose from the list.
    """
    live = head.to_state()
    if state["enabled"] != live["enabled"]:
        return True
    if not state["enabled"]:
        return False  # both off: nothing else matters
    return any(state[k] != live[k] for k in EDITABLE if k != "scale") or abs(
        state["scale"] - live["scale"]) > 1e-6


def set_pending(pending, head, state):
    """Return a new pending dict with `state` for `head`.

    If the edit brings the head back to its live state, it's no longer
    pending, so it's dropped (Reset-by-hand works naturally).
    """
    new = dict(pending)
    if differs(head, state):
        new[head.name] = state
    else:
        new.pop(head.name, None)
    return new


def prune(pending, heads):
    """Drop pending edits for heads that no longer exist (unplugged)."""
    names = {h.name for h in heads}
    return {name: s for name, s in pending.items() if name in names}


def enabled_count(states):
    return sum(1 for s in states.values() if s["enabled"])


# --- choices for the dropdowns ----------------------------------------------------


def resolutions(head):
    """Unique (width, height) pairs the head supports, largest first."""
    sizes = {(m.width, m.height) for m in head.modes}
    return sorted(sizes, key=lambda wh: (wh[0] * wh[1], wh[0]), reverse=True)


def refresh_rates(head, width, height):
    """Refresh rates (mHz) available at width x height, highest first."""
    return sorted({m.refresh_mhz for m in head.modes if (m.width, m.height) == (width, height)},
                  reverse=True)


def rotations_for(state):
    """ROTATIONS, plus the current transform if it's a flipped one."""
    if state["transform"] in ROTATIONS:
        return list(ROTATIONS)
    return list(ROTATIONS) + [state["transform"]]


def scales_for(state):
    """SCALES, plus the current scale if it's something else (e.g. 1.6)."""
    if any(abs(state["scale"] - s) < 1e-6 for s in SCALES):
        return list(SCALES)
    return sorted(SCALES + [state["scale"]])


# --- edits ------------------------------------------------------------------------


def with_resolution(state, head, width, height):
    """Switch to width x height, keeping the refresh rate as close as possible."""
    rates = refresh_rates(head, width, height)
    if not rates:
        raise ValueError(f"{head.name} has no {width}x{height} mode")
    current = state["mode"]["refresh_mhz"] if state["mode"] else None
    rate = rates[0] if current is None else min(rates, key=lambda r: (abs(r - current), -r))
    new = copy.deepcopy(state)
    new["mode"] = {"width": width, "height": height, "refresh_mhz": rate}
    return new


def with_refresh(state, head, refresh_mhz):
    m = state["mode"]
    if refresh_mhz not in refresh_rates(head, m["width"], m["height"]):
        raise ValueError(f"{head.name} has no {m['width']}x{m['height']} mode at that rate")
    new = copy.deepcopy(state)
    new["mode"]["refresh_mhz"] = refresh_mhz
    return new


def with_scale(state, scale):
    if scale <= 0:
        raise ValueError("scale must be greater than 0")
    new = copy.deepcopy(state)
    new["scale"] = scale
    return new


def with_transform(state, transform):
    new = copy.deepcopy(state)
    new["transform"] = transform
    return new


class AllOutputsOff(ValueError):
    """Refused: the change would leave no display on."""


def with_enabled(states, head, on):
    """New state for `head` when "Use this display" is switched.

    states: the current effective states of ALL heads (to place a newly
    enabled one and to refuse turning the last one off).

    Turning on: preferred mode, scale 1, normal rotation, placed to the
    right of everything that's on (position editing comes in step 4).
    Turning off: refused with AllOutputsOff if it's the only one on.
    """
    state = copy.deepcopy(states[head.name])
    if on == state["enabled"]:
        return state
    if not on:
        others_on = [n for n, s in states.items() if s["enabled"] and n != head.name]
        if not others_on:
            raise AllOutputsOff("At least one display has to stay on.")
        state["enabled"] = False
        state["mode"] = None
        return state

    mode = head.preferred_mode()
    if mode is None:
        raise ValueError(f"{head.name} reports no modes")
    right_edge = max(
        (s["position"][0] + logical_size(s)[0] for n, s in states.items()
         if s["enabled"] and n != head.name),
        default=0,
    )
    state.update(
        enabled=True,
        mode={"width": mode.width, "height": mode.height, "refresh_mhz": mode.refresh_mhz},
        position=[right_edge, 0],
        scale=1.0,
        transform=0,
    )
    return state


def validate(states):
    """Last check on the complete layout before Apply. Returns problems ([] = ok).

    The edit functions already refuse bad values; this catches anything
    that slipped through (e.g. a live state changed underneath us).
    """
    problems = []
    if enabled_count(states) == 0:
        problems.append("At least one display has to stay on.")
    for name, s in states.items():
        if s["enabled"] and s["mode"] is None:
            problems.append(f"{name} is on but has no resolution.")
        if s["enabled"] and s["scale"] <= 0:
            problems.append(f"{name} has an invalid scale.")
    return problems
