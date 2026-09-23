#!/usr/bin/env python3
"""displayctl GUI, Phase 3 step 3: edit display settings and apply them safely.

A canvas draws every monitor to scale (disabled ones dimmed, off to the
right). Select one to edit its settings in the side panel: "Use this
display", resolution, refresh rate, scale and rotation. Edits stay PENDING:
the canvas previews them, and nothing touches the real displays until you
press Apply. Reset throws the pending edits away. (Moving monitors comes in
step 4.)

How Wayland and GTK share one thread
------------------------------------
GTK runs a GLib main loop: it sleeps until something happens (mouse, timer,
a file descriptor becoming readable) and then calls the matching callback.
Our Wayland connection is just a socket (a file descriptor). So instead of a
second thread, we ask GLib to watch that fd and call us when the compositor
has sent something. We then read and dispatch the events, which runs the
same handlers as in the CLI (backend.OutputManager), and redraw.

What Apply does (see Applier)
-----------------------------
  hyprctl cross-check -> snapshot -> arm watchdog -> apply the FULL layout
  through our own connection -> verify -> "Keep these display settings?"
  (15 s; Esc and the timeout both revert).
  Keep:   the watchdog stands down.
  Revert: we revert through our OWN connection (on Hyprland only the client
          that applied a change can revert it while it's connected), then
          verify. The watchdog is only the fallback if the GUI crashes.
"""

import ctypes
import logging
import os
import select
import signal
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gio, GLib, GLibUnix, Gtk, Pango, PangoCairo  # noqa: E402

# Imported as modules (backend.x, safety.x) rather than names, so the test
# harness can swap in fakes and exercise Apply without touching the displays.
import backend  # noqa: E402
import durable  # noqa: E402
import layout  # noqa: E402
import safety  # noqa: E402
from backend import OutputManager, WaylandError  # noqa: E402

APP_ID = "dev.displayctl.App"
PADDING = 56  # empty space (px) around the layout on the canvas
CONFIRM_SECONDS = 15  # the "Keep these display settings?" countdown
# The watchdog must not act while the GUI is still deciding or reverting:
# its deadline is CONFIRM_SECONDS + this (+ its own grace period).
WATCHDOG_EXTRA_SECONDS = 15
# After a revert verifies OK, check again this much later: turning outputs
# on/off can make Hyprland re-apply its config rules a moment afterwards.
REVERIFY_SECONDS = 2
# After a pushed update, wait this long after the LAST push (bursts come in
# several wakeups) before re-reading the state on a fresh connection.
RESYNC_DELAY_MS = 300

ROTATION_NAMES = {0: "Normal", 1: "90°", 2: "180°", 3: "270°", 4: "Flipped",
                  5: "Flipped, 90°", 6: "Flipped, 180°", 7: "Flipped, 270°"}

# `gui.py --debug` logs fd wakeups, refreshes, redraws and the apply steps.
# (Add WAYLAND_DEBUG=1 for libwayland's per-message trace as well.)
log = logging.getLogger("displayctl.gui")


def format_hz(refresh_mhz, decimals=3):
    """144000 -> '144', 143999 -> '143.999' (the protocol sends milli-Hertz)."""
    return f"{refresh_mhz / 1000:.{decimals}f}".rstrip("0").rstrip(".")


def describe_heads(heads):
    """Compact one-line state summary for the debug log."""
    parts = []
    for h in heads:
        m = h.current_mode
        mode = f"{m.width}x{m.height}@{m.refresh_mhz}" if (h.enabled and m) else "-"
        parts.append(f"{h.name}:{'on' if h.enabled else 'off'} {mode} "
                     f"pos {h.position[0]},{h.position[1]}")
    return " | ".join(parts)


# --- Wayland <-> GLib glue -------------------------------------------------------


class WaylandSource:
    """Feeds Wayland events into the GLib main loop.

    GLib calls _on_fd whenever the connection's fd is readable. The steps
    there are libwayland's standard non-blocking pattern:
      read()      prepare_read + read_events: move bytes from the socket
                  into libwayland's event queue
      dispatch()  dispatch_pending: run our Python handlers for queued events
      flush()     send any requests we queued in those handlers
    """

    def __init__(self, om, on_error, on_changed):
        self.om = om
        self.on_error = on_error
        self.on_changed = on_changed
        # Events can already be queued from OutputManager's startup
        # roundtrips; handle them before we start sleeping on the fd.
        om.display.dispatch()
        om.display.flush()
        self.source_id = GLibUnix.fd_add_full(
            GLib.PRIORITY_DEFAULT,
            om.display.get_fd(),
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            self._on_fd,
        )

    def _on_fd(self, fd, condition):
        log.debug("fd %d readable (condition %s)", fd, condition)
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            self.on_error("The compositor closed the connection.")
            self.source_id = None
            return GLib.SOURCE_REMOVE  # stop watching a dead fd
        dones_before = self.om.done_count
        try:
            # Apply runs roundtrips synchronously on this same connection, and
            # they can consume the data GLib saw as "readable" before we get
            # here. read() would then block the whole UI waiting for more, so
            # check again, without waiting (timeout 0), that data is there.
            if select.select([fd], [], [], 0)[0]:
                self.om.display.read()
            self.om.display.dispatch()
            self.om.display.flush()
        except RuntimeError as e:
            self.on_error(f"Wayland error: {e}")
            self.source_id = None
            return GLib.SOURCE_REMOVE
        if self.om.done_count == dones_before:
            # The protocol says 'done' follows every batch of changes;
            # Hyprland 0.56.2 only sends it to the client that applied.
            log.debug("pushed update without a 'done' event")
        # Treat the push only as "something changed": on Hyprland the pushed
        # head state can be one change behind. The window re-reads the real
        # state on a fresh connection (see MainWindow.schedule_resync).
        self.on_changed()
        return GLib.SOURCE_CONTINUE  # keep watching

    def stop(self):
        if self.source_id is not None:
            GLib.source_remove(self.source_id)
            self.source_id = None


