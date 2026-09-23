# Phase 3 step 4: drag-to-arrange

## What the user does

Drag an enabled monitor on the canvas. While dragging, it follows the
pointer but is always shown at a legal, snapped position. On release the new
layout becomes a pending edit ("Not applied"), exactly like a resolution
change, and goes through the same Apply -> Keep/Revert flow. Reset discards
it. Disabled monitors can't be dragged (they have no position).

## Coordinates

- Everything is in **logical pixels**, the compositor's layout space:
  width/height = mode pixels / scale, swapped for 90/270 rotation
  (`layout.logical_size`). A 2560x1440 monitor at 125% is 2048x1152.
- The canvas maps logical -> screen with one factor and an offset. That
  mapping is **frozen while dragging**, otherwise the view would rescale
  under the pointer as the layout's bounding box changes.
- Positions are whole numbers.

## Rules for a legal layout

1. **No overlaps**: no two enabled monitors share any area.
2. **No gaps**: every enabled monitor touches at least one other along an
   edge (a shared edge segment of length > 0, not just a corner), and the
   whole layout is connected (you can walk from any monitor to any other
   across shared edges). A single monitor is trivially legal.
3. **Anchor at 0,0**: one monitor stays at 0,0; after any change the whole
   layout is shifted so the anchor is back there. The anchor is the monitor
   at 0,0 in the live state (normally the one you use most), or else the
   top-left-most enabled monitor. Dragging the anchor itself effectively
   moves everything else the opposite way, like Windows.

## Snapping (layout.snap_position)

Input: all effective states, the dragged monitor, and the raw position the
pointer asks for. Output: the legal position closest to it.

1. For every other enabled monitor O and each of its four sides, build a
   candidate that puts the dragged monitor flush against that side
   (e.g. right of O: x = O.right), keeping the raw coordinate on the other
   axis but clamped so the two share at least 1 px of edge.
2. On that free axis, snap to alignments with O when within a threshold
   (default 64 logical px, converted from ~10 screen px): top/top,
   bottom/bottom, centre/centre (left/left etc. for above/below).
3. Keep candidates that satisfy rules 1-2 for the whole layout.
4. Pick the one nearest to the raw position. If none is legal (shouldn't
   happen), keep the old position.

Then normalise (rule 3).

## Size changes

Changing resolution, scale or rotation changes a monitor's logical size, so
a neighbour could end up overlapping or floating. `layout.reflow_after_resize`
handles the common cases: monitors that touched the old right edge move by
the width change, monitors that touched the old bottom edge move by the
height change. If the result is still illegal, `layout.validate` reports it
and Apply refuses with "monitors overlap / don't touch: drag to arrange".
Enabling a monitor already places it flush to the right of the layout.

## Testing

- Pure functions (snap, normalise, connectivity, overlap, reflow) have unit
  tests in tests/test_arrange.py.
- The canvas drag is exercised by the fake harness (simulated drag events,
  fake apply/watchdog). Nothing is applied to real displays until the
  manual test list.

## Status (2026-09-23)

Built: layout.snap_position / moved / normalize / reflow_after_resize /
arrangement_problems (15 unit tests in tests/test_arrange.py), canvas drag in
gui.py (GestureDrag, frozen view, live snapped preview, pending on release),
Apply refuses an illegal arrangement. Fake harness: 50/50. Not yet applied on
real displays (manual test list in the commit).

Known limitation: because the view is frozen while dragging, a monitor
dragged beyond the original layout's bounding box can reach the canvas edge;
the view recentres on release.
