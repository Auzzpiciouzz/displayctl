#!/usr/bin/env python3
"""displayctl GUI, Phase 3 step 1: a READ-ONLY view of the monitor layout.

A canvas draws every enabled monitor as a rectangle at its real position and
relative size. Clicking one shows its details in the side panel. Nothing is
ever applied from here yet.

How Wayland and GTK share one thread
------------------------------------
GTK runs a GLib main loop: it sleeps until something happens (mouse, timer,
a file descriptor becoming readable) and then calls the matching callback.
Our Wayland connection is just a socket (a file descriptor). So instead of a
second thread, we ask GLib to watch that fd and call us when the compositor
has sent something. We then read and dispatch the events, which runs the
same handlers as in the CLI (backend.OutputManager). When a 'done' event
arrives, the state is consistent and we redraw.

We keep ONE output-management connection for the whole lifetime of the
window: it's the connection that will later apply changes, and on Hyprland
only the connection that applied a change can revert it (see
docs/hyprland-persistence.md).
"""

import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, GLib, GLibUnix, Gtk, Pango, PangoCairo  # noqa: E402

from backend import TRANSFORMS, OutputManager, WaylandError  # noqa: E402

APP_ID = "dev.displayctl.App"

# `gui.py --debug` logs every fd wakeup, 'done' event and redraw to stderr.
# (Add WAYLAND_DEBUG=1 for libwayland's per-message trace as well.)
log = logging.getLogger("displayctl.gui")


def describe_heads(heads):
    """Compact one-line state summary for the debug log."""
    parts = []
    for h in heads:
        m = h.current_mode
        mode = f"{m.width}x{m.height}@{m.refresh_mhz}" if (h.enabled and m) else "-"
        parts.append(f"{h.name}:{'on' if h.enabled else 'off'} {mode} "
                     f"pos {h.position[0]},{h.position[1]}")
    return " | ".join(parts)
MARGIN = 24  # empty space (px) around the layout on the canvas


def format_hz(refresh_mhz, decimals=3):
    """144000 -> '144', 143999 -> '143.999' (the protocol sends milli-Hertz)."""
    return f"{refresh_mhz / 1000:.{decimals}f}".rstrip("0").rstrip(".")


# --- Wayland <-> GLib glue -------------------------------------------------------