# --- The canvas ---------------------------------------------------------------------


class LayoutCanvas(Gtk.DrawingArea):
    """Draws the EFFECTIVE layout (live state + pending edits); click to
    select, drag an enabled monitor to move it.

    Enabled monitors sit at their position. Disabled ones have no position
    in the layout, so they're drawn dimmed with a dashed border in a row to
    the right, at their preferred mode's size. Monitors with pending edits
    get a "Not applied" line.

    Dragging (docs/drag-to-arrange.md): the logical<->screen mapping is
    frozen at drag start (otherwise the view would rescale under the
    pointer); every motion asks layout.snap_position for the nearest legal
    spot and draws the monitor there; on release the window turns it into a
    pending edit.
    """

    # Snap to edge/centre alignment within this many SCREEN pixels.
    SNAP_SCREEN_PX = 10

    def __init__(self, window):
        super().__init__(hexpand=True, vexpand=True)
        self.window = window  # gives us heads, effective states, pending
        self.selected = None  # name of the selected head, or None
        self._hit_boxes = []  # (name, x, y, w, h) in widget pixels, from the last draw
        self._enabled_names = set()  # which hit boxes may be dragged
        self._view = None  # (factor, offset_x, offset_y, left, top) of the last draw
        self._drag = None  # while dragging: name, start position, frozen view, preview

        self.set_draw_func(self._draw)

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_click)
        self.add_controller(click)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        # Redraw when the user switches light/dark or accent colour.
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *_: self.queue_draw())
        style.connect("notify::accent-color-rgba", lambda *_: self.queue_draw())

    def _layout_boxes(self):
        """(head, state, x, y, w, h) for every head, in compositor units."""
        states = self.window.effective()
        if self._drag and self._drag.get("preview"):
            # Show the dragged monitor at its snapped preview position.
            states[self._drag["name"]]["position"] = list(self._drag["preview"])
        boxes = []
        for head in self.window.om.heads:
            s = states[head.name]
            if s["enabled"] and s["mode"]:
                w, h = layout.logical_size(s)
                boxes.append((head, s, s["position"][0], s["position"][1], w, h))

        # Disabled heads: a row starting one "gap" to the right of the layout.
        # While dragging, the row stays where it was at drag start (it's
        # placed relative to the layout, which the drag is changing).
        if self._drag and "disabled_row" in self._drag:
            row = self._drag["disabled_row"]
        else:
            right = max((b[2] + b[4] for b in boxes), default=0)
            top = min((b[3] for b in boxes), default=0)
            gap = max(right // 12, 200)  # spacing, scaled to the layout
            x = right + gap if boxes else 0
            row = {}
            for head in self.window.om.heads:
                s = states[head.name]
                if s["enabled"] and s["mode"]:
                    continue
                mode = head.preferred_mode()
                if mode is None:
                    continue  # nothing to size it by
                row[head.name] = (x, top, mode.width, mode.height)
                x += mode.width + gap
        for head in self.window.om.heads:
            if head.name in row:
                boxes.append((head, states[head.name], *row[head.name]))
        return boxes

    def _draw(self, area, cr, width, height):
        boxes = self._layout_boxes()
        if self._drag:
            # A drag redraws every frame; count instead of logging each one.
            self._drag["frames"] = self._drag.get("frames", 0) + 1
        else:
            log.debug("redraw %dx%d, pending: %s", width, height, sorted(self.window.pending))
        self._hit_boxes = []
        if not boxes:
            return

        if self._drag and "view" in self._drag:
            # Frozen mapping while dragging.
            factor, offset_x, offset_y, left, top = self._drag["view"]
        else:
            # 1. Bounding box of everything we draw, in compositor coordinates.
            left = min(b[2] for b in boxes)
            top = min(b[3] for b in boxes)
            right = max(b[2] + b[4] for b in boxes)
            bottom = max(b[3] + b[5] for b in boxes)

            # 2. One scale factor for both axes keeps the real proportions;
            #    then centre the layout in the widget, with padding all round.
            factor = min((width - 2 * PADDING) / (right - left),
                         (height - 2 * PADDING) / (bottom - top))
            offset_x = (width - (right - left) * factor) / 2
            offset_y = (height - (bottom - top) * factor) / 2
        self._view = (factor, offset_x, offset_y, left, top)
        self._enabled_names = {b[0].name for b in boxes if b[1]["enabled"] and b[1]["mode"]}

        fg = self.get_color()  # the theme's text colour (light or dark)
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()

        for head, state, x, y, w, h in boxes:
            enabled = state["enabled"]
            sx = offset_x + (x - left) * factor
            sy = offset_y + (y - top) * factor
            # A small gap so touching monitors stay visually separate.
            gap = 3
            rect = (sx + gap, sy + gap, w * factor - 2 * gap, h * factor - 2 * gap)
            self._hit_boxes.append((head.name, *rect))
            selected = head.name == self.selected
            dim = 1.0 if enabled else 0.5  # disabled monitors are drawn faded

            # Fill
            if selected:
                cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.30 * dim)
            else:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.08 * dim)
            cr.rectangle(*rect)
            cr.fill_preserve()  # keep the path to draw the border next

            # Border: dashed for disabled monitors.
            cr.set_dash([] if enabled else [6, 4])
            if selected:
                cr.set_source_rgba(accent.red, accent.green, accent.blue, dim)
                cr.set_line_width(3)
            else:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45 * dim)
                cr.set_line_width(1.5)
            cr.stroke()
            cr.set_dash([])

            self._draw_label(cr, head, state, rect, fg, accent, dim)

    @staticmethod
    def label_lines(state):
        """(detail, hint) under a monitor's name. hint is None or a call to action."""
        if state["enabled"] and state["mode"]:
            m = state["mode"]
            detail = f"{m['width']}×{m['height']} @ {format_hz(m['refresh_mhz'], 2)} Hz"
            if abs(state["scale"] - 1) > 1e-6:
                detail += f" · {state['scale'] * 100:g}%"
            return detail, None
        # Say that the dashed rectangle can be clicked, and what for.
        return "Disabled", "Click to set up"

    def _draw_label(self, cr, head, state, rect, fg, accent, dim):
        """Name (large, bold), mode or "Disabled" (+ "Click to set up"), and
        "Not applied" if pending."""
        detail, hint = self.label_lines(state)
        name = GLib.markup_escape_text(head.name)
        pending = head.name in self.window.pending
        accent_hex = "#{:02x}{:02x}{:02x}".format(
            int(accent.red * 255), int(accent.green * 255), int(accent.blue * 255))
        layout_ = self.create_pango_layout(None)
        layout_.set_alignment(Pango.Alignment.CENTER)
        # Try big labels first; shrink until they fit inside the rectangle.
        for name_size, detail_size in ((180, 125), (140, 110), (100, 90)):
            markup = (f'<span size="{name_size}%" weight="bold">{name}</span>\n'
                      f'<span size="{detail_size}%">{GLib.markup_escape_text(detail)}</span>')
            if hint:
                markup += (f'\n<span size="{detail_size - 15}%" style="italic">'
                           f'{GLib.markup_escape_text(hint)}</span>')
            if pending:
                markup += (f'\n<span size="{detail_size}%" weight="bold" '
                           f'foreground="{accent_hex}">Not applied</span>')
            layout_.set_markup(markup, -1)
            text_w, text_h = layout_.get_pixel_size()
            if text_w <= rect[2] - 12 and text_h <= rect[3] - 12:
                break
        cr.set_source_rgba(fg.red, fg.green, fg.blue, dim)
        cr.move_to(rect[0] + (rect[2] - text_w) / 2, rect[1] + (rect[3] - text_h) / 2)
        PangoCairo.show_layout(cr, layout_)

    # --- dragging ---

    def _hit(self, x, y):
        for name, bx, by, bw, bh in self._hit_boxes:
            if bx <= x <= bx + bw and by <= y <= by + bh:
                return name
        return None

    # A press becomes a "drag" once the pointer moves this far (screen px);
    # below that it's just a click that selects.
    DRAG_START_PX = 6

    def _on_drag_begin(self, gesture, x, y):
        name = self._hit(x, y)
        if name not in self._enabled_names or self._view is None or self.window.applier.busy:
            gesture.set_state(Gtk.EventSequenceState.DENIED)  # nothing to drag
            return
        if len(self._enabled_names) < 2:
            # Nothing to arrange. Don't refuse the press yet (it may be a
            # click that selects); hint only if the pointer actually moves.
            self._drag = {"name": name, "single": True, "hinted": False}
            return
        start = self.window.effective()[name]["position"]
        self._drag = {"name": name, "start": tuple(start), "view": self._view,
                      "preview": None, "frames": 0,
                      "disabled_row": self._disabled_row()}
        log.debug("drag begin %s at %s", name, start)

    def _disabled_row(self):
        """{name: (x, y, w, h)} of the disabled row as drawn right now."""
        states = self.window.effective()
        return {b[0].name: b[2:] for b in self._layout_boxes()
                if not (states[b[0].name]["enabled"] and states[b[0].name]["mode"])}

    def _on_drag_update(self, gesture, dx, dy):
        if not self._drag:
            return
        if self._drag.get("single"):
            if not self._drag["hinted"] and max(abs(dx), abs(dy)) >= self.DRAG_START_PX:
                self._drag["hinted"] = True
                self.window.arrange_hint()
            return
        factor = self._drag["view"][0]
        sx, sy = self._drag["start"]
        raw = (sx + dx / factor, sy + dy / factor)  # screen px -> logical px
        self._drag["preview"] = layout.snap_position(
            self.window.effective(), self._drag["name"], raw,
            threshold=self.SNAP_SCREEN_PX / factor)
        self.queue_draw()

    def _on_drag_end(self, gesture, dx, dy):
        if not self._drag:
            return
        if self._drag.get("single"):
            self._drag = None
            return
        self._on_drag_update(gesture, dx, dy)
        drag, self._drag = self._drag, None
        log.debug("drag end %s at %s (%d frames drawn)",
                  drag["name"], drag["preview"], drag.get("frames", 0))
        if drag["preview"] and tuple(drag["preview"]) != drag["start"]:
            self.window.on_move(drag["name"], drag["preview"])
        else:
            self.queue_draw()

    def _on_click(self, gesture, n_press, x, y):
        clicked = None
        for name, bx, by, bw, bh in self._hit_boxes:
            if bx <= x <= bx + bw and by <= y <= by + bh:
                clicked = name
                break
        self.selected = clicked  # clicking empty space clears the selection
        self.window.on_select(clicked)


