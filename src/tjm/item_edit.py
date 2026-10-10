"""Edit one observed SWT item cell; callers must reread its committed value."""

from .desktop import DesktopError, wait_for
from .ui_tables import (CompleteTable, _pixel_box, _refresh_observation,
                        _signature, _table_at, _valid_box)


def edit_item_cell(ui, complete_table, row, header, value):
    """Enter exact text in a freshly guarded cell, then commit once with Tab.

    The native editing field is temporary: it may cease to exist on Tab. Its
    exact value is therefore checked before commit. The return record proves
    input dispatch only; reread the table before another edit or a Save.
    """
    if not isinstance(complete_table, CompleteTable):
        raise DesktopError("Item editing requires a complete table observation.")
    if not any(row is candidate for candidate in complete_table.rows):
        raise DesktopError("The edited row must be the exact observed row object.")
    if _signature(complete_table.readback) != complete_table._signature:
        raise DesktopError("Item table readback was modified after observation.")
    if not isinstance(header, str) or header not in row["cell_boxes"]:
        raise DesktopError("The edited header must name an observed cell.")
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("Item input must be text without null characters.")

    d = ui.desktop
    fresh = _refresh_observation(ui, complete_table.observation, "before-item-edit",
                                 complete_table.search_field, complete_table.search_value)
    if fresh.area.enabled is False:
        raise DesktopError("The native item table is explicitly disabled.")
    box = _valid_box(row["cell_boxes"][header], fresh.table_box, "Edited cell")
    point = fresh.capture.screen_point((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    hit, _ = _table_at(ui, fresh.capture, point)
    if not d.cf.CFEqual(hit.element, fresh.area.element) or hit.bounds != fresh.area.bounds:
        raise DesktopError("The cell point does not belong to the exact observed table.")
    d.double_click(fresh.capture, *point)

    capture = ui.snapshot("item-cell-editor", ocr=False)
    previous = fresh.capture
    if (capture.process_identity != previous.process_identity
            or capture.window_title != previous.window_title
            or not d.cf.CFEqual(capture.window_element, previous.window_element)
            or capture.window_bounds != previous.window_bounds
            or capture.screen_rect != previous.screen_rect
            or capture.image.size != previous.image.size):
        raise DesktopError("The item editor moved to another window or capture geometry.")
    control = d.hit_test(capture, *point, expected_role="AXTextField")
    _valid_box(_pixel_box(capture, control.bounds), fresh.table_box, "Native item editor")

    def exact_focus():
        d.guard(capture)
        current = d._control(control.element)
        return (current.role == "AXTextField" and current.bounds == control.bounds
                and current.enabled is True and d._read(control.element, "AXFocused") is True)

    wait_for(exact_focus, f"exact {header} item editor focus", timeout=2)
    d.key(capture, "cmd+a", focus_guard=exact_focus)
    d.paste(capture, value, focus_guard=exact_focus)

    def entered_exactly():
        return exact_focus() and d._control(control.element).value == value

    wait_for(entered_exactly, f"exact {header} item value before commit", timeout=2)
    d.key(capture, "tab", focus_guard=entered_exactly)
    record = {"action": "edit_item_cell", "header": header,
              "cell_box": list(box), "row_before": dict(row["values"]),
              "native_before": control.value, "requested": value,
              "native_before_commit": value, "native_role": control.role,
              "exact_focus_checked": True, "commit_key": "tab",
              "committed_value_verified": False}
    ui.actions.append(record)
    return record