class WaylandSource:
    """Feeds Wayland events into the GLib main loop.

    GLib calls _on_fd whenever the connection's fd is readable. The steps
    there are libwayland's standard non-blocking pattern:
      read()      prepare_read + read_events: move bytes from the socket
                  into libwayland's event queue (won't block: fd is readable)
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
            self.om.display.read()
            self.om.display.dispatch()
            self.om.display.flush()
        except RuntimeError as e:
            self.on_error(f"Wayland error: {e}")
            self.source_id = None
            return GLib.SOURCE_REMOVE
        if self.om.done_count == dones_before:
            # Something changed but no 'done' came with it. The protocol
            # says the compositor must send 'done' to every client after a
            # batch of changes; Hyprland 0.56.2 only sends it to the client
            # that applied the change. This connection only carries
            # output-management events, and Hyprland sends a change as one
            # burst that we just dispatched in full, so refresh now.
            log.debug("state changed without a 'done' event; refreshing anyway")
            self.on_changed()
        return GLib.SOURCE_CONTINUE  # keep watching

    def stop(self):
        if self.source_id is not None:
            GLib.source_remove(self.source_id)
            self.source_id = None


# --- The canvas ---------------------------------------------------------------------


class LayoutCanvas(Gtk.DrawingArea):
    """Draws enabled monitors to scale; click one to select it."""

    def __init__(self, om, on_select):
        super().__init__(hexpand=True, vexpand=True)
        self.om = om
        self.on_select = on_select
        self.selected = None  # name of the selected head, or None
        self._hit_boxes = []  # (name, x, y, w, h) in widget pixels, from the last draw

        self.set_draw_func(self._draw)

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_click)
        self.add_controller(click)

        # Redraw when the user switches light/dark or accent colour.
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *_: self.queue_draw())
        style.connect("notify::accent-color-rgba", lambda *_: self.queue_draw())

    def _enabled_heads(self):
        return [h for h in self.om.heads if h.enabled and h.current_mode is not None]

    def _draw(self, area, cr, width, height):
        heads = self._enabled_heads()
        log.debug("redraw %dx%d: %s", width, height, describe_heads(heads))
        self._hit_boxes = []
        if not heads:
            return

        # 1. Bounding box of the whole layout in compositor coordinates
        #    (logical size = pixels / scale, swapped when rotated).
        boxes = []
        for head in heads:
            w, h = head.logical_size()
            x, y = head.position
            boxes.append((head, x, y, w, h))
        left = min(b[1] for b in boxes)
        top = min(b[2] for b in boxes)
        right = max(b[1] + b[3] for b in boxes)
        bottom = max(b[2] + b[4] for b in boxes)

        # 2. One scale factor for both axes keeps the real proportions;
        #    then centre the layout in the widget.
        factor = min((width - 2 * MARGIN) / (right - left),
                     (height - 2 * MARGIN) / (bottom - top))
        offset_x = (width - (right - left) * factor) / 2
        offset_y = (height - (bottom - top) * factor) / 2

        fg = self.get_color()  # the theme's text colour (light or dark)
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()

        for head, x, y, w, h in boxes:
            sx = offset_x + (x - left) * factor
            sy = offset_y + (y - top) * factor
            sw, sh = w * factor, h * factor
            # A small gap so touching monitors stay visually separate.
            gap = 3
            rect = (sx + gap, sy + gap, sw - 2 * gap, sh - 2 * gap)
            self._hit_boxes.append((head.name, *rect))
            is_selected = head.name == self.selected

            # Fill
            if is_selected:
                cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.30)
            else:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.08)
            cr.rectangle(*rect)
            cr.fill_preserve()  # keep the path to draw the border next
            # Border
            if is_selected:
                cr.set_source_rgba(accent.red, accent.green, accent.blue, 1.0)
                cr.set_line_width(3)
            else:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
                cr.set_line_width(1.5)
            cr.stroke()

            # Label: name (bold) and mode, centred in the rectangle.
            mode = head.current_mode
            markup = (
                f"<b>{GLib.markup_escape_text(head.name)}</b>\n"
                f"{mode.width}×{mode.height} @ {format_hz(mode.refresh_mhz, 2)} Hz"
            )
            layout = self.create_pango_layout(None)
            layout.set_markup(markup, -1)
            layout.set_alignment(Pango.Alignment.CENTER)
            text_w, text_h = layout.get_pixel_size()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 1.0)
            cr.move_to(rect[0] + (rect[2] - text_w) / 2, rect[1] + (rect[3] - text_h) / 2)
            PangoCairo.show_layout(cr, layout)

    def _on_click(self, gesture, n_press, x, y):
        clicked = None
        for name, bx, by, bw, bh in self._hit_boxes:
            if bx <= x <= bx + bw and by <= y <= by + bh:
                clicked = name
                break
        self.selected = clicked  # clicking empty space clears the selection
        self.queue_draw()
        self.on_select(clicked)


# --- The details panel -----------------------------------------------------------


class DetailsPanel(Gtk.Stack):
    """Side panel: a placeholder, or the selected monitor's details."""

    FIELDS = [
        ("Name", "name"), ("Description", "description"), ("Make", "make"),
        ("Model", "model"), ("Serial", "serial"), ("Resolution", "resolution"),
        ("Refresh rate", "refresh"), ("Scale", "scale"), ("Position", "position"),
        ("Rotation", "transform"), ("Physical size", "physical"),
    ]

    def __init__(self):
        # hexpand=False explicitly: otherwise the page inside would make the
        # panel grab most of the window's width from the canvas.
        super().__init__(width_request=340, hexpand=False)

        placeholder = Adw.StatusPage(
            icon_name="video-display-symbolic",
            title="No monitor selected",
            description="Click a monitor to see its details.",
        )
        placeholder.add_css_class("compact")
        self.add_named(placeholder, "empty")

        group = Adw.PreferencesGroup(title="Details")
        self.rows = {}
        for title, key in self.FIELDS:
            # "property" style: small title above a prominent value
            # (the subtitle), which wraps cleanly when it's long.
            row = Adw.ActionRow(title=title, subtitle_selectable=True)
            row.add_css_class("property")
            group.add(row)
            self.rows[key] = row
        page = Adw.PreferencesPage()
        page.add(group)
        self.add_named(page, "details")

    def show_head(self, head):
        if head is None:
            self.set_visible_child_name("empty")
            return
        mode = head.current_mode
        values = {
            "name": head.name,
            "description": head.description,
            "make": head.make,
            "model": head.model,
            "serial": head.serial or "(none)",
            "resolution": f"{mode.width} × {mode.height}" if mode else "—",
            "refresh": f"{format_hz(mode.refresh_mhz)} Hz" if mode else "—",
            "scale": f"{head.scale:g}",
            "position": f"{head.position[0]}, {head.position[1]}",
            "transform": TRANSFORMS.get(head.transform, str(head.transform)),
            "physical": f"{head.physical_size[0]} × {head.physical_size[1]} mm",
        }
        for key, text in values.items():
            # Subtitles are Pango markup: escape so "&" etc. show literally.
            self.rows[key].set_subtitle(GLib.markup_escape_text(text))
        self.set_visible_child_name("details")


# --- Window and application ---------------------------------------------------------


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, om):
        super().__init__(application=app, title="Displays", default_width=1000, default_height=600)
        self.om = om

        self.window_title = Adw.WindowTitle(title="Displays")
        header = Adw.HeaderBar(title_widget=self.window_title)

        self.canvas = LayoutCanvas(om, self._on_select)
        self.details = DetailsPanel()

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
        self.set_content(view)

        # Every 'done' event = a new consistent state: refresh everything.
        om.listeners.append(self.refresh)
        self.refresh()

    def _on_select(self, name):
        self.details.show_head(self.om.head(name) if name else None)

    def refresh(self):
        log.debug("refresh (last done serial %s): %s", self.om.serial, describe_heads(self.om.heads))
        enabled = sum(1 for h in self.om.heads if h.enabled)
        disabled = len(self.om.heads) - enabled
        self.window_title.set_subtitle(f"{enabled} enabled, {disabled} disabled · read-only")
        # The selected monitor may have been unplugged or disabled.
        head = self.om.head(self.canvas.selected) if self.canvas.selected else None
        if head is None or not head.enabled:
            self.canvas.selected = None
            head = None
        self.details.show_head(head)
        self.canvas.queue_draw()

    def show_error(self, message):
        self.banner.set_title(message)
        self.banner.set_revealed(True)


class DisplayctlApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.om = None
        self.source = None
        self.window = None
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
        self.source = WaylandSource(self.om, self.window.show_error, self.window.refresh)
        self.window.present()

    def _on_shutdown(self, app):
        if self.source is not None:
            self.source.stop()
        if self.om is not None:
            self.om.close()


if __name__ == "__main__":
    # Our own flag, removed before GTK sees argv (it rejects unknown options).
    argv = [a for a in sys.argv if a != "--debug"]
    logging.basicConfig(
        level=logging.DEBUG if len(argv) != len(sys.argv) else logging.WARNING,
        format="%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S",
    )
    sys.exit(DisplayctlApp().run(argv))