# --- The details panel -----------------------------------------------------------


class DetailsPanel(Gtk.Stack):
    """Side panel: a placeholder, or the selected monitor's settings + identity.

    The settings rows are editable. Changing one calls
    window.on_edit(name, what, value); the window turns that into a pending
    edit and calls show() again. While show() fills the rows, their change
    signals must be ignored (self._filling), or filling a dropdown would
    count as the user picking something.
    """

    ABOUT = [
        ("Connector", "name"), ("Make", "make"), ("Model", "model"),
        ("Serial number", "serial"), ("Physical size", "physical"),
    ]

    def __init__(self, window):
        # hexpand=False explicitly: otherwise the page inside would make the
        # panel grab most of the window's width from the canvas.
        super().__init__(width_request=360, hexpand=False)
        self.window = window
        self._filling = False
        self._name = None  # head currently shown
        self._choices = {}  # dropdown key -> list of values behind its items

        placeholder = Adw.StatusPage(
            icon_name="video-display-symbolic",
            title="No monitor selected",
            description="Click a monitor to change its settings.",
        )
        placeholder.add_css_class("compact")
        self.add_named(placeholder, "empty")

        page = Adw.PreferencesPage()

        # Settings (editable)
        self.settings = Adw.PreferencesGroup()
        self.use_row = Adw.SwitchRow(title="Use this display")
        self.use_row.connect("notify::active", self._on_use)
        self.settings.add(self.use_row)
        self.combos = {}
        for key, title in (("resolution", "Resolution"), ("refresh", "Refresh rate"),
                           ("scale", "Scale"), ("rotation", "Rotation")):
            row = Adw.ComboRow(title=title, model=Gtk.StringList())
            row.connect("notify::selected", self._on_combo, key)
            self.settings.add(row)
            self.combos[key] = row
        self.position_row = Adw.ActionRow(title="Position", subtitle_selectable=True)
        self.position_row.add_css_class("property")
        self.settings.add(self.position_row)
        page.add(self.settings)

        # About this display (fixed identity)
        about = Adw.PreferencesGroup(title="About this display")
        self.about_rows = {}
        for label, key in self.ABOUT:
            # "property" style: small title above a prominent value.
            row = Adw.ActionRow(title=label, subtitle_selectable=True)
            row.add_css_class("property")
            about.add(row)
            self.about_rows[key] = row
        page.add(about)
        self.add_named(page, "details")

    # --- filling the rows from a state ---

    def show(self, head, state, pending):
        if head is None:
            self._name = None
            self.set_visible_child_name("empty")
            return
        self._name = head.name
        self._filling = True
        try:
            self._fill(head, state, pending)
        finally:
            self._filling = False
        self.set_visible_child_name("details")

    def _set_choices(self, key, values, labels, current):
        """Fill a dropdown and select `current` (or nothing if it's absent)."""
        row = self.combos[key]
        # Only replace the list when it actually changed: rebuilding a
        # dropdown's model is what the user would see as flicker.
        if self._choices.get(key) != values:
            row.set_model(Gtk.StringList.new(labels))
            self._choices[key] = values
        row.set_selected(values.index(current) if current in values else Gtk.INVALID_LIST_POSITION)

    def _fill(self, head, state, pending):
        title = GLib.markup_escape_text(head.name)
        self.settings.set_title(title + (" · not applied" if pending else ""))
        on = state["enabled"] and state["mode"] is not None
        self.use_row.set_active(state["enabled"])

        if on:
            m = state["mode"]
            sizes = layout.resolutions(head)
            self._set_choices("resolution", sizes, [f"{w} × {h}" for w, h in sizes],
                              (m["width"], m["height"]))
            rates = layout.refresh_rates(head, m["width"], m["height"])
            self._set_choices("refresh", rates, [f"{format_hz(r)} Hz" for r in rates],
                              m["refresh_mhz"])
            scales = layout.scales_for(state)
            current_scale = min(scales, key=lambda s: abs(s - state["scale"]))
            self._set_choices("scale", scales, [f"{s * 100:g}%" for s in scales], current_scale)
            rots = layout.rotations_for(state)
            self._set_choices("rotation", rots, [ROTATION_NAMES[r] for r in rots],
                              state["transform"])
            self.position_row.set_subtitle(f"{state['position'][0]}, {state['position'][1]}")
        else:
            for key in self.combos:
                self._set_choices(key, [], [], None)
            self.position_row.set_subtitle("—")
        for row in list(self.combos.values()) + [self.position_row]:
            row.set_sensitive(on)

        values = {
            "name": head.name,
            "make": head.make or "—",
            "model": head.model or "—",
            "serial": head.serial or "(none)",
            "physical": f"{head.physical_size[0]} × {head.physical_size[1]} mm",
        }
        for key, text in values.items():
            # Subtitles are Pango markup: escape so "&" etc. show literally.
            self.about_rows[key].set_subtitle(GLib.markup_escape_text(text))

    # --- user edits ---
    #
    # The edit is handled from GLib.idle_add, i.e. just AFTER this signal
    # handler returns. Handling it right here would refill the rows (show())
    # while GTK is still inside this row's own "selected changed" signal;
    # replacing a dropdown's model from inside its own signal hung GTK.

    def _edit_later(self, what, value):
        name = self._name

        def run():
            self.window.on_edit(name, what, value)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(run)

    def _on_use(self, row, _pspec):
        if not self._filling and self._name:
            self._edit_later("enabled", row.get_active())

    def _on_combo(self, row, _pspec, key):
        if self._filling or not self._name:
            return
        index = row.get_selected()
        values = self._choices.get(key, [])
        if 0 <= index < len(values):
            self._edit_later(key, values[index])


