"""Confirm painted item VAT and correct it through an exact guarded popup.

Cached native choices/highlights do not establish the document's current VAT.
The native_items opener owns the guarded single click and cancellation context.
"""

from decimal import Decimal
import re

from .desktop import DesktopError, Rect, wait_for
from .workflow_rules import WorkflowReview, required_vat_definition


def parse_item_vat_display(display):
    """Read the converter's complete final percentage, never the VAT name."""
    match = re.fullmatch(r"(.+) \(([0-9]+(?:\.[0-9]{1,6})?)%\)", display) if isinstance(display, str) else None
    if match is None or not match[1].strip() or Decimal(match[2]) > 100:
        raise DesktopError("Selected VAT requires its complete native name and final percentage display.")
    return match[1], Decimal(match[2])


def _popup_guard(desktop, context):
    desktop._require_focus()
    if not callable(context.source_guard) or context.source_guard() is not True:
        raise DesktopError("The exact source item/document context changed while reading VAT.")
    popup = context.popup
    if popup.title != "":
        raise DesktopError("VAT context does not identify its observed unnamed popup.")
    current = desktop._control(popup.element)
    focused = desktop._read(desktop.root, "AXFocusedWindow", required=True)
    matches = [window for window in desktop.windows()
               if desktop.cf.CFEqual(window.element, popup.element)]
    if (len(matches) != 1 or matches[0].title != "" or matches[0].bounds != popup.bounds
            or current.role != "AXWindow" or current.title not in (None, "")
            or current.bounds != popup.bounds
            or desktop._read(popup.element, "AXModal", required=True) is not False
            or not desktop.cf.CFEqual(focused, popup.element)):
        raise DesktopError("The exact owned nonmodal VAT popup is no longer focused and unchanged.")


def _elements(desktop, element, attribute, *, maximum=64):
    value = desktop._read(element, attribute, required=True)
    if isinstance(value, (str, bytes)):
        raise DesktopError(f"VAT {attribute} is not a native element collection.")
    try:
        values = list(value)
    except TypeError as exc:
        raise DesktopError(f"VAT {attribute} is not a native element collection.") from exc
    if len(values) > maximum:
        raise DesktopError(f"VAT {attribute} exceeds the bounded complete-read limit.")
    return values


def _only_child(desktop, parent, role):
    children = _elements(desktop, parent.element, "AXChildren", maximum=8)
    if len(children) != 1:
        raise DesktopError(f"VAT popup requires exactly one direct {role} child.")
    child = desktop._control(children[0])
    table_border = (parent.role == "AXScrollArea" and role == "AXTable"
                    and child.bounds == Rect(parent.bounds.x - 1, parent.bounds.y - 1,
                                             parent.bounds.width + 2, parent.bounds.height + 2))
    if child.role != role or (not parent.bounds.contains_rect(child.bounds) and not table_border):
        raise DesktopError(f"VAT popup's direct {role} child is unproved or outside its parent.")
    return child


def read_vat_popup(ui, context):
    """Observe one exact native popup highlight; leave it open on failure.

    The supplied context must come from native_items' verified single-click
    opener. No unnamed-window screenshot or main-window capture guard is used.
    This popup state does not prove the current painted document VAT.
    """
    desktop = ui.desktop
    _popup_guard(desktop, context)
    popup = desktop._control(context.popup.element)
    scroll = _only_child(desktop, popup, "AXScrollArea")
    table = _only_child(desktop, scroll, "AXTable")
    if table.enabled is not True:
        raise DesktopError("VAT popup table is not enabled.")
    rows = _elements(desktop, table.element, "AXRows")
    selected = _elements(desktop, table.element, "AXSelectedRows")
    if len(selected) != 1:
        raise DesktopError(f"VAT needs one selected native row; observed {len(selected)}.")
    indexes = [index for index, row in enumerate(rows)
               if desktop.cf.CFEqual(row, selected[0])]
    if len(indexes) != 1:
        raise DesktopError("The selected VAT row is not one exact member of the popup table.")
    row = desktop._control(selected[0])
    if (row.role != "AXRow" or not scroll.bounds.contains_rect(row.bounds)
            or not popup.bounds.contains_rect(row.bounds)):
        raise DesktopError("The selected VAT row is unproved or outside its popup table.")
    text = _only_child(desktop, row, "AXStaticText")
    if not scroll.bounds.contains_rect(text.bounds) or not popup.bounds.contains_rect(text.bounds):
        raise DesktopError("Selected VAT text is clipped outside its observed popup/scroll area.")
    name, percent = parse_item_vat_display(text.value)
    _popup_guard(desktop, context)
    fresh_table, fresh_row, fresh_text = (desktop._control(control.element) for control in (table, row, text))
    fresh_selection = _elements(desktop, table.element, "AXSelectedRows")
    if (fresh_table.role != table.role or fresh_table.bounds != table.bounds
            or fresh_table.enabled is not True
            or fresh_row.role != row.role or fresh_row.bounds != row.bounds
            or fresh_text.role != text.role or fresh_text.bounds != text.bounds
            or fresh_text.value != text.value or len(fresh_selection) != 1
            or not desktop.cf.CFEqual(fresh_selection[0], row.element)):
        raise DesktopError("Native VAT selection/value changed during readback.")
    return {"display": text.value, "name": name, "percent": format(percent, "f"),
            "evidence": {"method": "native AXSelectedRows and complete row AXStaticText value",
                         "popup_bounds": popup.bounds.as_dict(), "table_bounds": table.bounds.as_dict(),
                         "selected_row_index": indexes[0], "available_row_count": len(rows),
                         "source": context.evidence,
                         "precision": "The displayed percentage is observed; hidden extra precision is not inferred."}}