# --- Applying -------------------------------------------------------------------------


class Applier:
    """Runs one Apply: checks, snapshot, watchdog, apply, verify, keep/revert.

    The steps that wait (settling before verify, the 15 s countdown) are GLib
    timeouts, so the window stays responsive. `busy` is True from Apply
    until the change is kept or reverted; the window disables editing then.
    """

    def __init__(self, window):
        self.window = window
        self.busy = False
        self.watchdog = None
        self.snapshot = None
        self.desired = None
        self.dialog = None
        self._countdown_id = None
        self._decided = False

    @property
    def om(self):
        return self.window.om

    def start(self):
        states = self.window.effective()

        # Guards: a sane layout, and the protocol agreeing with hyprctl.
        problems = layout.validate(states)
        if problems:
            self.window.alert("Can't apply these settings", "\n".join(problems))
            return
        disagreements = backend.hyprland_disagreements(self.om.heads)
        if disagreements:
            self.window.alert(
                "Refusing to apply",
                "The compositor's two views of your displays disagree, so it's not "
                "safe to guess the current state:\n\n• " + "\n• ".join(disagreements)
                + "\n\nNothing was changed.")
            return
        try:
            lock = safety.take_lock()
        except safety.LockBusy as e:
            self.window.alert("Another change is in progress", f"{e}. Try again shortly.")
            return

        self.busy = True
        self._decided = False
        self.window.update_actions()
        try:
            # Snapshot, then arm the watchdog BEFORE applying.
            self.snapshot = self.om.snapshot()
            backend.save_json(backend.LAST_GOOD, self.snapshot)
            # Our PID: if we hang, the watchdog kills us on its deadline so our
            # connection closes and its revert works (see watchdog.py).
            self.watchdog = safety.start_watchdog(
                lock, CONFIRM_SECONDS + WATCHDOG_EXTRA_SECONDS, kill_pid=os.getpid())
        except OSError as e:
            self._finish()
            self.window.alert("Can't apply", f"Couldn't save the snapshot or start the watchdog: {e}")
            return
        finally:
            lock.close()  # the watchdog holds its own inherited copy of the lock
        self.desired = states
        log.debug("apply: %s", {n: layout.logical_size(s) for n, s in states.items()})

        try:
            result = self.om.run_configuration(states, "apply")
        except WaylandError as e:
            # Our connection is broken, so the watchdog's revert will work.
            safety.tell(self.watchdog, "revert")
            self._finish()
            self.window.alert("Apply failed", f"{e}. The watchdog is restoring the previous settings.")
            return
        log.debug("apply result: %s", result)

        if result == "cancelled":
            safety.tell(self.watchdog, "keep")  # nothing was applied
            self._finish()
            self.window.app.reconnect()  # we still sent a configuration
            self.window.alert("Apply was cancelled",
                              "The displays changed while applying (monitor plugged or "
                              "unplugged?). Nothing was changed. Check the layout and try again.")
            return
        if result == "failed":
            # The compositor should have undone any partial change itself;
            # restore the snapshot anyway, through our own connection.
            self.window.toast("The compositor rejected these settings. Restoring…")
            self._revert()
            return

        safety.tell(self.watchdog, "applied")
        # Give the mode switch a moment, then check what really happened.
        GLib.timeout_add(1000, self._verify_then_ask)

    def _verify_then_ask(self):
        problems, _ = backend.verify_state(self.desired, settle=0)
        log.debug("verify after apply: %s", problems or "ok")
        # Hyprland sends the applying client (us) the state from BEFORE our
        # commit, so our own view is now one change behind. Replace it with
        # a fresh read, then log and show the real live state.
        self.om.sync_from(backend.read_states())
        self.window.refresh()
        self._ask(problems)
        return GLib.SOURCE_REMOVE

    # --- the "Keep these display settings?" dialog ---

    def _ask(self, problems):
        self._remaining = CONFIRM_SECONDS
        self._problems = problems
        dialog = Adw.AlertDialog(heading="Keep these display settings?")
        dialog.add_response("revert", "Revert")
        dialog.add_response("keep", "Keep Changes")
        dialog.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
        # Esc (the "close" response) and Enter both mean Revert: if the screen
        # is unreadable, any blind key press must lead back to safety.
        dialog.set_close_response("revert")
        dialog.set_default_response("revert")
        dialog.connect("response", self._on_response)
        self.dialog = dialog
        self._update_body()
        dialog.present(self.window)
        # timeout_add (milliseconds), not timeout_add_seconds: the latter
        # rounds to whole seconds to save power, so the first tick can come
        # early and "15 s" could end after ~14 s.
        self._countdown_id = GLib.timeout_add(1000, self._tick)

    def _update_body(self):
        body = f"Reverting to the previous settings in {self._remaining} s."
        if self._problems:
            body += ("\n\nNote: the displays don't fully match what was requested:\n• "
                     + "\n• ".join(self._problems))
        if backend.on_hyprland():
            body += "\n\nChanges last until Hyprland reloads its configuration."
        self.dialog.set_body(body)

    def _tick(self):
        self._remaining -= 1
        if self._remaining <= 0:
            self._countdown_id = None
            self.dialog.force_close()  # closes without emitting "response"
            self._decide(keep=False)
            return GLib.SOURCE_REMOVE
        self._update_body()
        return GLib.SOURCE_CONTINUE

    def _on_response(self, dialog, response):
        self._decide(keep=(response == "keep"))

    def _decide(self, keep):
        if self._decided:  # timeout and a click can race; act only once
            return
        self._decided = True
        if self._countdown_id is not None:
            GLib.source_remove(self._countdown_id)
            self._countdown_id = None
        log.debug("decision: %s", "keep" if keep else "revert")
        if keep:
            safety.tell(self.watchdog, "keep")
            self._end_transaction()
            self.window.toast("Display settings kept")
            self.window.app.persist_async()
        else:
            self._revert()

    # --- reverting ---

    def _revert(self):
        """Restore the snapshot through OUR connection, then verify."""
        desired = backend.desired_from_snapshot(self.snapshot)
        try:
            result = self.om.run_configuration(desired, "apply")
        except WaylandError as e:
            result = f"error: {e}"
        log.debug("self-revert result: %s", result)
        GLib.timeout_add(1000, self._verify_revert, desired)

    def _verify_revert(self, desired, second_check=False):
        problems, _ = backend.verify_state(desired, settle=0)
        log.debug("verify after revert%s: %s", " (second check)" if second_check else "",
                  problems or "ok")
        if not problems and not second_check:
            # Looks right; check once more a bit later before trusting it.
            GLib.timeout_add(REVERIFY_SECONDS * 1000, self._verify_revert, desired, True)
            return GLib.SOURCE_REMOVE
        if not problems:
            safety.tell(self.watchdog, "keep")  # already restored: nothing left to do
            self._end_transaction()
            self.window.toast("Reverted to the previous settings")
        else:
            # Our revert didn't take. On Hyprland another client's revert is
            # ignored while we're connected, so disconnect first, then let
            # the watchdog restore the snapshot, then reconnect.
            self.window.toast("Revert didn't take effect; handing over to the watchdog…")
            self.window.app.handover_to_watchdog(self.watchdog, self._after_handover)
        return GLib.SOURCE_REMOVE

    def _after_handover(self, exit_code):
        self._finish()
        self.window.clear_pending()
        if exit_code == 0:
            self.window.toast("The watchdog restored the previous settings")
        else:
            self.window.alert("Revert failed",
                              f"Neither the GUI nor the watchdog could restore the previous "
                              f"settings. See {safety.WATCHDOG_LOG}.\nSnapshot: {backend.LAST_GOOD}")

    def _end_transaction(self):
        """Keep or verified revert: done. Drop "last applier" status.

        Hard rule on Hyprland: connect -> apply -> verify -> disconnect.
        While the client that applied the last change stays connected,
        Hyprland ignores other clients' changes (kanshi, displayctl, a dock
        daemon), and it keeps sending us stale state. A fresh connection
        fixes both, and gives us a correct initial state.
        """
        self._finish()
        self.window.pending = {}
        self.window.app.reconnect()

    def _finish(self):
        self.busy = False
        self.dialog = None
        self.window.update_actions()


# --- Window and application ---------------------------------------------------------


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, om):
        super().__init__(application=app, title="Displays", default_width=1100, default_height=680)
        self.app = app
        self.om = om
        self.pending = {}  # {head name: state}, see layout.py
        self.applier = Applier(self)

        self.window_title = Adw.WindowTitle(title="Displays")
        header = Adw.HeaderBar(title_widget=self.window_title)
        self.reset_button = Gtk.Button(label="Reset")
        self.reset_button.set_tooltip_text("Discard the changes you haven't applied")
        self.reset_button.connect("clicked", lambda *_: self.clear_pending())
        header.pack_start(self.reset_button)
        self.apply_button = Gtk.Button(label="Apply")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", lambda *_: self.applier.start())
        header.pack_end(self.apply_button)

        self.canvas = LayoutCanvas(self)
        self.details = DetailsPanel(self)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.append(self.canvas)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        body.append(self.details)

        self.banner = Adw.Banner()  # shown if the connection breaks
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.banner)
        content.append(body)

        view = Adw.ToolbarView(content=content)
        view.add_top_bar(header)
        self.toasts = Adw.ToastOverlay(child=view)
        self.set_content(self.toasts)

        self.refresh()

    # --- state ---

    def effective(self):
        return layout.effective(self.om.heads, self.pending)

    def set_om(self, om):
        """Switch to a new connection (after a reconnect)."""
        self.om = om
        self.refresh()

    def schedule_resync(self):
        """A push arrived: (re)start the timer; re-read once the pushes stop.

        Every push restarts the timer, so a burst of pushes causes one
        fresh read, RESYNC_DELAY_MS after the last of them.
        """
        if getattr(self, "_resync_id", None):
            GLib.source_remove(self._resync_id)
        self._resync_id = GLib.timeout_add(RESYNC_DELAY_MS, self._resync)

    def _resync(self):
        self._resync_id = None
        log.debug("pushed state: %s", describe_heads(self.om.heads))
        try:
            fresh = backend.read_states()
        except WaylandError as e:
            log.debug("fresh read failed: %s", e)
            return GLib.SOURCE_REMOVE
        self.om.sync_from(fresh)
        log.debug("fresh read:   %s", describe_heads(self.om.heads))
        self.refresh()
        return GLib.SOURCE_REMOVE

    def refresh(self):
        """The live state changed (or we changed pending): update everything."""
        log.debug("refresh (last done serial %s): %s", self.om.serial, describe_heads(self.om.heads))
        # Drop edits for unplugged monitors, and edits that now match reality.
        self.pending = {
            name: s for name, s in layout.prune(self.pending, self.om.heads).items()
            if layout.differs(self.om.head(name), s)
        }
        states = self.effective()
        on = layout.enabled_count(states)
        subtitle = f"{on} enabled, {len(states) - on} disabled"
        if self.pending:
            subtitle += " · changes not applied"
        self.window_title.set_subtitle(subtitle)

        if self.canvas.selected and self.om.head(self.canvas.selected) is None:
            self.canvas.selected = None  # it was unplugged
        name = self.canvas.selected
        head = self.om.head(name) if name else None
        self.details.show(head, states[name] if head else None, name in self.pending)
        self.update_actions()
        self.canvas.queue_draw()

    def update_actions(self):
        busy = self.applier.busy
        self.apply_button.set_sensitive(bool(self.pending) and not busy)
        self.reset_button.set_sensitive(bool(self.pending) and not busy)
        self.details.set_sensitive(not busy)
        self.canvas.set_sensitive(not busy)

    def clear_pending(self):
        self.pending = {}
        self.refresh()

    # --- user actions ---

    def on_select(self, name):
        self.refresh()

    def on_edit(self, name, what, value):
        head = self.om.head(name)
        if head is None or self.applier.busy:
            return
        states = self.effective()
        state = states[name]
        try:
            if what == "enabled":
                state = layout.with_enabled(states, head, value)
            elif what == "resolution":
                state = layout.with_resolution(state, head, *value)
            elif what == "refresh":
                state = layout.with_refresh(state, head, value)
            elif what == "scale":
                state = layout.with_scale(state, value)
            elif what == "rotation":
                state = layout.with_transform(state, value)
        except ValueError as e:  # includes layout.AllOutputsOff
            self.toast(str(e))
            self.refresh()  # put the control back to what it was
            return
        new_states = dict(states)
        new_states[name] = state
        if what in ("resolution", "scale", "rotation"):
            # The monitor's logical size changed: keep neighbours attached.
            new_states = layout.reflow_after_resize(states, new_states, name)
        self._set_states(layout.normalize(new_states))
        log.debug("edit %s %s=%s -> pending %s", name, what, value, sorted(self.pending))
        self.refresh()

    def on_move(self, name, pos):
        """A monitor was dragged to `pos` (already snapped to a legal spot)."""
        if self.om.head(name) is None or self.applier.busy:
            return
        self._set_states(layout.moved(self.effective(), name, pos))
        log.debug("move %s to %s -> pending %s", name, pos, sorted(self.pending))
        self.refresh()

    def _set_states(self, states):
        """Make `states` the effective layout: pending = what differs from live."""
        pending = self.pending
        for head in self.om.heads:
            pending = layout.set_pending(pending, head, states[head.name])
        self.pending = pending

    # --- messages ---

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def arrange_hint(self):
        """Tried to drag with only one monitor on: say exactly what to do.

        Names the disabled monitor(s). With exactly one, the toast also has
        a "Select <name>" button that selects it, so its "Use this display"
        switch is right there in the panel.
        """
        states = self.effective()
        off = [h.name for h in self.om.heads if not states[h.name]["enabled"]]
        if not off:
            text = "Connect another display to arrange displays."
        elif len(off) == 1:
            text = (f"Turn on {off[0]} to arrange displays: select it and switch on "
                    "“Use this display”.")
        else:
            text = (f"Turn on another display ({', '.join(off)}) to arrange displays: "
                    "select it and switch on “Use this display”.")
        self.last_hint = text  # for the test harness
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=8)
        # HIGH: show it now, not queued behind whatever toast is showing
        # (libadwaita shows one toast at a time).
        toast.set_priority(Adw.ToastPriority.HIGH)
        if len(off) == 1:
            toast.set_button_label(f"Select {off[0]}")
            toast.connect("button-clicked", lambda *_: self.select(off[0]))
        self.toasts.add_toast(toast)

    def select(self, name):
        """Select a monitor as if it had been clicked on the canvas."""
        self.canvas.selected = name
        self.on_select(name)

    def alert(self, heading, body):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def show_error(self, message):
        self.banner.set_title(message)
        self.banner.set_revealed(True)


class DisplayctlApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.om = None
        self.source = None
        self.window = None
        self.persist_result = None  # None / "running" / (ok, output) of the last persist
        self.connect("shutdown", self._on_shutdown)

    def do_activate(self):
        if self.window is not None:  # second launch: just raise the window
            self.window.present()
            return
        try:
            self.om = OutputManager()  # the one persistent connection
        except WaylandError as e:
            print(f"displayctl: {e}", file=sys.stderr)
            self.quit()
            return
        self.window = MainWindow(self, self.om)
        self._watch(self.om)
        self.window.present()

    def _watch(self, om):
        self.source = WaylandSource(om, self.window.show_error, self.window.schedule_resync)

    def _disconnect(self):
        self.source.stop()
        self.om.close()

    def _connect(self):
        try:
            self.om = OutputManager()
        except WaylandError as e:
            self.window.show_error(f"Couldn't reconnect to the compositor: {e}")
            return
        self._watch(self.om)
        self.window.set_om(self.om)

    def reconnect(self):
        """Close our output-management connection and open a fresh one."""
        log.debug("reconnecting (ends 'last applier' status, fresh state)")
        self._disconnect()
        self._connect()

    def persist_async(self):
        """After Keep: make the layout permanent (docs: "Durable Keep").

        Runs `displayctl.py persist` as a separate process: it writes our
        monitors.lua, reloads Hyprland once and verifies (a few seconds), so
        the window stays responsive and our process needs no second Wayland
        connection. Only on Hyprland with the user's dofile line present.
        """
        if not backend.on_hyprland() or not durable.dofile_line_present():
            log.debug("persist skipped (not Hyprland, or hyprland.lua doesn't load our file)")
            return
        self.persist_result = "running"  # for tests and --debug
        self.window.toast("Making the layout permanent…")
        script = str(Path(__file__).resolve().parent / "displayctl.py")
        proc = Gio.Subprocess.new(
            [sys.executable, script, "persist"],
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)

        def finished(proc, result):
            _, out, _ = proc.communicate_utf8_finish(result)
            ok = proc.get_successful()
            self.persist_result = (ok, out or "")
            log.debug("persist %s:\n%s", "ok" if ok else "FAILED", (out or "").strip())
            if ok:
                self.window.toast("Layout saved: it will survive a Hyprland reload")
            else:
                self.window.alert("Couldn't make the layout permanent",
                                  "The change is live, but a Hyprland reload would undo it.\n\n"
                                  + (out or "").strip())

        proc.communicate_utf8_async(None, None, finished)

    def handover_to_watchdog(self, watchdog, done):
        """Disconnect, let the watchdog revert, reconnect, then done(exit_code).

        Needed when our own revert didn't take: on Hyprland the watchdog's
        revert only works once the applying client (us) has disconnected.
        """
        self._disconnect()
        safety.tell(watchdog, "revert")
        waited = [0]

        def poll():
            code = watchdog.poll()
            waited[0] += 1
            if code is None and waited[0] < 120:  # up to 60 s
                return GLib.SOURCE_CONTINUE
            self._connect()
            done(code if code is not None else 1)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(500, poll)

    def _on_shutdown(self, app):
        # Closing the window mid-countdown: our connection closes here, and
        # the watchdog sees its pipe close when we exit, so it reverts.
        log.debug("shutdown: closing the Wayland connection")
        if self.source is not None:
            self.source.stop()
        if self.om is not None and self.om.display._ptr is not None:
            self.om.close()