def read_item_vat(ui, *, row_index=0, expected_sku, title="New Order"):
    """Read native popup highlight state; document VAT needs painted readback."""
    from .native_items import open_item_vat_popup

    context = open_item_vat_popup(ui, row_index=row_index, expected_sku=expected_sku, title=title)
    if not callable(context.cancel):
        raise DesktopError("VAT opener did not provide its exact guarded cancellation action.")
    result = read_vat_popup(ui, context)
    context.cancel()
    ui.actions.append({"action": "read_native_item_vat", "sku": expected_sku,
                       "row_index": row_index, **result})
    return result


def _vat_choices(ui, context):
    """Read every native choice; no clipped, duplicate or hidden row is accepted."""
    d = ui.desktop
    _popup_guard(d, context)
    popup = d._control(context.popup.element)
    scroll = _only_child(d, popup, "AXScrollArea")
    table = _only_child(d, scroll, "AXTable")
    if table.enabled is not True:
        raise DesktopError("VAT popup table is not enabled.")
    elements = _elements(d, table.element, "AXRows")
    choices = []
    for element in elements:
        if any(d.cf.CFEqual(element, old["row"].element) for old in choices):
            raise DesktopError("VAT popup contains a duplicate native row.")
        row = d._control(element)
        if (row.role != "AXRow" or not scroll.bounds.contains_rect(row.bounds)
                or not popup.bounds.contains_rect(row.bounds)):
            raise DesktopError("Every VAT choice must be a complete row inside its exact popup table.")
        text = _only_child(d, row, "AXStaticText")
        if not scroll.bounds.contains_rect(text.bounds) or not popup.bounds.contains_rect(text.bounds):
            raise DesktopError("Every VAT choice text must be complete inside the observed popup/scroll area.")
        name, percent = parse_item_vat_display(text.value)
        if any(choice["name"] == name for choice in choices):
            raise WorkflowReview("conflicting_vat", "VAT popup contains duplicate exact Names.", name=name)
        choices.append({"row": row, "text": text, "name": name, "percent": percent})
    if not choices:
        raise DesktopError("VAT popup has no complete native choices.")
    _popup_guard(d, context)
    return table, choices


def _same_choices(d, original, fresh):
    before_table, before = original
    after_table, after = fresh
    if (before_table != after_table or not d.cf.CFEqual(before_table.element, after_table.element)
            or len(before) != len(after)):
        return False
    return all(a["row"] == b["row"] and a["text"] == b["text"]
               and d.cf.CFEqual(a["row"].element, b["row"].element)
               and d.cf.CFEqual(a["text"].element, b["text"].element)
               for a, b in zip(before, after))


def _source_choice(choices, wanted):
    matched = [choice for choice in choices if choice["name"] == wanted["name"]]
    if matched and matched[0]["percent"] != Decimal(wanted["value"]):
        raise WorkflowReview("conflicting_vat", "The required VAT Name has another displayed rate.",
                             expected=wanted, observed=matched[0]["text"].value)
    return matched[0] if matched else None


def _popup_highlight(ui, inventory):
    """Retain popup selection state as a guard, never as the document VAT."""
    d = ui.desktop
    table, choices = inventory
    selected = _elements(d, table.element, "AXSelectedRows")
    if len(selected) > 1:
        raise DesktopError("VAT popup has multiple highlighted native rows.")
    indexes = [index for index, choice in enumerate(choices)
               if selected and d.cf.CFEqual(choice["row"].element, selected[0])]
    if selected and len(indexes) != 1:
        raise DesktopError("VAT popup highlight is not one exact member of its complete inventory.")
    return indexes


def _click_vat_choice(ui, context, inventory, highlight, choice, record):
    """Click one measured, actionless native row through the owned-popup API."""
    d = ui.desktop
    target = choice["text"]

    def content_guard():
        _popup_guard(d, context)
        if _popup_highlight(ui, inventory) != highlight:
            raise DesktopError("The VAT popup highlight changed before source selection; input refused.")
        for control in (choice["row"], target):
            code, names = d.ax.AXUIElementCopyActionNames(control.element, None)
            if code != d.ax.kAXErrorSuccess or names is None or isinstance(names, (str, bytes)):
                raise DesktopError("VAT native action inventory is unreadable; click refused.")
            if list(names):
                raise DesktopError("VAT row/text differs from the demonstrated actionless native shape.")
        if choice["row"].enabled not in (None, True) or target.enabled is not True:
            raise DesktopError("The exact VAT row/text is disabled; click refused.")
        if not _same_choices(d, inventory, _vat_choices(ui, context)):
            raise DesktopError("The complete VAT choices changed before selection; input refused.")
        _popup_guard(d, context)
        return True

    content_guard()
    record.update(selection_requested=True, selection_method="guarded owned-popup click",
                  popup_highlight_indexes=highlight,
                  selected_display=choice["text"].value,
                  selection_target={"role": target.role, "enabled": target.enabled,
                                    "bounds": target.bounds.as_dict(), "exposed_actions": []},
                  choices=[{"display": option["text"].value, "name": option["name"],
                            "percent": format(option["percent"], "f"),
                            "bounds": option["row"].bounds.as_dict()} for option in inventory[1]])
    # The intention is recorded first. An uncertain action is never retried.
    d.click_owned_popup(context.popup, choice["row"], target.bounds.x + target.bounds.width / 2,
                        target.bounds.y + target.bounds.height / 2,
                        source_guard=context.source_guard, content_guard=content_guard)

    def parent_ready():
        d._require_focus()
        if context.source_guard() is not True:
            raise DesktopError("Source document/table changed after VAT selection.")
        focused = d._read(d.root, "AXFocusedWindow", required=True)
        parent = d.window(ui.title)
        return (d.cf.CFEqual(focused, parent.element)
                and not any(d.cf.CFEqual(window.element, context.popup.element) for window in d.windows()))

    wait_for(parent_ready, "selected VAT popup closed to the unchanged source document", timeout=2)


def ensure_item_vat(ui, item, ledger, journal_directory, *, row_index=0, title="New Order"):
    """Confirm/correct only this transaction row; never rewrite its Product master.

    A missing exact definition gets one resolution and one fresh popup check.
    Complete painted VAT is authoritative before/after. Cached popup choices
    and its highlight state never establish the document row's selected VAT.
    """
    from .document_content import parse_painted_vat
    from .native_items import open_item_vat_popup, read_painted_item_cell
    from .product_sequence import resolve_vat

    if title != "New Order":
        raise ValueError("Item VAT correction is restricted to the retained New Order.")
    wanted = required_vat_definition(item.vat_percent)
    common = {"row_index": row_index, "expected_sku": item.sku, "title": title}
    record = {"action": "ensure_native_item_vat", "sku": item.sku, "row_index": row_index,
              "expected": wanted, "selection_requested": False, "verified": False}
    ui.actions.append(record)

    def painted():
        raw = read_painted_item_cell(ui, header="VAT", **common)
        actual = parse_painted_vat(raw["text"])
        return {"display": raw["text"], "name": actual["name"], "percent": actual["vat_percent"],
                "evidence": {"method": "complete painted VAT cell", "read": raw}}

    before = painted()
    record["before"] = before
    if Decimal(before["percent"]) == item.vat_percent:
        record.update(after=before, verified=True)
        return record
    context = open_item_vat_popup(ui, **common)
    inventory = _vat_choices(ui, context)
    choice = _source_choice(inventory[1], wanted)
    if choice is None:
        context.cancel()
        record["definition_resolution"] = resolve_vat(ui, item, ledger, journal_directory)
        context = open_item_vat_popup(ui, **common)
        inventory = _vat_choices(ui, context)
        choice = _source_choice(inventory[1], wanted)
        if choice is None:
            context.cancel()
            raise WorkflowReview("vat_not_available", "The resolved VAT is absent from the retained Order's fresh popup; no source selection dispatched.",
                                 expected=wanted)
    highlight = _popup_highlight(ui, inventory)
    _click_vat_choice(ui, context, inventory, highlight, choice, record)
    actual = painted()
    record["after"] = actual
    if Decimal(actual["percent"]) != item.vat_percent:
        raise WorkflowReview("item_vat_mismatch", "The fresh selected VAT differs from the source; do not repeat input.",
                             expected=wanted, observed=actual)
    record["verified"] = True
    return record