def drop_vk_suboptimal(level, fields, *_user_data):
    """GLib log writer that drops GDK's harmless VK_SUBOPTIMAL_KHR warnings.

    (They appear when a monitor's mode changes under a Vulkan surface; GTK
    recreates the swapchain and carries on.) Everything else goes to GLib's
    default writer. Each field's value is a raw C pointer; length -1 means
    a NUL-terminated string, readable with ctypes while we're called.
    """
    for f in fields:
        if f.key == "MESSAGE" and f.length == -1 and f.value:
            if b"VK_SUBOPTIMAL_KHR" in ctypes.string_at(f.value):
                return GLib.LogWriterOutput.HANDLED
    return GLib.log_writer_default(level, fields, None)


if __name__ == "__main__":
    # Our own flag, removed before GTK sees argv (it rejects unknown options).
    argv = [a for a in sys.argv if a != "--debug"]
    debug = len(argv) != len(sys.argv)
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S",
    )
    if debug:
        GLib.log_set_writer_func(drop_vk_suboptimal)

    app = DisplayctlApp()

    def quit_on_signal():
        """Ctrl+C / kill: quit through the normal shutdown path, so the
        Wayland connection is closed properly (and no traceback)."""
        log.debug("signal received: quitting")
        app.quit()
        return GLib.SOURCE_CONTINUE

    # Python's default SIGINT handler raises KeyboardInterrupt, and PyGObject
    # installs its own fallback while app.run() runs if that default is still
    # in place. Replace it, then let GLib deliver the signals as main-loop
    # events instead.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    for signum in (signal.SIGINT, signal.SIGTERM):
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signum, quit_on_signal)
    sys.exit(app.run(argv))
